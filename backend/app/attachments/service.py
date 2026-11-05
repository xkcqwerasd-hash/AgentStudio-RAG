from typing import Any, Dict, List, Optional

from app.attachments.budget import AttachmentBudgetManager, AttachmentBudgetResult
from app.attachments.chunker import AttachmentChunk, AttachmentChunker
from app.attachments.evidence import AttachmentEvidenceItem, AttachmentScoredChunk
from app.attachments.retriever import AttachmentIndexManager, attachment_index_manager
from app.core.logging import logger
from app.models.conversation import ConversationAttachment


class AttachmentRetrievalService:
    """
    Unified Conversation-scoped Attachment Retrieval & Context Budget Service.
    Seamlessly orchestrates:
    - Adaptive chunking (PDF, DOCX, TXT, Markdown).
    - Conversation-isolated in-memory BM25 indexing.
    - Dual-track context assembly (Small full-text vs Large BM25 retrieval).
    - Token budget constraint and citation coherence.
    """

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        full_text_threshold_tokens: int = 4000,
        default_attachment_budget: int = 6000,
        index_manager: Optional[AttachmentIndexManager] = None,
    ):
        self.chunker = AttachmentChunker(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self.budget_manager = AttachmentBudgetManager(
            full_text_threshold_tokens=full_text_threshold_tokens,
            default_attachment_budget=default_attachment_budget,
        )
        self.index_manager = index_manager or attachment_index_manager

    def search_attachments(
        self,
        conversation_id: str,
        query: str,
        attachments: List[ConversationAttachment],
        top_k: int = 4,
        chunk_size: Optional[int] = None,
    ) -> List[AttachmentScoredChunk]:
        """
        Direct conversation-scoped BM25 search across all attachment chunks.
        """
        completed = [a for a in attachments if a.parse_status == "COMPLETED"]
        if not completed or not query.strip():
            return []

        # If custom chunk size specified, use temporary chunker
        chunker = (
            AttachmentChunker(chunk_size=chunk_size)
            if chunk_size is not None
            else self.chunker
        )
        chunks = chunker.chunk_attachments(completed)
        if not chunks:
            return []

        index = self.index_manager.get_or_build_index(conversation_id, chunks)
        return index.search(query=query, top_k=top_k)

    def prepare_attachment_context(
        self,
        conversation_id: str,
        query: str,
        attachments: List[ConversationAttachment],
        system_prompt: str = "",
        reserved_for_history: int = 2048,
        reserved_for_kb: int = 4096,
        override_budget: Optional[int] = None,
        top_k: int = 4,
        force_strategy: Optional[str] = None,  # "full_text" or "bm25_retrieval"
    ) -> AttachmentBudgetResult:
        """
        Prepare attachment context with automatic Dual-Track budgeting:
        - Small Attachments (<= threshold & <= budget): Complete text preserved.
        - Large Attachments (> threshold or > budget): BM25 Top-K chunk retrieval & pruning.
        """
        completed = [a for a in attachments if a.parse_status == "COMPLETED"]
        # Filter attachments with actual readable text
        valid_attachments = [
            a for a in completed if a.extracted_text and a.extracted_text.strip()
        ]
        if not valid_attachments:
            return AttachmentBudgetResult(
                strategy="empty",
                total_attachment_tokens=0,
                allocated_budget=0,
                used_tokens=0,
                chunks_retained=0,
                chunks_pruned=0,
                attachments_count=len(attachments),
                selected_chunks=[],
                formatted_context="",
                details={"reason": "No readable text found in attachments"},
            )

        # 1. Calculate effective budget
        effective_budget = self.budget_manager.calculate_effective_attachment_budget(
            query=query,
            system_prompt=system_prompt,
            reserved_for_history=reserved_for_history,
            reserved_for_kb=reserved_for_kb,
            override_budget=override_budget,
        )

        # 2. Total attachment tokens
        total_tokens = self.budget_manager.calculate_total_attachment_tokens(valid_attachments)

        # 3. Dual-track decision
        is_small = (
            total_tokens <= self.budget_manager.full_text_threshold
            and total_tokens <= effective_budget
        )

        if force_strategy == "full_text" or (force_strategy is None and is_small):
            return self.budget_manager.assemble_full_text_context(
                attachments=valid_attachments,
                budget=effective_budget,
            )
        else:
            # Large attachment strategy: chunk, index, search & prune
            chunks = self.chunker.chunk_attachments(valid_attachments)
            if not chunks:
                return AttachmentBudgetResult(
                    strategy="empty",
                    total_attachment_tokens=total_tokens,
                    allocated_budget=effective_budget,
                    used_tokens=0,
                    chunks_retained=0,
                    chunks_pruned=0,
                    attachments_count=len(valid_attachments),
                    selected_chunks=[],
                    formatted_context="",
                    details={"reason": "Attachments produced no text chunks"},
                )

            index = self.index_manager.get_or_build_index(conversation_id, chunks)
            # Retrieve wider candidate pool (up to 3x top_k) for budget pruning
            candidate_pool = index.search(query=query, top_k=max(top_k * 3, 10))

            return self.budget_manager.assemble_retrieved_context(
                candidate_chunks=candidate_pool,
                total_attachment_tokens=total_tokens,
                budget=effective_budget,
                attachments_count=len(valid_attachments),
            )

    def invalidate_conversation(self, conversation_id: str) -> None:
        """Invalidate in-memory index for a conversation."""
        self.index_manager.invalidate_conversation(conversation_id)

    def clear_all_indexes(self) -> None:
        """Clear all in-memory indexes."""
        self.index_manager.clear()


# Global singleton service
attachment_retrieval_service = AttachmentRetrievalService()
