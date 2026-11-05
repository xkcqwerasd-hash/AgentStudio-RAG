import asyncio
import json
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import get_current_user_id
from app.core.exceptions import (
    ConversationNotFoundError,
    KnowledgeBaseNotFoundError,
    VectorModelMismatchError,
)
from app.llm.embeddings import get_embedding_client
from app.llm.factory import resolve_llm_client
from app.memory.extractor import run_memory_extraction_task
from app.models.conversation import Conversation, ConversationAttachment, Message
from app.models.knowledge import KnowledgeBase
from app.rag.context_budget import ContextBudgetManager
from app.rag.workflow import WorkflowRAGEngine
from app.routing import DirectChatEngine, ExecutionPath, get_intent_router
from app.services.attachment_storage import delete_conversation_physical_attachments
from app.schemas.chat import ChatRequest, ChatResponse, Citation
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetailResponse,
    ConversationResponse,
    MessageResponse,
)

router = APIRouter(tags=["Chat"])


async def _inspect_routing_context(
    payload: ChatRequest,
    db: AsyncSession,
    user_id: str,
) -> tuple[Optional[KnowledgeBase], bool, List[Dict[str, str]]]:
    """Helper to check KB existence, attachment presence, and history for routing, strictly scoped to user_id."""
    kb = None
    if payload.knowledge_base_id:
        kb_stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == payload.knowledge_base_id,
            KnowledgeBase.owner_id == user_id,
        )
        kb = (await db.execute(kb_stmt)).scalar_one_or_none()
        if not kb:
            raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)

    has_attachments = False
    history_messages: List[Dict[str, str]] = []

    if payload.conversation_id:
        conv_stmt = select(Conversation).where(
            Conversation.id == payload.conversation_id,
            Conversation.owner_id == user_id,
        )
        conv = (await db.execute(conv_stmt)).scalar_one_or_none()
        if not conv:
            raise ConversationNotFoundError(payload.conversation_id)

        att_stmt = select(func.count(ConversationAttachment.id)).where(
            ConversationAttachment.conversation_id == payload.conversation_id,
            ConversationAttachment.parse_status == "COMPLETED",
        )
        has_attachments = ((await db.execute(att_stmt)).scalar() or 0) > 0

        hist_stmt = (
            select(Message)
            .where(Message.conversation_id == payload.conversation_id)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        raw_h = list(reversed((await db.execute(hist_stmt)).scalars().all()))
        for m in raw_h:
            if m.role in ("user", "assistant"):
                cleaned = m.content.replace("\n\n[回答已被用户中止]", "").replace("[回答已被用户中止]", "").strip()
                if cleaned:
                    history_messages.append({"role": m.role, "content": cleaned})

    return kb, has_attachments, history_messages


@router.post("/chat/completions", response_model=ChatResponse)
async def chat_completions(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Execute chat with intelligent intent routing (Direct LLM vs Workflow RAG).
    """
    kb, has_attachments, history_messages = await _inspect_routing_context(payload, db, user_id=user_id)

    decision = get_intent_router().route(
        query=payload.query,
        has_knowledge_base=bool(kb),
        has_completed_attachments=has_attachments,
        history_messages=history_messages,
        requested_mode=payload.mode or "workflow",
    )

    llm_client, model_cfg = await resolve_llm_client(db, payload.model_id, user_id=user_id)

    if decision.execution_path == ExecutionPath.DIRECT_LLM:
        direct_engine = DirectChatEngine(db, llm_client=llm_client, llm_model=model_cfg.model_name)
        res = await direct_engine.execute(payload, decision, user_id=user_id)
        if getattr(res, "message_id", None):
            asyncio.create_task(
                run_memory_extraction_task(
                    user_id=user_id,
                    query=payload.query,
                    answer=res.answer,
                    conversation_id=res.conversation_id,
                    message_id=res.message_id,
                )
            )
        return res

    # RAG Workflow path
    if kb:
        embedding_client = get_embedding_client()
        active_model = embedding_client.model_name
        active_dim = embedding_client.dimensions
        active_provider = embedding_client.provider_name
        kb_provider = getattr(kb, "embedding_provider", None) or "local_bge"
        kb_model = getattr(kb, "embedding_model", None) or "BAAI/bge-small-zh-v1.5"
        kb_dim = getattr(kb, "embedding_dimensions", None) or 512
        if active_model != kb_model or active_dim != kb_dim:
            raise VectorModelMismatchError(
                current_model=f"{active_model} ({active_dim}d, {active_provider})",
                index_model=f"{kb_model} ({kb_dim}d, {kb_provider})",
                kb_id=kb.id,
            )

    ContextBudgetManager().validate_user_query(payload.query)
    engine = WorkflowRAGEngine(db, llm_client=llm_client, llm_model=model_cfg.model_name)
    res = await engine.execute(payload, decision, user_id=user_id)
    if getattr(res, "message_id", None):
        asyncio.create_task(
            run_memory_extraction_task(
                user_id=user_id,
                query=payload.query,
                answer=res.answer,
                conversation_id=res.conversation_id,
                message_id=res.message_id,
            )
        )
    return res


@router.post("/chat/completions/stream")
async def chat_completions_stream(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Execute chat completions with intelligent intent routing and real-time SSE streaming.
    """
    kb, has_attachments, history_messages = await _inspect_routing_context(payload, db, user_id=user_id)

    decision = get_intent_router().route(
        query=payload.query,
        has_knowledge_base=bool(kb),
        has_completed_attachments=has_attachments,
        history_messages=history_messages,
        requested_mode=payload.mode or "workflow",
    )

    llm_client, model_cfg = await resolve_llm_client(db, payload.model_id, user_id=user_id)

    if decision.execution_path == ExecutionPath.DIRECT_LLM:
        direct_engine = DirectChatEngine(db, llm_client=llm_client, llm_model=model_cfg.model_name)

        async def sse_direct_stream():
            try:
                async for item in direct_engine.execute_stream(payload, decision, user_id=user_id):
                    event_name = item.get("event", "message")
                    data_json = json.dumps(item.get("data", {}), ensure_ascii=False)
                    yield f"event: {event_name}\ndata: {data_json}\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err_data = json.dumps({"detail": str(exc)}, ensure_ascii=False)
                yield f"event: error\ndata: {err_data}\n\n"

        return StreamingResponse(
            sse_direct_stream(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # RAG Workflow path
    if kb:
        embedding_client = get_embedding_client()
        active_model = embedding_client.model_name
        active_dim = embedding_client.dimensions
        active_provider = embedding_client.provider_name
        kb_provider = getattr(kb, "embedding_provider", None) or "local_bge"
        kb_model = getattr(kb, "embedding_model", None) or "BAAI/bge-small-zh-v1.5"
        kb_dim = getattr(kb, "embedding_dimensions", None) or 512
        if active_model != kb_model or active_dim != kb_dim:
            raise VectorModelMismatchError(
                current_model=f"{active_model} ({active_dim}d, {active_provider})",
                index_model=f"{kb_model} ({kb_dim}d, {kb_provider})",
                kb_id=kb.id,
            )

    ContextBudgetManager().validate_user_query(payload.query)
    engine = WorkflowRAGEngine(db, llm_client=llm_client, llm_model=model_cfg.model_name)

    async def sse_event_stream():
        try:
            async for item in engine.execute_stream(payload, decision, user_id=user_id):
                event_name = item.get("event", "message")
                data_json = json.dumps(item.get("data", {}), ensure_ascii=False)
                yield f"event: {event_name}\ndata: {data_json}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err_data = json.dumps({"detail": str(exc)}, ensure_ascii=False)
            yield f"event: error\ndata: {err_data}\n\n"

    return StreamingResponse(
        sse_event_stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Create a new conversation optionally bound to a specific knowledge base, owned by current user.
    """
    if payload.knowledge_base_id:
        kb_stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == payload.knowledge_base_id,
            KnowledgeBase.owner_id == user_id,
        )
        kb = (await db.execute(kb_stmt)).scalar_one_or_none()
        if not kb:
            raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)

    conv = Conversation(
        owner_id=user_id,
        knowledge_base_id=payload.knowledge_base_id,
        title=payload.title or "新建对话",
        mode=payload.mode or "workflow",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    return ConversationResponse(
        id=conv.id,
        knowledge_base_id=conv.knowledge_base_id,
        title=conv.title,
        mode=conv.mode,
        message_count=0,
        last_message_preview=None,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.get("/conversations", response_model=List[ConversationResponse])
async def list_conversations(
    knowledge_base_id: Optional[str] = Query(None, description="按知识库ID过滤会话"),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    List conversations owned by current user, ordered by updated_at descending.
    """
    stmt = (
        select(Conversation)
        .where(Conversation.owner_id == user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    if knowledge_base_id:
        stmt = stmt.where(Conversation.knowledge_base_id == knowledge_base_id)

    res = await db.execute(stmt)
    convs = res.scalars().all()

    items: List[ConversationResponse] = []
    for c in convs:
        count_stmt = select(func.count(Message.id)).where(Message.conversation_id == c.id)
        msg_count = (await db.execute(count_stmt)).scalar() or 0

        last_msg_stmt = (
            select(Message.content)
            .where(Message.conversation_id == c.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        last_content = (await db.execute(last_msg_stmt)).scalar_one_or_none()
        preview = (last_content[:60] + "...") if last_content and len(last_content) > 60 else last_content

        items.append(
            ConversationResponse(
                id=c.id,
                knowledge_base_id=c.knowledge_base_id,
                title=c.title,
                mode=c.mode,
                message_count=msg_count,
                last_message_preview=preview,
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
        )
    return items


@router.get("/conversations/{conv_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Get conversation details and full message history for the authenticated owner.
    """
    stmt = select(Conversation).where(
        Conversation.id == conv_id,
        Conversation.owner_id == user_id,
    )
    conv = (await db.execute(stmt)).scalar_one_or_none()
    if not conv:
        raise ConversationNotFoundError(conv_id)

    msg_stmt = (
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
    )
    msgs = (await db.execute(msg_stmt)).scalars().all()

    msg_responses = [
        MessageResponse(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            citations=[Citation(**c) for c in m.citations],
            execution_info=m.execution_info,
            created_at=m.created_at,
        )
        for m in msgs
    ]

    last_content = msgs[-1].content if msgs else None
    preview = (last_content[:60] + "...") if last_content and len(last_content) > 60 else last_content

    return ConversationDetailResponse(
        id=conv.id,
        knowledge_base_id=conv.knowledge_base_id,
        title=conv.title,
        mode=conv.mode,
        message_count=len(msgs),
        last_message_preview=preview,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=msg_responses,
    )


@router.get("/conversations/{conv_id}/messages", response_model=List[MessageResponse])
async def get_conversation_messages(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Get messages for a conversation owned by current user.
    """
    conv_stmt = select(Conversation).where(
        Conversation.id == conv_id,
        Conversation.owner_id == user_id,
    )
    conv = (await db.execute(conv_stmt)).scalar_one_or_none()
    if not conv:
        raise ConversationNotFoundError(conv_id)

    stmt = (
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
    )
    res = await db.execute(stmt)
    messages = res.scalars().all()
    return [
        MessageResponse(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            citations=[Citation(**c) for c in m.citations],
            execution_info=m.execution_info,
            created_at=m.created_at,
        )
        for m in messages
    ]


@router.delete("/conversations/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Delete a conversation and all its messages, scoped to owner.
    """
    stmt = select(Conversation).where(
        Conversation.id == conv_id,
        Conversation.owner_id == user_id,
    )
    res = await db.execute(stmt)
    conv = res.scalar_one_or_none()
    if not conv:
        raise ConversationNotFoundError(conv_id)

    await db.delete(conv)
    await db.commit()
    delete_conversation_physical_attachments(conv_id, owner_id=user_id)
    from app.attachments import attachment_retrieval_service
    attachment_retrieval_service.invalidate_conversation(conv_id)
    return None
