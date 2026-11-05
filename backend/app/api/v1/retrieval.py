import time
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import get_current_user_id
from app.core.config import settings
from app.core.exceptions import KnowledgeBaseNotFoundError
from app.llm.embeddings import get_embedding_client
from app.models.knowledge import KnowledgeBase
from app.retrieval.bm25_retriever import BM25Retriever
from app.retrieval.reranker import CrossEncoderReranker
from app.retrieval.rrf_fusion import HybridRetriever
from app.retrieval.vector_store import VectorRetriever
from app.schemas.chat import (
    RetrievalTestItem,
    RetrievalTestRequest,
    RetrievalTestResponse,
)

router = APIRouter(prefix="/retrieval", tags=["Retrieval"])


@router.post("/test", response_model=RetrievalTestResponse)
async def test_retrieval(
    payload: RetrievalTestRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Independent test endpoint for Dense vector similarity retrieval.
    """
    start_time = time.perf_counter()

    # Check KB
    kb_stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == payload.knowledge_base_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(kb_stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)

    embedding_client = get_embedding_client()
    query_vec = await embedding_client.embed_query(payload.query)

    retriever = VectorRetriever(db)
    scored_chunks = await retriever.search(
        knowledge_base_id=kb.id,
        query_vector=query_vec,
        top_k=payload.top_k,
        threshold=0.0,  # return all top_k for debugging analysis
    )

    duration_ms = (time.perf_counter() - start_time) * 1000

    items = [
        RetrievalTestItem(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            page_number=c.page_number,
            similarity_score=c.similarity_score,
            content=c.content,
            dense_rank=idx,
            dense_score=c.similarity_score,
        )
        for idx, c in enumerate(scored_chunks, start=1)
    ]

    return RetrievalTestResponse(
        query=payload.query,
        knowledge_base_id=kb.id,
        total_retrieved=len(items),
        results=items,
        duration_ms=round(duration_ms, 2),
        dense_duration_ms=round(duration_ms, 2),
        degradation_status="none",
    )


@router.post("/bm25-test", response_model=RetrievalTestResponse)
async def test_bm25_retrieval(
    payload: RetrievalTestRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Independent test endpoint for sparse BM25 retrieval without Dense fusion.
    """
    start_time = time.perf_counter()

    # Check KB
    kb_stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == payload.knowledge_base_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(kb_stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)

    bm25_retriever = BM25Retriever(db)
    scored_chunks = await bm25_retriever.search(
        knowledge_base_id=kb.id,
        query=payload.query,
        top_k=payload.top_k,
    )

    duration_ms = (time.perf_counter() - start_time) * 1000

    items = [
        RetrievalTestItem(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            page_number=c.page_number,
            similarity_score=c.similarity_score,
            content=c.content,
            bm25_rank=idx,
            bm25_score=c.similarity_score,
        )
        for idx, c in enumerate(scored_chunks, start=1)
    ]

    return RetrievalTestResponse(
        query=payload.query,
        knowledge_base_id=kb.id,
        total_retrieved=len(items),
        results=items,
        duration_ms=round(duration_ms, 2),
        bm25_duration_ms=round(duration_ms, 2),
        degradation_status="none",
    )


@router.post("/hybrid-test", response_model=RetrievalTestResponse)
async def test_hybrid_retrieval(
    payload: RetrievalTestRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Independent test endpoint for RRF Hybrid Retrieval combining Dense + BM25.
    """
    # Check KB
    kb_stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == payload.knowledge_base_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(kb_stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)

    hybrid_retriever = HybridRetriever(db)
    result = await hybrid_retriever.search(
        knowledge_base_id=kb.id,
        query=payload.query,
        top_k=payload.top_k,
    )

    items = [
        RetrievalTestItem(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            page_number=c.page_number,
            similarity_score=c.similarity_score,
            content=c.content,
            dense_rank=c.dense_rank,
            bm25_rank=c.bm25_rank,
            dense_score=c.dense_score,
            bm25_score=c.bm25_score,
            fusion_score=c.fusion_score,
        )
        for c in result.chunks
    ]

    return RetrievalTestResponse(
        query=payload.query,
        knowledge_base_id=kb.id,
        total_retrieved=len(items),
        results=items,
        duration_ms=result.total_duration_ms,
        dense_duration_ms=result.dense_duration_ms,
        bm25_duration_ms=result.bm25_duration_ms,
        rrf_duration_ms=result.rrf_duration_ms,
        degradation_status=result.degradation_status,
    )


@router.post("/rerank-test", response_model=RetrievalTestResponse)
async def test_rerank_retrieval(
    payload: RetrievalTestRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Independent test endpoint for Cross-Encoder Reranking after Hybrid RRF candidate generation.
    """
    # Check KB
    kb_stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == payload.knowledge_base_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(kb_stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)

    # 1. Hybrid candidate generation (Top-20 default pool)
    candidate_pool_size = max(payload.top_k, settings.RERANKER_CANDIDATE_POOL_SIZE)
    hybrid_retriever = HybridRetriever(db)
    hybrid_res = await hybrid_retriever.search(
        knowledge_base_id=kb.id,
        query=payload.query,
        top_k=candidate_pool_size,
    )

    if not hybrid_res.chunks:
        return RetrievalTestResponse(
            query=payload.query,
            knowledge_base_id=kb.id,
            total_retrieved=0,
            results=[],
            duration_ms=hybrid_res.total_duration_ms,
            dense_duration_ms=hybrid_res.dense_duration_ms,
            bm25_duration_ms=hybrid_res.bm25_duration_ms,
            rrf_duration_ms=hybrid_res.rrf_duration_ms,
            reranker_duration_ms=0.0,
            reranker_candidates_in=0,
            reranker_candidates_out=0,
            reranker_model=settings.RERANKER_MODEL_NAME,
            reranker_degraded=False,
            degradation_status=hybrid_res.degradation_status,
        )

    # 2. Cross-Encoder rerank
    reranker = CrossEncoderReranker()
    rerank_res = await reranker.rerank(
        query=payload.query,
        candidates=hybrid_res.chunks,
        top_k=payload.top_k,
    )

    total_duration = hybrid_res.total_duration_ms + rerank_res.duration_ms

    items = [
        RetrievalTestItem(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            page_number=c.page_number,
            similarity_score=c.reranker_score if c.reranker_score is not None else c.similarity_score,
            content=c.content,
            dense_rank=c.dense_rank,
            bm25_rank=c.bm25_rank,
            dense_score=c.dense_score,
            bm25_score=c.bm25_score,
            fusion_score=c.fusion_score,
            reranker_score=c.reranker_score,
            reranker_rank=c.reranker_rank,
            reranker_raw_score=c.reranker_raw_score,
        )
        for c in rerank_res.chunks
    ]

    return RetrievalTestResponse(
        query=payload.query,
        knowledge_base_id=kb.id,
        total_retrieved=len(items),
        results=items,
        duration_ms=round(total_duration, 2),
        dense_duration_ms=hybrid_res.dense_duration_ms,
        bm25_duration_ms=hybrid_res.bm25_duration_ms,
        rrf_duration_ms=hybrid_res.rrf_duration_ms,
        reranker_duration_ms=rerank_res.duration_ms,
        reranker_candidates_in=rerank_res.candidates_in,
        reranker_candidates_out=rerank_res.candidates_out,
        reranker_model=rerank_res.model_name,
        reranker_degraded=rerank_res.degraded,
        degradation_status=hybrid_res.degradation_status,
    )
