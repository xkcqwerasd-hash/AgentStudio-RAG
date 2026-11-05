from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import BaseTool
from app.agent.tools.evidence import EvidenceItem, EvidenceRegistry
from app.core.config import settings
from app.core.exceptions import KnowledgeBaseNotFoundError
from app.core.logging import logger
from app.db.session import async_session_factory
from app.models.chunk import DocumentChunk
from app.models.knowledge import KnowledgeBase
from app.retrieval.reranker import CrossEncoderReranker
from app.retrieval.rrf_fusion import HybridRetriever
from app.retrieval.vector_store import ScoredChunk


class SearchKnowledgeBaseInput(BaseModel):
    """Input argument schema for search_knowledge_base tool."""
    knowledge_base_id: str = Field(
        ...,
        min_length=1,
        description="目标知识库唯一UUID标识符，用于严格限定检索范围，防止跨库越权数据泄露",
    )
    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="用于在知识库中进行技术事实检索的精准问题或关键词",
    )
    top_k: int = Field(
        default=4,
        ge=1,
        le=20,
        description="期望返回的高相关技术证据分块数量(默认4，建议4~8，上限20)",
    )


class SearchKnowledgeBaseTool(BaseTool):
    """
    SearchKnowledgeBaseTool encapsulates the enterprise-grade RAG retrieval engine (M1~M3.3)
    into a standardized, LangGraph-ready tool.
    Pipeline:
    1. Knowledge Base existence and isolation check.
    2. Parallel Dense (BGE Embedding) + Sparse (BM25) search.
    3. Reciprocal Rank Fusion (RRF).
    4. Cross-Encoder Reranker (CPU INT8 ONNX) high-precision re-ranking.
    5. Run-scoped Evidence Registry global reference tagging ([Ref-X]).
    """

    def __init__(
        self,
        db: Optional[AsyncSession] = None,
        evidence_registry: Optional[EvidenceRegistry] = None,
        reranker: Optional[CrossEncoderReranker] = None,
        user_id: Optional[str] = None,
    ):
        self.db = db
        self.evidence_registry = evidence_registry
        self.reranker = reranker or CrossEncoderReranker()
        self.user_id = user_id

    @property
    def name(self) -> str:
        return "search_knowledge_base"

    @property
    def description(self) -> str:
        return (
            "在指定企业知识库中进行深度语义向量与关键词稀疏混合检索，"
            "并通过 Cross-Encoder 精细重排输出高相关的真实技术文档证据分块。"
            "当用户提问涉及技术规范、系统配置、架构设计或领域专业事实时调用此工具。"
        )

    @property
    def args_schema(self) -> Type[BaseModel]:
        return SearchKnowledgeBaseInput

    def with_evidence_registry(self, registry: EvidenceRegistry) -> "SearchKnowledgeBaseTool":
        """Attach a Run-scoped EvidenceRegistry instance for global [Ref-X] tracking."""
        self.evidence_registry = registry
        return self

    def with_session(self, db: AsyncSession) -> "SearchKnowledgeBaseTool":
        """Attach an existing database session."""
        self.db = db
        return self

    def with_user_id(self, user_id: str) -> "SearchKnowledgeBaseTool":
        """Attach a user identity to ensure knowledge base isolation."""
        self.user_id = user_id
        return self

    async def _run(
        self,
        knowledge_base_id: str,
        query: str,
        top_k: int = 4,
    ) -> Dict[str, Any]:
        """Execute knowledge search using existing internal services."""
        if self.db is not None:
            return await self._execute_search(self.db, knowledge_base_id, query, top_k)
        else:
            async with async_session_factory() as session:
                return await self._execute_search(session, knowledge_base_id, query, top_k)

    async def _execute_search(
        self,
        session: AsyncSession,
        knowledge_base_id: str,
        query: str,
        top_k: int,
    ) -> Dict[str, Any]:
        # 1. Verify KnowledgeBase existence and tenant ownership
        kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id)
        if self.user_id is not None:
            kb_stmt = kb_stmt.where(KnowledgeBase.owner_id == self.user_id)
        kb_res = await session.execute(kb_stmt)
        kb = kb_res.scalar_one_or_none()
        if not kb:
            raise KnowledgeBaseNotFoundError(knowledge_base_id)

        # 2. Check if KB contains indexed chunks
        count_stmt = select(func.count(DocumentChunk.id)).where(
            DocumentChunk.knowledge_base_id == knowledge_base_id
        )
        total_chunks = (await session.execute(count_stmt)).scalar() or 0
        if total_chunks == 0:
            return {
                "query": query,
                "knowledge_base_id": knowledge_base_id,
                "knowledge_base_name": kb.name,
                "evidence": [],
                "evidence_count": 0,
                "candidates_retrieved": 0,
                "message": f"知识库 '{kb.name}' 当前为空，尚未索引任何有效文档分块。",
                "degraded": False,
                "degradation_reason": None,
            }

        # 3. Parallel Dual-path Hybrid Retrieval (Dense + BM25 merged via RRF)
        candidate_pool_size = max(
            top_k * settings.RRF_CANDIDATE_FACTOR,
            settings.RERANKER_CANDIDATE_POOL_SIZE,
        )
        hybrid_retriever = HybridRetriever(session)
        hybrid_res = await hybrid_retriever.search(
            knowledge_base_id=kb.id,
            query=query,
            top_k=candidate_pool_size,
        )

        # 4. Handle no chunks retrieved
        if not hybrid_res.chunks:
            return {
                "query": query,
                "knowledge_base_id": knowledge_base_id,
                "knowledge_base_name": kb.name,
                "evidence": [],
                "evidence_count": 0,
                "candidates_retrieved": 0,
                "message": f"在知识库 '{kb.name}' 中未找到与问题 '{query}' 相关的证据分块。",
                "dense_duration_ms": hybrid_res.dense_duration_ms,
                "bm25_duration_ms": hybrid_res.bm25_duration_ms,
                "rrf_duration_ms": hybrid_res.rrf_duration_ms,
                "total_retrieval_duration_ms": hybrid_res.total_duration_ms,
                "degraded": hybrid_res.degradation_status != "none",
                "degradation_reason": hybrid_res.degradation_status if hybrid_res.degradation_status != "none" else None,
            }

        # 5. Cross-Encoder Reranker precision scoring & sorting
        rerank_res = await self.reranker.rerank(
            query=query,
            candidates=hybrid_res.chunks,
            top_k=top_k,
        )
        ranked_chunks = rerank_res.chunks

        # 6. Global Run-scoped Evidence Registration
        if self.evidence_registry is not None:
            evidence_items = self.evidence_registry.register_chunks(ranked_chunks)
        else:
            # Standalone call fallback
            evidence_items = [
                EvidenceItem(
                    ref_id=idx,
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    document_name=c.document_name,
                    page_number=c.page_number,
                    chunk_index=c.chunk_index,
                    content=c.content,
                    similarity_score=c.similarity_score,
                    dense_rank=c.dense_rank,
                    bm25_rank=c.bm25_rank,
                    dense_score=c.dense_score,
                    bm25_score=c.bm25_score,
                    fusion_score=c.fusion_score,
                    reranker_score=c.reranker_score,
                    reranker_raw_score=c.reranker_raw_score,
                )
                for idx, c in enumerate(ranked_chunks, start=1)
            ]

        # 7. Construct and return structured evidence response
        total_retrieval_ms = round(hybrid_res.total_duration_ms + rerank_res.duration_ms, 2)
        is_degraded = (hybrid_res.degradation_status != "none") or rerank_res.degraded
        degrade_reason = (
            rerank_res.degradation_reason
            if rerank_res.degraded
            else (hybrid_res.degradation_status if hybrid_res.degradation_status != "none" else None)
        )

        return {
            "query": query,
            "knowledge_base_id": knowledge_base_id,
            "knowledge_base_name": kb.name,
            "source_type": "knowledge_base",
            "evidence": [e.model_dump() for e in evidence_items],
            "evidence_count": len(evidence_items),
            "evidence_refs": [f"[Ref-{e.ref_id}]" for e in evidence_items],
            "candidates_retrieved": len(hybrid_res.chunks),
            "retrieved_chunks": len(ranked_chunks),
            "returned_chunks": len(evidence_items),
            "dense_duration_ms": hybrid_res.dense_duration_ms,
            "bm25_duration_ms": hybrid_res.bm25_duration_ms,
            "rrf_duration_ms": hybrid_res.rrf_duration_ms,
            "reranker_duration_ms": rerank_res.duration_ms,
            "total_retrieval_duration_ms": total_retrieval_ms,
            "degraded": is_degraded,
            "degradation_reason": degrade_reason,
        }
