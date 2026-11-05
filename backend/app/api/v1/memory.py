from typing import List, Optional
import uuid
from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import get_current_user_id
from app.core.exceptions import MemoryNotFoundError
from app.memory.extractor import MemoryExtractor
from app.memory.manager import MemoryManager
from app.models.memory import Memory, MemoryExtractionLog
from app.schemas.memory import (
    MemoryCreateRequest,
    MemoryExtractionLogResponse,
    MemoryExtractionTestRequest,
    MemoryListResponse,
    MemoryQueryRequest,
    MemoryQueryResultItem,
    MemoryResponse,
    MemoryStatsResponse,
    MemoryUpdateRequest,
    UserMemorySettingResponse,
    UserMemorySettingUpdateRequest,
)

router = APIRouter(prefix="/memories", tags=["Long-term Memory"])


def _to_memory_response(m: Memory) -> MemoryResponse:
    return MemoryResponse(
        id=m.id,
        owner_id=m.owner_id,
        content=m.content,
        memory_type=m.memory_type,
        tags=m.tags,
        source_type=m.source_type,
        source_conversation_id=m.source_conversation_id,
        source_message_id=m.source_message_id,
        confidence=m.confidence,
        access_count=m.access_count,
        last_accessed_at=m.last_accessed_at,
        is_active=m.is_active,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryCreateRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new persistent long-term memory.
    The owner_id is strictly bound to the authenticated user_id.
    """
    manager = MemoryManager(db)
    mem = await manager.create_memory(
        user_id=user_id,
        content=payload.content,
        memory_type=payload.memory_type,
        tags=payload.tags,
        source_type=payload.source_type or "EXPLICIT_USER",
        source_conversation_id=payload.source_conversation_id,
        source_message_id=payload.source_message_id,
        confidence=payload.confidence if payload.confidence is not None else 1.0,
    )
    return _to_memory_response(mem)


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    memory_type: Optional[str] = Query(None, description="按记忆类型过滤"),
    is_active: Optional[bool] = Query(None, description="按激活状态过滤"),
    search: Optional[str] = Query(None, description="按关键词搜索内容"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    List all memories belonging strictly to the current user.
    """
    manager = MemoryManager(db)
    items, total = await manager.list_memories(
        user_id=user_id,
        memory_type=memory_type,
        is_active=is_active,
        search=search,
        page=page,
        page_size=page_size,
    )
    return MemoryListResponse(
        items=[_to_memory_response(m) for m in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/query", response_model=List[MemoryQueryResultItem])
async def query_memories(
    payload: MemoryQueryRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Vector similarity search across active memories strictly belonging to current user.
    """
    manager = MemoryManager(db)
    results = await manager.retrieve_relevant_memories(
        user_id=user_id,
        query=payload.query,
        top_k=payload.top_k or 5,
        threshold=payload.threshold if payload.threshold is not None else 0.65,
        memory_type=payload.memory_type,
    )
    return [
        MemoryQueryResultItem(
            memory=_to_memory_response(mem),
            similarity_score=round(score, 4),
        )
        for mem, score in results
    ]


@router.get("/stats", response_model=MemoryStatsResponse)
async def get_memory_stats(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get aggregated memory counts and status for current authenticated user.
    """
    manager = MemoryManager(db)
    stats = await manager.get_memory_stats(user_id=user_id)
    return MemoryStatsResponse(**stats)


@router.get("/settings", response_model=UserMemorySettingResponse)
async def get_user_memory_settings(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current user's long-term memory configuration.
    """
    manager = MemoryManager(db)
    setting = await manager.get_user_setting(user_id=user_id)
    return UserMemorySettingResponse(
        owner_id=setting.owner_id,
        auto_memory_enabled=setting.auto_memory_enabled,
        created_at=setting.created_at,
        updated_at=setting.updated_at,
    )


@router.put("/settings", response_model=UserMemorySettingResponse)
async def update_user_memory_settings(
    payload: UserMemorySettingUpdateRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Update current user's long-term memory configuration (e.g. toggle auto-memory).
    """
    manager = MemoryManager(db)
    setting = await manager.update_user_setting(
        user_id=user_id,
        auto_memory_enabled=payload.auto_memory_enabled,
    )
    return UserMemorySettingResponse(
        owner_id=setting.owner_id,
        auto_memory_enabled=setting.auto_memory_enabled,
        created_at=setting.created_at,
        updated_at=setting.updated_at,
    )


@router.get("/logs", response_model=List[MemoryExtractionLogResponse])
async def list_extraction_logs(
    message_id: Optional[str] = Query(None, description="按消息 ID 过滤"),
    conversation_id: Optional[str] = Query(None, description="按会话 ID 过滤"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    List extraction audit logs for current user to track memory extraction status and idempotency.
    """
    stmt = (
        select(MemoryExtractionLog)
        .where(MemoryExtractionLog.owner_id == user_id)
        .order_by(MemoryExtractionLog.created_at.desc())
        .limit(50)
    )
    if message_id:
        stmt = stmt.where(MemoryExtractionLog.message_id == message_id)
    if conversation_id:
        stmt = stmt.where(MemoryExtractionLog.conversation_id == conversation_id)

    rows = (await db.execute(stmt)).scalars().all()
    return [
        MemoryExtractionLogResponse(
            id=r.id,
            message_id=r.message_id,
            conversation_id=r.conversation_id,
            owner_id=r.owner_id,
            status=r.status,
            extracted_count=r.extracted_count,
            reason=r.reason,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/extract", response_model=List[MemoryResponse])
async def trigger_extraction_test(
    payload: MemoryExtractionTestRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually trigger memory extraction for a conversation turn (testing & verification endpoint).
    """
    extractor = MemoryExtractor()
    dummy_msg_id = str(uuid.uuid4())
    memories = await extractor.extract_from_conversation(
        user_id=user_id,
        query=payload.query,
        answer=payload.answer or "",
        conversation_id=payload.conversation_id,
        message_id=dummy_msg_id,
        db=db,
    )
    return [_to_memory_response(m) for m in memories]


@router.get("/{memory_id}", response_model=MemoryResponse)
async def get_memory_detail(
    memory_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get single memory detail. Returns 404 if memory doesn't exist or doesn't belong to current user.
    """
    manager = MemoryManager(db)
    mem = await manager.get_memory(user_id=user_id, memory_id=memory_id)
    if not mem:
        raise MemoryNotFoundError(memory_id)
    return _to_memory_response(mem)


@router.put("/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: str,
    payload: MemoryUpdateRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Update memory content, type, tags, or status.
    """
    manager = MemoryManager(db)
    mem = await manager.update_memory(
        user_id=user_id,
        memory_id=memory_id,
        content=payload.content,
        memory_type=payload.memory_type,
        tags=payload.tags,
        is_active=payload.is_active,
        confidence=payload.confidence,
    )
    return _to_memory_response(mem)


@router.patch("/{memory_id}/status", response_model=MemoryResponse)
async def toggle_memory_status(
    memory_id: str,
    is_active: bool = Query(..., description="启用/停用状态"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Toggle active status of a memory item.
    """
    manager = MemoryManager(db)
    mem = await manager.toggle_status(
        user_id=user_id,
        memory_id=memory_id,
        is_active=is_active,
    )
    return _to_memory_response(mem)


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Physically delete a memory. Deleted memories can never be recalled again.
    """
    manager = MemoryManager(db)
    await manager.delete_memory(user_id=user_id, memory_id=memory_id)
    return {"deleted": True, "id": memory_id}


@router.delete("")
async def clear_all_memories(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Clear all memories belonging exclusively to current user.
    """
    manager = MemoryManager(db)
    deleted_count = await manager.clear_memories(user_id=user_id)
    return {"deleted_count": deleted_count}
