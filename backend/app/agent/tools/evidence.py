import threading
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field

from app.retrieval.vector_store import ScoredChunk


class EvidenceItem(BaseModel):
    """
    Structured evidence item produced by search tools and tracked globally per Agent Run.
    Preserves all provenance and ranking metadata from ScoredChunk without data loss.
    """
    ref_id: int = Field(..., description="Run-scoped globally unique citation index (1-based)")
    chunk_id: str = Field(..., description="Unique chunk UUID")
    document_id: str = Field(..., description="Source document UUID")
    document_name: str = Field(..., description="Human-readable document file name")
    page_number: int = Field(default=1, description="Original document page number")
    chunk_index: int = Field(default=0, description="Chunk sequence index within document")
    content: str = Field(..., description="Full text content of the chunk")
    similarity_score: float = Field(..., description="Final relevance score (normalized [0, 1])")
    dense_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    dense_score: Optional[float] = None
    bm25_score: Optional[float] = None
    fusion_score: Optional[float] = None
    reranker_score: Optional[float] = None
    reranker_raw_score: Optional[float] = None
    source_type: Optional[str] = Field(default=None, description="来源类型: knowledge_base / pdf / docx / markdown / txt")
    heading_path: Optional[Union[List[str], str]] = Field(default=None, description="章节路径 (DOCX / Markdown)")
    block_index: Optional[int] = Field(default=None, description="段落编号 (DOCX)")
    line_start: Optional[int] = Field(default=None, description="起始行号 (TXT)")
    line_end: Optional[int] = Field(default=None, description="结束行号 (TXT)")

    @property
    def tag(self) -> str:
        """Formatted citation tag, e.g. [Ref-1]."""
        return f"[Ref-{self.ref_id}]"

    @property
    def provenance_label(self) -> str:
        src = (self.source_type or "").lower()
        if src == "pdf" and self.page_number:
            return f"页码: 第 {self.page_number} 页"
        elif src == "docx":
            parts = []
            if self.heading_path:
                parts.append(f"章节: {self.heading_path}")
            if self.block_index is not None:
                parts.append(f"段落: #{self.block_index}")
            return " | ".join(parts) if parts else "文档段落"
        elif src in ["markdown", "md"]:
            return f"章节: {self.heading_path}" if self.heading_path else "文档正文"
        elif src == "txt":
            if self.line_start is not None and self.line_end is not None:
                if self.line_start == self.line_end:
                    return f"行号: 第 {self.line_start} 行"
                return f"行号: 第 {self.line_start}-{self.line_end} 行"
            return "文本内容"
        elif self.page_number:
            return f"页码: 第 {self.page_number} 页"
        return "参考资料"

    def to_scored_chunk(self) -> ScoredChunk:
        """Convert back to ScoredChunk for seamless CitationExtractor validation."""
        if self.source_type and self.source_type != "knowledge_base":
            from app.attachments.evidence import AttachmentScoredChunk
            return AttachmentScoredChunk(
                chunk_id=self.chunk_id,
                document_id=self.document_id,
                document_name=self.document_name,
                page_number=self.page_number,
                chunk_index=self.chunk_index,
                content=self.content,
                similarity_score=self.similarity_score,
                dense_rank=self.dense_rank,
                bm25_rank=self.bm25_rank,
                dense_score=self.dense_score,
                bm25_score=self.bm25_score,
                fusion_score=self.fusion_score,
                reranker_score=self.reranker_score,
                reranker_raw_score=self.reranker_raw_score,
                attachment_id=self.document_id,
                source_type=self.source_type,
                heading_path=self.heading_path,
                block_index=self.block_index,
                line_start=self.line_start,
                line_end=self.line_end,
            )
        return ScoredChunk(
            chunk_id=self.chunk_id,
            document_id=self.document_id,
            document_name=self.document_name,
            page_number=self.page_number,
            chunk_index=self.chunk_index,
            content=self.content,
            similarity_score=self.similarity_score,
            dense_rank=self.dense_rank,
            bm25_rank=self.bm25_rank,
            dense_score=self.dense_score,
            bm25_score=self.bm25_score,
            fusion_score=self.fusion_score,
            reranker_score=self.reranker_score,
            reranker_raw_score=self.reranker_raw_score,
        )


class EvidenceRegistry:
    """
    Run-scoped Evidence Registry for Agentic RAG.
    Guarantees:
    1. Cross-tool deduplication: if multiple tool calls recall the same chunk, it is deduplicated.
    2. Global reference uniqueness: Ref-1, Ref-2, ..., Ref-N are assigned sequentially across
       multiple search calls, preventing ID collisions in the agent's context.
    3. Thread/async safety: Thread-safe locking protects state during concurrent tool executions.
    4. Run-level Token Budget & Chunk limits: Cumulative token budget tracking prevents context explosion
       from runaway multiple tool calls; rejected/pruned chunks are never registered into citation index.
    5. CitationExtractor compatibility: can export ordered ScoredChunk list matching [Ref-X] indices.
    """

    def __init__(
        self,
        max_total_evidence_tokens: int = 4000,
        max_evidence_chunks: int = 20,
    ):
        self._chunks_by_id: Dict[str, ScoredChunk] = {}
        self._ref_id_by_chunk_id: Dict[str, int] = {}
        self._registered_list: List[ScoredChunk] = []
        self._lock = threading.Lock()
        self.max_total_evidence_tokens = max_total_evidence_tokens
        self.max_evidence_chunks = max_evidence_chunks
        self.cumulative_evidence_tokens: int = 0
        self.budget_exceeded: bool = False
        self.pruned_chunks_count: int = 0
        from app.rag.context_budget import TokenEstimator
        self.estimator = TokenEstimator()

    def _to_evidence_item(self, ref_id: int, chunk: ScoredChunk) -> EvidenceItem:
        return EvidenceItem(
            ref_id=ref_id,
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_name=chunk.document_name,
            page_number=chunk.page_number,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            similarity_score=chunk.similarity_score,
            dense_rank=chunk.dense_rank,
            bm25_rank=chunk.bm25_rank,
            dense_score=chunk.dense_score,
            bm25_score=chunk.bm25_score,
            fusion_score=chunk.fusion_score,
            reranker_score=chunk.reranker_score,
            reranker_raw_score=chunk.reranker_raw_score,
            source_type=getattr(chunk, "source_type", None),
            heading_path=getattr(chunk, "heading_path", None),
            block_index=getattr(chunk, "block_index", None),
            line_start=getattr(chunk, "line_start", None),
            line_end=getattr(chunk, "line_end", None),
        )

    def register_chunks(self, chunks: List[ScoredChunk]) -> List[EvidenceItem]:
        """
        Register a batch of ScoredChunk objects from a retrieval tool call.
        Enforces cross-call deduplication, global Ref-X sequencing, and run-level token budget limits.
        """
        with self._lock:
            items: List[EvidenceItem] = []
            for chunk in chunks:
                cid = chunk.chunk_id
                if cid in self._ref_id_by_chunk_id:
                    # Deduplication: reuse existing global ref_id without adding new token overhead
                    ref_id = self._ref_id_by_chunk_id[cid]
                    existing_chunk = self._chunks_by_id[cid]
                    items.append(self._to_evidence_item(ref_id, existing_chunk))
                    continue

                # Estimate tokens for new incoming chunk
                chunk_tokens = self.estimator.estimate(chunk.content)
                if (
                    self.cumulative_evidence_tokens + chunk_tokens > self.max_total_evidence_tokens
                    or len(self._registered_list) >= self.max_evidence_chunks
                ):
                    self.budget_exceeded = True
                    self.pruned_chunks_count += 1
                    # Do not register pruned chunk; omit from items so model cannot cite it!
                    continue

                # Register new evidence chunk
                ref_id = len(self._registered_list) + 1
                self._chunks_by_id[cid] = chunk
                self._ref_id_by_chunk_id[cid] = ref_id
                self._registered_list.append(chunk)
                self.cumulative_evidence_tokens += chunk_tokens

                items.append(self._to_evidence_item(ref_id, chunk))
            return items

    def get_all_chunks(self) -> List[ScoredChunk]:
        """
        Return all uniquely registered chunks in strict order of ref_id (1-based index).
        Directly consumable by CitationExtractor.extract_citations().
        """
        return list(self._registered_list)

    def get_all_evidence_items(self) -> List[EvidenceItem]:
        """
        Return all uniquely registered evidence items with persistent ref_ids.
        Directly exportable to AgentState evidence.
        """
        items: List[EvidenceItem] = []
        for idx, chunk in enumerate(self._registered_list, start=1):
            items.append(
                EvidenceItem(
                    ref_id=idx,
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    document_name=chunk.document_name,
                    page_number=chunk.page_number,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    similarity_score=chunk.similarity_score,
                    dense_rank=chunk.dense_rank,
                    bm25_rank=chunk.bm25_rank,
                    dense_score=chunk.dense_score,
                    bm25_score=chunk.bm25_score,
                    fusion_score=chunk.fusion_score,
                    reranker_score=chunk.reranker_score,
                    reranker_raw_score=chunk.reranker_raw_score,
                    source_type=getattr(chunk, "source_type", None),
                    heading_path=getattr(chunk, "heading_path", None),
                    block_index=getattr(chunk, "block_index", None),
                    line_start=getattr(chunk, "line_start", None),
                    line_end=getattr(chunk, "line_end", None),
                )
            )
        return items

    def get_chunk_by_ref_id(self, ref_id: int) -> Optional[ScoredChunk]:
        if 1 <= ref_id <= len(self._registered_list):
            return self._registered_list[ref_id - 1]
        return None

    @property
    def total_evidence_count(self) -> int:
        return len(self._registered_list)

    def format_evidence_text(self, items: List[EvidenceItem]) -> str:
        """Format evidence items into clean markdown blocks for LLM reading."""
        if not items:
            if self.budget_exceeded:
                return "【系统提示：当前任务已达到累计证据 Token 预算上限，本次未能引入额外证据分块。请严格基于现有证据作答。】"
            return "未检索到相关证据分块。"
        blocks = []
        for it in items:
            block = (
                f"[Ref-{it.ref_id}] 来源文件: {it.document_name} | {it.provenance_label} | 相关度: {it.similarity_score:.4f}\n"
                f"{it.content.strip()}"
            )
            blocks.append(block)
        if self.budget_exceeded:
            blocks.append("【系统提示：当前任务已达到累计证据 Token 预算上限，部分超出预算分块已被安全裁剪。】")
        return "\n\n".join(blocks)
