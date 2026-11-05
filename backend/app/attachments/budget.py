from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from app.attachments.chunker import AttachmentChunk
from app.attachments.evidence import AttachmentScoredChunk, chunk_to_scored_chunk
from app.core.config import settings
from app.core.exceptions import ContextBudgetExceededError, QueryTooLongError
from app.models.conversation import ConversationAttachment
from app.rag.context_budget import estimate_tokens


@dataclass
class AttachmentBudgetResult:
    """
    Structured outcome of the Dual-Track Attachment Context allocation.
    """
    strategy: Literal["full_text", "bm25_retrieval", "empty"]
    total_attachment_tokens: int
    allocated_budget: int
    used_tokens: int
    chunks_retained: int
    chunks_pruned: int
    attachments_count: int
    selected_chunks: List[AttachmentScoredChunk]
    formatted_context: str
    details: Dict[str, Any] = field(default_factory=dict)


class AttachmentBudgetManager:
    """
    Dual-Track Attachment Context & Token Budget Manager.
    Governs whether to inject attachments as Full-Text or via BM25 Top-K retrieval,
    coordinating with the global context window and user query protection.
    """

    def __init__(
        self,
        model_context_window: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        context_safety_margin: Optional[int] = None,
        max_query_tokens: Optional[int] = None,
        full_text_threshold_tokens: int = 4000,
        default_attachment_budget: int = 6000,
    ):
        self.model_context_window = (
            model_context_window
            if model_context_window is not None
            else settings.MODEL_CONTEXT_WINDOW
        )
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else settings.MAX_OUTPUT_TOKENS
        )
        self.context_safety_margin = (
            context_safety_margin
            if context_safety_margin is not None
            else settings.CONTEXT_SAFETY_MARGIN
        )
        self.max_query_tokens = (
            max_query_tokens
            if max_query_tokens is not None
            else settings.MAX_QUERY_TOKENS
        )
        self.full_text_threshold = full_text_threshold_tokens
        self.default_attachment_budget = default_attachment_budget

    @property
    def available_input_budget(self) -> int:
        """Net usable input budget after reserving model output and safety margin."""
        net = self.model_context_window - self.max_output_tokens - self.context_safety_margin
        return max(512, net)

    def validate_user_query(self, query: str) -> int:
        """
        Enforce user query protection with highest priority.
        Raises QueryTooLongError if query exceeds allowed threshold.
        """
        query_tokens = estimate_tokens(query)
        if query_tokens > self.max_query_tokens:
            raise QueryTooLongError(query_tokens, self.max_query_tokens)
        return query_tokens

    def calculate_effective_attachment_budget(
        self,
        query: str,
        system_prompt: str = "",
        reserved_for_history: int = 2048,
        reserved_for_kb: int = 4096,
        override_budget: Optional[int] = None,
    ) -> int:
        """
        Compute effective budget available for attachments without conflicting
        with user query, system prompt, conversation history, or KB retrieval.
        """
        query_tokens = self.validate_user_query(query)
        sys_tokens = estimate_tokens(system_prompt)
        prompt_overhead = 150

        remaining_pool = (
            self.available_input_budget
            - query_tokens
            - sys_tokens
            - prompt_overhead
            - reserved_for_history
            - reserved_for_kb
        )

        if override_budget is not None:
            return max(50, min(override_budget, max(50, remaining_pool)))

        target = min(self.default_attachment_budget, max(512, remaining_pool))
        return target

    def calculate_total_attachment_tokens(
        self,
        attachments: List[ConversationAttachment],
    ) -> int:
        """
        Calculate total estimated tokens across all completed attachments.
        """
        total = 0
        for att in attachments:
            if att.parse_status != "COMPLETED":
                continue
            if att.token_count and att.token_count > 0:
                total += att.token_count
            else:
                total += estimate_tokens(att.extracted_text)
        return total

    def prune_retrieved_chunks(
        self,
        chunks: List[AttachmentScoredChunk],
        budget: int,
    ) -> Tuple[List[AttachmentScoredChunk], int, int, int]:
        """
        Prune candidate attachment chunks based on relevance ranking and token budget:
        - Highest-scored chunks are preserved first.
        - Chunks are never cut mid-sentence.
        - Discarded chunks are completely excluded from the result to guarantee
          that CitationExtractor only validates against retained chunks.
        Returns: (retained_chunks, used_tokens, kept_count, pruned_count)
        """
        if not chunks or budget <= 0:
            return [], 0, 0, len(chunks)

        # Sort strictly descending by similarity_score
        sorted_chunks = sorted(chunks, key=lambda c: c.similarity_score, reverse=True)

        retained: List[AttachmentScoredChunk] = []
        accumulated_tokens = 0

        for idx, chunk in enumerate(sorted_chunks, start=1):
            prov_label = self._format_chunk_provenance(chunk)
            sample_block = (
                f"[Ref-{idx}] 来源文件: {chunk.document_name} | {prov_label} | 相关度: {chunk.similarity_score:.4f}\n"
                f"{chunk.content.strip()}"
            )
            block_tokens = estimate_tokens(sample_block)

            if accumulated_tokens + block_tokens <= budget:
                retained.append(chunk)
                accumulated_tokens += block_tokens
            else:
                # If budget is too tight to hold even one chunk, preserve at least top-1 if budget >= 50
                if not retained and budget >= 50:
                    retained.append(chunk)
                    accumulated_tokens += block_tokens
                break

        pruned_count = len(chunks) - len(retained)
        return retained, accumulated_tokens, len(retained), pruned_count

    def assemble_full_text_context(
        self,
        attachments: List[ConversationAttachment],
        budget: int,
    ) -> AttachmentBudgetResult:
        """
        Execute Small Attachment Strategy:
        Assemble complete attachment content without lossy chunking.
        """
        selected_chunks: List[AttachmentScoredChunk] = []
        blocks: List[str] = []
        accumulated_tokens = 0
        ref_id = 1

        for att in attachments:
            if att.parse_status != "COMPLETED":
                continue

            pages = att.parsed_pages
            if not pages:
                # Plain extracted text fallback
                text = att.extracted_text.strip()
                if not text:
                    continue
                safe_page = 1
                chunk = AttachmentScoredChunk(
                    chunk_id=f"att_full_{att.id[:8]}_{ref_id}",
                    document_id=att.id,
                    document_name=att.original_filename,
                    page_number=safe_page,
                    chunk_index=ref_id - 1,
                    content=text,
                    similarity_score=1.0,
                    attachment_id=att.id,
                    conversation_id=att.conversation_id,
                    source_type=self._get_source_type(att.file_extension),
                    heading_path=None,
                    block_index=None,
                    line_start=1,
                    line_end=text.count("\n") + 1,
                )
                prov = "全文参考"
                block = f"[Ref-{ref_id}] 来源文件: {att.original_filename} | {prov}\n{text}"
                b_tokens = estimate_tokens(block)

                selected_chunks.append(chunk)
                blocks.append(block)
                accumulated_tokens += b_tokens
                ref_id += 1
            else:
                # Per-page / per-section representation
                for p_idx, page in enumerate(pages, start=1):
                    p_text = page.get("text", "").strip()
                    if not p_text:
                        continue
                    meta = page.get("metadata", {})
                    p_num = page.get("page_number")
                    h_path = meta.get("heading_path")
                    b_idx = meta.get("block_index")
                    src_type = self._get_source_type(att.file_extension)

                    safe_page_num = p_num if p_num is not None else 1
                    chunk = AttachmentScoredChunk(
                        chunk_id=f"att_full_{att.id[:8]}_{ref_id}",
                        document_id=att.id,
                        document_name=att.original_filename,
                        page_number=safe_page_num,
                        chunk_index=ref_id - 1,
                        content=p_text,
                        similarity_score=1.0,
                        attachment_id=att.id,
                        conversation_id=att.conversation_id,
                        source_type=src_type,
                        heading_path=h_path,
                        block_index=b_idx,
                        line_start=meta.get("line_start"),
                        line_end=meta.get("line_end"),
                    )
                    prov = self._format_chunk_provenance(chunk)
                    block = f"[Ref-{ref_id}] 来源文件: {att.original_filename} | {prov}\n{p_text}"
                    b_tokens = estimate_tokens(block)

                    selected_chunks.append(chunk)
                    blocks.append(block)
                    accumulated_tokens += b_tokens
                    ref_id += 1

        formatted = "\n\n".join(blocks)
        total_tokens = self.calculate_total_attachment_tokens(attachments)

        return AttachmentBudgetResult(
            strategy="full_text",
            total_attachment_tokens=total_tokens,
            allocated_budget=budget,
            used_tokens=accumulated_tokens,
            chunks_retained=len(selected_chunks),
            chunks_pruned=0,
            attachments_count=len(attachments),
            selected_chunks=selected_chunks,
            formatted_context=formatted,
            details={"strategy_reason": "Total tokens fit within full-text threshold and budget"},
        )

    def assemble_retrieved_context(
        self,
        candidate_chunks: List[AttachmentScoredChunk],
        total_attachment_tokens: int,
        budget: int,
        attachments_count: int,
    ) -> AttachmentBudgetResult:
        """
        Execute Large Attachment Strategy:
        Prune Top-K retrieved chunks by similarity and budget, format contiguous [Ref-X] blocks.
        """
        retained, used_tokens, kept_count, pruned_count = self.prune_retrieved_chunks(
            chunks=candidate_chunks,
            budget=budget,
        )

        blocks: List[str] = []
        ordered_chunks: List[AttachmentScoredChunk] = []

        for idx, chunk in enumerate(retained, start=1):
            prov = self._format_chunk_provenance(chunk)
            block = (
                f"[Ref-{idx}] 来源文件: {chunk.document_name} | {prov} | 相关度: {chunk.similarity_score:.4f}\n"
                f"{chunk.content.strip()}"
            )
            blocks.append(block)

            # Ensure chunk_index corresponds to sequential citation index
            chunk.chunk_index = idx - 1
            ordered_chunks.append(chunk)

        formatted = "\n\n".join(blocks)

        return AttachmentBudgetResult(
            strategy="bm25_retrieval",
            total_attachment_tokens=total_attachment_tokens,
            allocated_budget=budget,
            used_tokens=used_tokens,
            chunks_retained=kept_count,
            chunks_pruned=pruned_count,
            attachments_count=attachments_count,
            selected_chunks=ordered_chunks,
            formatted_context=formatted,
            details={
                "strategy_reason": "Total tokens exceeded threshold, executed BM25 Top-K retrieval",
                "candidate_count": len(candidate_chunks),
            },
        )

    @staticmethod
    def _format_chunk_provenance(chunk: AttachmentScoredChunk) -> str:
        src = (chunk.source_type or "").lower()
        if src == "pdf" and chunk.page_number:
            return f"页码: 第 {chunk.page_number} 页"
        elif src == "docx":
            parts = []
            if chunk.heading_path:
                h_str = " > ".join(chunk.heading_path) if isinstance(chunk.heading_path, list) else str(chunk.heading_path)
                parts.append(f"章节: {h_str}")
            if chunk.block_index is not None:
                parts.append(f"段落: #{chunk.block_index}")
            return " | ".join(parts) if parts else "段落内容"
        elif src in ["markdown", "md"]:
            if chunk.heading_path:
                h_str = " > ".join(chunk.heading_path) if isinstance(chunk.heading_path, list) else str(chunk.heading_path)
                return f"章节: {h_str}"
            return "文档正文"
        elif src == "txt":
            if chunk.line_start is not None and chunk.line_end is not None:
                if chunk.line_start == chunk.line_end:
                    return f"行号: 第 {chunk.line_start} 行"
                return f"行号: 第 {chunk.line_start}-{chunk.line_end} 行"
            return "文本内容"
        elif chunk.page_number:
            return f"页码: 第 {chunk.page_number} 页"
        return "附件正文"

    @staticmethod
    def _get_source_type(ext: Optional[str]) -> str:
        if not ext:
            return "txt"
        e = ext.lower().lstrip(".")
        if e == "pdf":
            return "pdf"
        elif e == "docx":
            return "docx"
        elif e in ["md", "markdown"]:
            return "markdown"
        return "txt"
