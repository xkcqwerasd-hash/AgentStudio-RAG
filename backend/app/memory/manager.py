from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import ConversationNotFoundError, MemoryNotFoundError
from app.core.logging import logger
from app.llm.base import BaseEmbeddingClient
from app.llm.embeddings import get_embedding_client
from app.models.conversation import Conversation
from app.models.memory import Memory, MemorySourceType, MemoryType, UserMemorySetting


class MemoryManager:
    """
    Core Long-Term Memory Service for AgentStudio-RAG (P1.2 Stage 2).

    Key architectural responsibilities:
    1. Multi-tenant isolation: All read, write, query, and delete operations are strictly partitioned by owner_id.
    2. Vector recall: Reuses existing Embedding Client and NumPy for sub-millisecond cosine similarity search.
    3. Lifecycle management: Supports create, update, delete, deactivate/activate, and exact-fact deduplication.
    4. Safe deduplication: Avoids blind semantic overwrites; identical content refreshes access & confidence.
    5. Fault tolerance: Graceful degradation if embedding generation fails.
    """

    def __init__(
        self,
        db: AsyncSession,
        embedding_client: Optional[BaseEmbeddingClient] = None,
    ):
        self.db = db
        self.embedding_client = embedding_client or get_embedding_client()

    async def create_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str = MemoryType.IMPORTANT_FACT,
        tags: Optional[List[str]] = None,
        source_type: str = MemorySourceType.EXPLICIT_USER,
        source_conversation_id: Optional[str] = None,
        source_message_id: Optional[str] = None,
        confidence: float = 1.0,
    ) -> Memory:
        """
        Create and persist a new long-term memory for the specified owner_id.
        Performs exact-content deduplication to avoid accumulating verbatim copies.
        """
        cleaned_content = content.strip()
        if not cleaned_content:
            raise ValueError("Memory content cannot be empty.")

        # 1. Validate conversation source if provided
        if source_conversation_id:
            conv = await self.db.get(Conversation, source_conversation_id)
            if not conv:
                raise ConversationNotFoundError(source_conversation_id)
            # Future P1.3 check: if conv has owner_id, enforce conv.owner_id == user_id
            if hasattr(conv, "owner_id") and conv.owner_id and conv.owner_id != user_id:
                raise ConversationNotFoundError(source_conversation_id)

        # 2. Exact content deduplication check (same owner, active, exact match)
        # Note: Semantic similarity is NOT contradiction; we only deduplicate verbatim facts.
        exact_stmt = select(Memory).where(
            Memory.owner_id == user_id,
            Memory.is_active == True,
            func.lower(Memory.content) == cleaned_content.lower(),
        )
        existing = (await self.db.execute(exact_stmt)).scalar_one_or_none()
        if existing:
            existing.confidence = max(existing.confidence, confidence)
            existing.access_count += 1
            existing.updated_at = utc_now()
            if tags:
                merged_tags = list(set(existing.tags + tags))
                existing.tags = merged_tags
            await self.db.commit()
            await self.db.refresh(existing)
            logger.info(f"Deduplicated existing memory {existing.id} for user {user_id}")
            return existing

        # 3. Generate embedding vector
        vec = None
        try:
            vec = await self.embedding_client.embed_query(cleaned_content)
        except Exception as exc:
            logger.warning(
                f"Embedding generation failed for memory creation: {exc}. Persisting memory without vector."
            )

        # 4. Instantiate and commit new Memory
        mem = Memory(
            owner_id=user_id,
            content=cleaned_content,
            memory_type=memory_type,
            tags=tags or [],
            source_type=source_type,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            confidence=confidence,
            is_active=True,
            access_count=0,
        )
        if vec is not None:
            mem.embedding = vec

        self.db.add(mem)
        await self.db.commit()
        await self.db.refresh(mem)
        logger.info(f"Created new memory {mem.id} (type: {memory_type}) for user {user_id}")
        return mem

    async def get_memory(self, user_id: str, memory_id: str) -> Optional[Memory]:
        """
        Get a specific memory item by ID, strictly constrained to owner_id.
        """
        stmt = select(Memory).where(
            Memory.id == memory_id,
            Memory.owner_id == user_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_memories(
        self,
        user_id: str,
        memory_type: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Memory], int]:
        """
        List memories belonging to user_id with optional type, status, and keyword filtering.
        """
        stmt = select(Memory).where(Memory.owner_id == user_id)

        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if is_active is not None:
            stmt = stmt.where(Memory.is_active == is_active)
        if search and search.strip():
            stmt = stmt.where(Memory.content.ilike(f"%{search.strip()}%"))

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.db.execute(count_stmt)).scalar() or 0

        p = max(1, page)
        ps = max(1, min(100, page_size))
        stmt = (
            stmt.order_by(Memory.created_at.desc())
            .offset((p - 1) * ps)
            .limit(ps)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return list(rows), total

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        content: Optional[str] = None,
        memory_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_active: Optional[bool] = None,
        confidence: Optional[float] = None,
    ) -> Memory:
        """
        Update memory attributes. Re-embeds content if content changed.
        Strictly prevents cross-user modification.
        """
        mem = await self.get_memory(user_id, memory_id)
        if not mem:
            raise MemoryNotFoundError(memory_id)

        if content is not None:
            cleaned = content.strip()
            if cleaned and cleaned != mem.content:
                mem.content = cleaned
                try:
                    mem.embedding = await self.embedding_client.embed_query(cleaned)
                except Exception as exc:
                    logger.warning(f"Failed to re-embed updated memory {memory_id}: {exc}")

        if memory_type is not None:
            mem.memory_type = memory_type
        if tags is not None:
            mem.tags = tags
        if is_active is not None:
            mem.is_active = is_active
        if confidence is not None:
            mem.confidence = confidence

        mem.updated_at = utc_now()
        await self.db.commit()
        await self.db.refresh(mem)
        logger.info(f"Updated memory {memory_id} for user {user_id}")
        return mem

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        """
        Physically delete a memory belonging to user_id.
        Deleted memories are completely purged and can never be recalled again.
        """
        mem = await self.get_memory(user_id, memory_id)
        if not mem:
            raise MemoryNotFoundError(memory_id)

        await self.db.delete(mem)
        await self.db.commit()
        logger.info(f"Deleted memory {memory_id} for user {user_id}")
        return True

    async def toggle_status(self, user_id: str, memory_id: str, is_active: bool) -> Memory:
        """
        Toggle active state of a memory. Inactive memories are excluded from vector recall.
        """
        mem = await self.get_memory(user_id, memory_id)
        if not mem:
            raise MemoryNotFoundError(memory_id)

        mem.is_active = is_active
        mem.updated_at = utc_now()
        await self.db.commit()
        await self.db.refresh(mem)
        logger.info(f"Toggled memory {memory_id} active status to {is_active} for user {user_id}")
        return mem

    async def clear_memories(self, user_id: str) -> int:
        """
        Clear all memories belonging exclusively to user_id.
        """
        stmt = select(Memory).where(Memory.owner_id == user_id)
        rows = (await self.db.execute(stmt)).scalars().all()
        count = len(rows)
        for r in rows:
            await self.db.delete(r)
        await self.db.commit()
        logger.info(f"Cleared all {count} memories for user {user_id}")
        return count

    async def get_user_setting(self, user_id: str) -> UserMemorySetting:
        """
        Get or initialize user memory settings.
        Partitions settings strictly by owner_id.
        """
        stmt = select(UserMemorySetting).where(UserMemorySetting.owner_id == user_id)
        setting = (await self.db.execute(stmt)).scalar_one_or_none()
        if not setting:
            setting = UserMemorySetting(
                owner_id=user_id,
                auto_memory_enabled=True,
            )
            self.db.add(setting)
            await self.db.commit()
            await self.db.refresh(setting)
        return setting

    async def update_user_setting(
        self, user_id: str, auto_memory_enabled: bool
    ) -> UserMemorySetting:
        """
        Update user memory settings (e.g. enable or disable auto-extraction).
        """
        setting = await self.get_user_setting(user_id)
        setting.auto_memory_enabled = auto_memory_enabled
        setting.updated_at = utc_now()
        await self.db.commit()
        await self.db.refresh(setting)
        logger.info(
            f"Updated user memory setting for {user_id}: auto_memory_enabled={auto_memory_enabled}"
        )
        return setting

    async def get_memory_stats(self, user_id: str) -> Dict[str, Any]:
        """
        Get aggregated memory statistics for a user.
        """
        setting = await self.get_user_setting(user_id)
        stmt = select(Memory).where(Memory.owner_id == user_id)
        rows = (await self.db.execute(stmt)).scalars().all()

        total = len(rows)
        active = sum(1 for m in rows if m.is_active)
        profile_cnt = sum(1 for m in rows if m.memory_type == MemoryType.USER_PROFILE)
        bg_cnt = sum(1 for m in rows if m.memory_type == MemoryType.PROJECT_CONTEXT)
        fact_cnt = sum(1 for m in rows if m.memory_type == MemoryType.IMPORTANT_FACT)
        rule_cnt = sum(1 for m in rows if m.memory_type == MemoryType.DIRECT_RULE)

        return {
            "total_memories": total,
            "active_memories": active,
            "profile_count": profile_cnt,
            "background_count": bg_cnt,
            "fact_count": fact_cnt,
            "rule_count": rule_cnt,
            "auto_memory_enabled": setting.auto_memory_enabled,
        }

    async def supersede_memory(
        self,
        user_id: str,
        target_memory_id: str,
        new_content: str,
        confidence: Optional[float] = None,
    ) -> Memory:
        """
        Explicitly supersede an existing memory with new content and update its vector embedding.
        """
        return await self.update_memory(
            user_id=user_id,
            memory_id=target_memory_id,
            content=new_content,
            confidence=confidence,
        )

    async def retrieve_relevant_memories(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        threshold: Optional[float] = None,
        memory_type: Optional[str] = None,
    ) -> List[Tuple[Memory, float]]:
        """
        Perform dense semantic vector search across active memories strictly belonging to user_id.

        Guarantees:
        1. Multi-tenant isolation: owner_id == user_id.
        2. Status check: is_active == True (deleted or deactivated items are NEVER recalled).
        3. Graceful degradation: If embedding fails, logs warning and returns empty list without crashing.
        4. Cosine similarity: Computes dot product over normalized vectors via NumPy.
        5. Access tracking: Increments access_count and updates last_accessed_at for recalled items.
        """
        # Strict user-isolated and active-only filter
        stmt = select(Memory).where(
            Memory.owner_id == user_id,
            Memory.is_active == True,
        )
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)

        rows = (await self.db.execute(stmt)).scalars().all()
        if not rows:
            return []

        # Generate query vector
        try:
            q_vec_list = await self.embedding_client.embed_query(query)
        except Exception as exc:
            logger.warning(
                f"Embedding service failed during memory recall for user {user_id}: {exc}. "
                "Gracefully returning empty recall results."
            )
            return []

        if not q_vec_list:
            return []

        q_vec = np.array(q_vec_list, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm < 1e-9:
            return []

        effective_threshold = (
            threshold if threshold is not None else settings.MEMORY_SIMILARITY_THRESHOLD
        )
        scored: List[Tuple[Memory, float]] = []
        for mem in rows:
            m_vec_list = mem.embedding
            if not m_vec_list:
                continue

            m_vec = np.array(m_vec_list, dtype=np.float32)
            if m_vec.shape != q_vec.shape:
                continue

            m_norm = np.linalg.norm(m_vec)
            if m_norm < 1e-9:
                continue

            cos_sim = float(np.dot(q_vec, m_vec) / (q_norm * m_norm))
            if cos_sim >= effective_threshold:
                scored.append((mem, cos_sim))

        # Sort descending by similarity score
        scored.sort(key=lambda item: item[1], reverse=True)
        top_items = scored[:top_k]

        # Update access count & timestamp for returned items
        if top_items:
            now_dt = utc_now()
            for m, _ in top_items:
                m.access_count += 1
                m.last_accessed_at = now_dt
            await self.db.flush()

        return top_items
