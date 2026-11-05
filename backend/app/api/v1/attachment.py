import json
import uuid
from typing import List
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import get_current_user_id
from app.core.config import settings
from app.core.exceptions import ConversationNotFoundError
from app.core.logging import logger
from app.ingestion.parsers.factory import ParserFactory
from app.models.conversation import Conversation, ConversationAttachment
from app.rag.context_budget import estimate_tokens
from app.schemas.attachment import (
    AttachmentPageItem,
    ConversationAttachmentDetailResponse,
    ConversationAttachmentResponse,
)
from app.services.attachment_storage import (
    delete_attachment_physical_dir,
    save_attachment_file_atomic,
    validate_file_metadata,
    verify_file_content_and_mime,
)

router = APIRouter(prefix="/conversations/{conversation_id}/attachments", tags=["Chat Attachments"])


async def _get_conversation_or_404(conversation_id: str, db: AsyncSession, user_id: str) -> Conversation:
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.owner_id == user_id,
    )
    res = await db.execute(stmt)
    conv = res.scalar_one_or_none()
    if not conv:
        raise ConversationNotFoundError(conversation_id)
    return conv


@router.post("", response_model=List[ConversationAttachmentResponse], status_code=status.HTTP_201_CREATED)
async def upload_attachments(
    conversation_id: str,
    files: List[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Upload one or more documents (PDF, DOCX, TXT, MD) to a Conversation as ephemeral attachments.
    Files are stored in physical storage, parsed, and recorded with page-level metadata.
    """
    await _get_conversation_or_404(conversation_id, db, user_id=user_id)

    # 1. Check existing attachment count limit
    count_stmt = (
        select(func.count(ConversationAttachment.id))
        .where(ConversationAttachment.conversation_id == conversation_id)
    )
    current_count = (await db.execute(count_stmt)).scalar() or 0
    if current_count + len(files) > settings.MAX_ATTACHMENTS_PER_CONVERSATION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"当前会话附件数量将超过上限限制：当前已有 {current_count} 个，"
                f"本次尝试添加 {len(files)} 个，最大允许 {settings.MAX_ATTACHMENTS_PER_CONVERSATION} 个"
            ),
        )

    created_attachments: List[ConversationAttachment] = []
    saved_att_ids: List[str] = []

    try:
        for file in files:
            content = await file.read()
            # Validate name and size bounds
            try:
                clean_name, ext = validate_file_metadata(file.filename or "attachment", len(content))
            except ValueError as val_err:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err))

            # Verify actual content magic bytes & MIME type
            try:
                mime_type = verify_file_content_and_mime(content, ext)
            except ValueError as mime_err:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(mime_err))

            att_id = str(uuid.uuid4())
            # Save file atomically to disk
            try:
                final_path, storage_key = save_attachment_file_atomic(
                    conversation_id, att_id, ext, content, owner_id=user_id
                )
                saved_att_ids.append(att_id)
            except Exception as store_err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"保存附件物理文件失败: {str(store_err)}",
                )

            # Parse file content
            pages_data = []
            total_pages = 0
            char_length = 0
            token_count = 0
            parse_status = "COMPLETED"
            error_message = None

            try:
                parser = ParserFactory.get_parser(final_path)
                parsed_doc = parser.parse(final_path)
                total_pages = parsed_doc.total_pages
                for p in parsed_doc.pages:
                    pages_data.append({
                        "page_number": p.page_number,
                        "text": p.text,
                        "metadata": p.metadata,
                    })
                char_length = sum(len(p.text) for p in parsed_doc.pages)
                token_count = sum(estimate_tokens(p.text) for p in parsed_doc.pages)
            except Exception as parse_exc:
                logger.error(f"Failed to parse uploaded attachment {clean_name}: {parse_exc}", exc_info=True)
                parse_status = "FAILED"
                error_message = f"解析异常: {str(parse_exc)}"

            attachment = ConversationAttachment(
                id=att_id,
                conversation_id=conversation_id,
                original_filename=clean_name,
                storage_key=storage_key,
                file_extension=ext,
                mime_type=mime_type,
                file_size=len(content),
                parse_status=parse_status,
                page_count=total_pages,
                char_length=char_length,
                token_count=token_count,
                parsed_content_json=json.dumps(pages_data, ensure_ascii=False),
                error_message=error_message,
            )
            db.add(attachment)
            created_attachments.append(attachment)

        await db.commit()
        for att in created_attachments:
            await db.refresh(att)

        # Invalidate in-memory retrieval index for this conversation
        from app.attachments import attachment_retrieval_service
        attachment_retrieval_service.invalidate_conversation(conversation_id)

        return created_attachments

    except HTTPException:
        await db.rollback()
        for att_id in saved_att_ids:
            delete_attachment_physical_dir(conversation_id, att_id, owner_id=user_id)
        raise
    except Exception as exc:
        await db.rollback()
        for att_id in saved_att_ids:
            delete_attachment_physical_dir(conversation_id, att_id, owner_id=user_id)
        logger.error(f"Failed to complete attachment upload: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"附件上传处理失败: {str(exc)}") from exc


@router.get("", response_model=List[ConversationAttachmentResponse])
async def list_attachments(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    List all attachments for the given conversation.
    """
    await _get_conversation_or_404(conversation_id, db, user_id=user_id)

    stmt = (
        select(ConversationAttachment)
        .where(ConversationAttachment.conversation_id == conversation_id)
        .order_by(ConversationAttachment.created_at.asc())
    )
    res = await db.execute(stmt)
    return res.scalars().all()


@router.get("/{attachment_id}", response_model=ConversationAttachmentDetailResponse)
async def get_attachment_detail(
    conversation_id: str,
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Get detailed attachment information including extracted pages and full text.
    Enforces strict conversation boundary (cannot access attachments of other conversations).
    """
    await _get_conversation_or_404(conversation_id, db, user_id=user_id)

    stmt = select(ConversationAttachment).where(
        ConversationAttachment.id == attachment_id,
        ConversationAttachment.conversation_id == conversation_id,
    )
    res = await db.execute(stmt)
    att = res.scalar_one_or_none()
    if not att:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"指定的附件不存在或不属于当前会话: {attachment_id}",
        )

    pages = [
        AttachmentPageItem(
            page_number=p.get("page_number", 1),
            text=p.get("text", ""),
            metadata=p.get("metadata", {}),
        )
        for p in att.parsed_pages
    ]

    return ConversationAttachmentDetailResponse(
        id=att.id,
        conversation_id=att.conversation_id,
        original_filename=att.original_filename,
        storage_key=att.storage_key,
        file_extension=att.file_extension,
        mime_type=att.mime_type,
        file_size=att.file_size,
        parse_status=att.parse_status,
        page_count=att.page_count,
        char_length=att.char_length,
        token_count=att.token_count,
        error_message=att.error_message,
        created_at=att.created_at,
        updated_at=att.updated_at,
        parsed_pages=pages,
        extracted_text=att.extracted_text,
    )


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    conversation_id: str,
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Delete an attachment record and remove its physical directory on disk.
    """
    await _get_conversation_or_404(conversation_id, db, user_id=user_id)

    stmt = select(ConversationAttachment).where(
        ConversationAttachment.id == attachment_id,
        ConversationAttachment.conversation_id == conversation_id,
    )
    res = await db.execute(stmt)
    att = res.scalar_one_or_none()
    if not att:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"指定的附件不存在或不属于当前会话: {attachment_id}",
        )

    await db.delete(att)
    await db.commit()

    # Clean up physical files
    delete_attachment_physical_dir(conversation_id, attachment_id, owner_id=user_id)

    # Invalidate in-memory retrieval index for this conversation
    from app.attachments import attachment_retrieval_service
    attachment_retrieval_service.invalidate_conversation(conversation_id)
