import time
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import KnowledgeBaseNotFoundError
from app.core.logging import logger
from app.llm.embeddings import get_embedding_client, validate_vector
from app.models.chunk import DocumentChunk
from app.models.knowledge import KnowledgeBase
from app.schemas.knowledge import ReindexResponse


class ReindexService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def reindex_knowledge_base(
        self,
        kb_id: str,
        target_provider: Optional[str] = None,
        target_model: Optional[str] = None,
        batch_size: Optional[int] = None,
    ) -> ReindexResponse:
        """
        Recompute embeddings for all chunks in a knowledge base with strict atomicity:
        1. Fetch all existing chunks without re-parsing documents.
        2. Compute and validate all embeddings in memory.
        3. If any chunk fails or raises an error, abort immediately and rollback transaction.
        4. Commit new embeddings and updated index metadata only when all chunks succeed.
        """
        start_time = time.perf_counter()

        # 1. Fetch knowledge base
        kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        kb_res = await self.db.execute(kb_stmt)
        kb = kb_res.scalar_one_or_none()
        if not kb:
            raise KnowledgeBaseNotFoundError(kb_id)

        prev_provider = kb.embedding_provider or "test_double"
        prev_model = kb.embedding_model or "hash-projection-v1"
        prev_dim = kb.embedding_dimensions or 1024

        # 2. Initialize target embedding client
        provider_to_use = target_provider or settings.EMBEDDING_PROVIDER
        model_to_use = target_model or settings.EMBEDDING_MODEL_NAME
        embedding_client = get_embedding_client(
            provider=provider_to_use,
            model_name=model_to_use,
        )

        target_provider_name = embedding_client.provider_name
        target_model_name = embedding_client.model_name
        target_dimensions = embedding_client.dimensions

        # 3. Fetch all DocumentChunk records
        chunks_stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.knowledge_base_id == kb_id)
            .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
        )
        chunks_res = await self.db.execute(chunks_stmt)
        chunks = chunks_res.scalars().all()

        if not chunks:
            # No chunks to reindex; just update KB metadata and version
            kb.embedding_provider = target_provider_name
            kb.embedding_model = target_model_name
            kb.embedding_dimensions = target_dimensions
            kb.index_version += 1
            await self.db.commit()
            duration_ms = (time.perf_counter() - start_time) * 1000
            return ReindexResponse(
                knowledge_base_id=kb_id,
                reindexed_chunks=0,
                duration_ms=round(duration_ms, 2),
                previous_provider=prev_provider,
                previous_model=prev_model,
                previous_dimensions=prev_dim,
                new_provider=target_provider_name,
                new_model=target_model_name,
                new_dimensions=target_dimensions,
                new_index_version=kb.index_version,
                status="COMPLETED",
            )

        logger.info(
            f"[Reindex] Starting re-indexing for KB '{kb.name}' ({kb_id}): "
            f"{len(chunks)} chunks from {prev_provider}/{prev_model} ({prev_dim}d) "
            f"to {target_provider_name}/{target_model_name} ({target_dimensions}d)..."
        )

        effective_batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        all_new_vectors: List[List[float]] = []

        # 4. Compute all new embeddings in staging memory
        for i in range(0, len(chunks), effective_batch_size):
            batch_chunks = chunks[i : i + effective_batch_size]
            batch_texts = [c.content for c in batch_chunks]
            batch_vectors = await embedding_client.embed_documents(batch_texts)

            # Validate each vector before appending
            for vec in batch_vectors:
                validate_vector(
                    vec=vec,
                    expected_dim=target_dimensions,
                    provider_name=target_provider_name,
                    model_name=target_model_name,
                )
            all_new_vectors.extend(batch_vectors)

        assert len(all_new_vectors) == len(chunks), (
            f"Computed vector count ({len(all_new_vectors)}) does not match chunk count ({len(chunks)})"
        )

        # 5. Atomic database update
        for chunk, vec in zip(chunks, all_new_vectors):
            chunk.embedding = vec

        kb.embedding_provider = target_provider_name
        kb.embedding_model = target_model_name
        kb.embedding_dimensions = target_dimensions
        kb.index_version += 1

        await self.db.commit()

        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            f"[Reindex] Successfully reindexed {len(chunks)} chunks for KB {kb_id} "
            f"in {duration_ms:.1f}ms. Index version bumped to {kb.index_version}."
        )

        return ReindexResponse(
            knowledge_base_id=kb_id,
            reindexed_chunks=len(chunks),
            duration_ms=round(duration_ms, 2),
            previous_provider=prev_provider,
            previous_model=prev_model,
            previous_dimensions=prev_dim,
            new_provider=target_provider_name,
            new_model=target_model_name,
            new_dimensions=target_dimensions,
            new_index_version=kb.index_version,
            status="COMPLETED",
        )
