from dataclasses import dataclass, field
import asyncio
import time
from typing import Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.llm.embeddings import get_embedding_client
from app.retrieval.bm25_retriever import BM25Retriever
from app.retrieval.vector_store import ScoredChunk, VectorRetriever


def rrf_fuse(
    dense_results: List[ScoredChunk],
    bm25_results: List[ScoredChunk],
    k: int = 60,
    dense_weight: float = 1.0,
    bm25_weight: float = 1.0,
    top_k: Optional[int] = None,
) -> List[ScoredChunk]:
    """
    Reciprocal Rank Fusion (RRF) algorithm:
    RRF_score(d) = sum_{m in M} [ w_m / (k + r_m(d)) ]

    Features:
    - 1-based ranks r_m(d) >= 1
    - Smooth fusion constant k (default 60)
    - Independent weight multipliers w_m (default 1.0)
    - Deterministic tie-breaking on score collisions
    - Full metadata preservation from underlying ScoredChunk
    """
    if not dense_results and not bm25_results:
        return []

    # Map chunk_id -> metadata and rankings
    merged_info: Dict[str, Dict] = {}

    # 1. Process Dense candidates
    for rank_idx, chunk in enumerate(dense_results, start=1):
        cid = chunk.chunk_id
        if cid not in merged_info:
            merged_info[cid] = {
                "chunk": chunk,
                "dense_rank": rank_idx,
                "dense_score": chunk.similarity_score,
                "bm25_rank": None,
                "bm25_score": None,
            }
        else:
            merged_info[cid]["dense_rank"] = rank_idx
            merged_info[cid]["dense_score"] = chunk.similarity_score

    # 2. Process BM25 candidates
    for rank_idx, chunk in enumerate(bm25_results, start=1):
        cid = chunk.chunk_id
        if cid not in merged_info:
            merged_info[cid] = {
                "chunk": chunk,
                "dense_rank": None,
                "dense_score": None,
                "bm25_rank": rank_idx,
                "bm25_score": chunk.similarity_score,
            }
        else:
            merged_info[cid]["bm25_rank"] = rank_idx
            merged_info[cid]["bm25_score"] = chunk.similarity_score

    # 3. Calculate RRF Fusion scores
    scored_list = []
    for cid, info in merged_info.items():
        score = 0.0
        dense_rank = info["dense_rank"]
        bm25_rank = info["bm25_rank"]

        if dense_rank is not None:
            score += dense_weight / (k + dense_rank)

        if bm25_rank is not None:
            score += bm25_weight / (k + bm25_rank)

        info["fusion_score"] = score
        scored_list.append(info)

    # 4. Deterministic multi-key sorting:
    # Key 1: fusion_score descending
    # Key 2: both retrievers present (True > False)
    # Key 3: better dense rank (lower is better)
    # Key 4: better dense score (higher is better)
    # Key 5: chunk_id lexicographical order (stable tie-breaker)
    def sort_key(item: Dict):
        fs = item["fusion_score"]
        both_present = 1 if (item["dense_rank"] is not None and item["bm25_rank"] is not None) else 0
        dr = item["dense_rank"] if item["dense_rank"] is not None else 999999
        ds = item["dense_score"] if item["dense_score"] is not None else -1e9
        cid = item["chunk"].chunk_id
        return (-fs, -both_present, dr, -ds, cid)

    scored_list.sort(key=sort_key)

    # 5. Assemble final ScoredChunk list with RRF metadata
    final_chunks: List[ScoredChunk] = []
    for item in scored_list:
        orig = item["chunk"]
        f_score = round(float(item["fusion_score"]), 6)
        f_chunk = ScoredChunk(
            chunk_id=orig.chunk_id,
            document_id=orig.document_id,
            document_name=orig.document_name,
            page_number=orig.page_number,
            chunk_index=orig.chunk_index,
            content=orig.content,
            similarity_score=f_score,
            dense_rank=item["dense_rank"],
            bm25_rank=item["bm25_rank"],
            dense_score=item["dense_score"],
            bm25_score=item["bm25_score"],
            fusion_score=f_score,
        )
        final_chunks.append(f_chunk)

    if top_k is not None:
        return final_chunks[:top_k]
    return final_chunks


@dataclass
class HybridRetrievalResult:
    chunks: List[ScoredChunk]
    dense_chunks: List[ScoredChunk] = field(default_factory=list)
    bm25_chunks: List[ScoredChunk] = field(default_factory=list)
    dense_duration_ms: float = 0.0
    bm25_duration_ms: float = 0.0
    rrf_duration_ms: float = 0.0
    total_duration_ms: float = 0.0
    dense_chunks_count: int = 0
    bm25_chunks_count: int = 0
    deduplicated_chunks_count: int = 0
    degradation_status: str = "none"  # "none", "dense_failed", "bm25_failed", "both_failed"
    query_used: str = ""


class HybridRetriever:
    """
    Orchestrates asynchronous parallel Dense (VectorRetriever) and Sparse (BM25Retriever)
    retrieval, merges them via RRF, and provides automatic fallback degradation.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.vector_retriever = VectorRetriever(db)
        self.bm25_retriever = BM25Retriever(db)
        self.embedding_client = get_embedding_client()

    async def search(
        self,
        knowledge_base_id: str,
        query: str,
        top_k: int = 4,
        k: Optional[int] = None,
        dense_weight: Optional[float] = None,
        bm25_weight: Optional[float] = None,
        candidate_factor: Optional[int] = None,
    ) -> HybridRetrievalResult:
        total_start = time.perf_counter()
        k_val = k if k is not None else settings.RRF_K
        w_dense = dense_weight if dense_weight is not None else settings.RRF_DENSE_WEIGHT
        w_bm25 = bm25_weight if bm25_weight is not None else settings.RRF_BM25_WEIGHT
        factor = candidate_factor if candidate_factor is not None else settings.RRF_CANDIDATE_FACTOR

        candidate_k = max(top_k * factor, top_k + 4)

        dense_chunks: List[ScoredChunk] = []
        bm25_chunks: List[ScoredChunk] = []
        dense_duration_ms: float = 0.0
        bm25_duration_ms: float = 0.0
        dense_error: Optional[Exception] = None
        bm25_error: Optional[Exception] = None

        async def _run_dense() -> Tuple[List[ScoredChunk], float]:
            t0 = time.perf_counter()
            query_vec = await self.embedding_client.embed_query(query)
            res = await self.vector_retriever.search(
                knowledge_base_id=knowledge_base_id,
                query_vector=query_vec,
                top_k=candidate_k,
                threshold=settings.SIMILARITY_THRESHOLD,
            )
            dt = (time.perf_counter() - t0) * 1000
            return res, dt

        async def _run_bm25() -> Tuple[List[ScoredChunk], float]:
            t0 = time.perf_counter()
            res = await self.bm25_retriever.search(
                knowledge_base_id=knowledge_base_id,
                query=query,
                top_k=candidate_k,
            )
            dt = (time.perf_counter() - t0) * 1000
            return res, dt

        # Pre-warm BM25 index on the current session if needed, avoiding concurrent DB queries
        # on the single-threaded aiosqlite connection during asyncio.gather.
        try:
            await self.bm25_retriever.manager.get_or_build_index(knowledge_base_id, self.db)
        except Exception as e:
            bm25_error = e

        if bm25_error:
            # BM25 failed during index prep, run Dense only
            try:
                dense_chunks, dense_duration_ms = await _run_dense()
            except Exception as e:
                dense_error = e
            results = [(dense_chunks, dense_duration_ms) if not dense_error else dense_error, bm25_error]
        else:
            # Both ready: execute in parallel
            results = await asyncio.gather(_run_dense(), _run_bm25(), return_exceptions=True)

        # Parse Dense outcome
        if isinstance(results[0], Exception):
            dense_error = results[0]
            logger.error(f"Dense vector retrieval failed: {dense_error}")
        else:
            dense_chunks, dense_duration_ms = results[0]

        # Parse BM25 outcome
        if isinstance(results[1], Exception):
            bm25_error = results[1]
            logger.error(f"BM25 sparse retrieval failed: {bm25_error}")
        else:
            bm25_chunks, bm25_duration_ms = results[1]

        # Handle exception degradation
        degradation_status = "none"
        if dense_error and bm25_error:
            degradation_status = "both_failed"
            logger.error("Both Dense and BM25 retrievers failed.")
            total_duration_ms = (time.perf_counter() - total_start) * 1000
            return HybridRetrievalResult(
                chunks=[],
                dense_chunks=[],
                bm25_chunks=[],
                dense_duration_ms=round(dense_duration_ms, 2),
                bm25_duration_ms=round(bm25_duration_ms, 2),
                rrf_duration_ms=0.0,
                total_duration_ms=round(total_duration_ms, 2),
                dense_chunks_count=0,
                bm25_chunks_count=0,
                deduplicated_chunks_count=0,
                degradation_status=degradation_status,
                query_used=query,
            )

        if dense_error and not bm25_error:
            degradation_status = "dense_failed"
            logger.warning("Degrading to BM25 Only due to Dense retriever failure.")
            # Format BM25 as final output
            for idx, c in enumerate(bm25_chunks, start=1):
                c.bm25_rank = idx
                c.bm25_score = c.similarity_score
            final_chunks = bm25_chunks[:top_k]
            total_duration_ms = (time.perf_counter() - total_start) * 1000
            return HybridRetrievalResult(
                chunks=final_chunks,
                dense_chunks=[],
                bm25_chunks=bm25_chunks,
                dense_duration_ms=round(dense_duration_ms, 2),
                bm25_duration_ms=round(bm25_duration_ms, 2),
                rrf_duration_ms=0.0,
                total_duration_ms=round(total_duration_ms, 2),
                dense_chunks_count=0,
                bm25_chunks_count=len(bm25_chunks),
                deduplicated_chunks_count=len(bm25_chunks),
                degradation_status=degradation_status,
                query_used=query,
            )

        if bm25_error and not dense_error:
            degradation_status = "bm25_failed"
            logger.warning("Degrading to Dense Only due to BM25 retriever failure.")
            # Format Dense as final output
            for idx, c in enumerate(dense_chunks, start=1):
                c.dense_rank = idx
                c.dense_score = c.similarity_score
            final_chunks = dense_chunks[:top_k]
            total_duration_ms = (time.perf_counter() - total_start) * 1000
            return HybridRetrievalResult(
                chunks=final_chunks,
                dense_chunks=dense_chunks,
                bm25_chunks=[],
                dense_duration_ms=round(dense_duration_ms, 2),
                bm25_duration_ms=round(bm25_duration_ms, 2),
                rrf_duration_ms=0.0,
                total_duration_ms=round(total_duration_ms, 2),
                dense_chunks_count=len(dense_chunks),
                bm25_chunks_count=0,
                deduplicated_chunks_count=len(dense_chunks),
                degradation_status=degradation_status,
                query_used=query,
            )

        # Both succeeded: run RRF fusion
        rrf_start = time.perf_counter()
        unique_cids = {c.chunk_id for c in dense_chunks} | {c.chunk_id for c in bm25_chunks}
        dedup_count = len(unique_cids)

        final_chunks = rrf_fuse(
            dense_results=dense_chunks,
            bm25_results=bm25_chunks,
            k=k_val,
            dense_weight=w_dense,
            bm25_weight=w_bm25,
            top_k=top_k,
        )
        rrf_duration_ms = (time.perf_counter() - rrf_start) * 1000
        total_duration_ms = (time.perf_counter() - total_start) * 1000

        return HybridRetrievalResult(
            chunks=final_chunks,
            dense_chunks=dense_chunks,
            bm25_chunks=bm25_chunks,
            dense_duration_ms=round(dense_duration_ms, 2),
            bm25_duration_ms=round(bm25_duration_ms, 2),
            rrf_duration_ms=round(rrf_duration_ms, 2),
            total_duration_ms=round(total_duration_ms, 2),
            dense_chunks_count=len(dense_chunks),
            bm25_chunks_count=len(bm25_chunks),
            deduplicated_chunks_count=dedup_count,
            degradation_status=degradation_status,
            query_used=query,
        )
