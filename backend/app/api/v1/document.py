from pathlib import Path
from typing import List
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import get_current_user_id
from app.core.exceptions import DocumentNotFoundError
from app.core.logging import logger
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.schemas.document import ChunkPageResponse, ChunkResponse, DocumentResponse

router = APIRouter(prefix="/documents", tags=["Documents"])


async def _get_user_document_or_404(doc_id: str, db: AsyncSession, user_id: str) -> Document:
    stmt = (
        select(Document)
        .join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
        .where(
            Document.id == doc_id,
            KnowledgeBase.owner_id == user_id,
        )
    )
    res = await db.execute(stmt)
    doc = res.scalar_one_or_none()
    if not doc:
        raise DocumentNotFoundError(doc_id)
    return doc


@router.get("/{doc_id}", response_model=DocumentResponse)
async def get_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    doc = await _get_user_document_or_404(doc_id, db, user_id)
    return DocumentResponse.model_validate(doc)


@router.get("/{doc_id}/chunks", response_model=ChunkPageResponse)
async def get_document_chunks(
    doc_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    doc = await _get_user_document_or_404(doc_id, db, user_id)

    total_stmt = select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == doc_id)
    total = (await db.execute(total_stmt)).scalar() or 0

    offset = (page - 1) * page_size
    chunks_stmt = (
        select(DocumentChunk)
        .where(DocumentChunk.document_id == doc_id)
        .order_by(DocumentChunk.chunk_index.asc())
        .offset(offset)
        .limit(page_size)
    )
    res = await db.execute(chunks_stmt)
    chunks = res.scalars().all()

    items = [ChunkResponse.model_validate(c) for c in chunks]
    return ChunkPageResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    doc = await _get_user_document_or_404(doc_id, db, user_id)

    # 1. Clean up physical files on disk
    try:
        file_path = Path(doc.file_path)
        if file_path.exists():
            file_path.unlink()
            parent_dir = file_path.parent
            if parent_dir.exists() and not any(parent_dir.iterdir()):
                parent_dir.rmdir()
    except Exception as exc:
        logger.warning(f"Error removing file for document {doc_id}: {exc}")

    # 2. Explicitly remove chunks and document using bulk delete
    kb_id = doc.knowledge_base_id
    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
    await db.execute(delete(Document).where(Document.id == doc_id))
    await db.commit()

    # 3. Invalidate BM25 index cache
    try:
        from app.retrieval.bm25_retriever import bm25_index_manager
        bm25_index_manager.invalidate(kb_id)
    except Exception as e:
        logger.warning(f"Failed to invalidate BM25 index on document deletion: {e}")

    return None
