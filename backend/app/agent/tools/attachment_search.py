import asyncio
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import BaseTool
from app.agent.tools.evidence import EvidenceItem, EvidenceRegistry
from app.attachments.service import AttachmentRetrievalService, attachment_retrieval_service
from app.core.logging import logger
from app.db.session import async_session_factory
from app.models.conversation import ConversationAttachment


class SearchConversationAttachmentsInput(BaseModel):
    """Input argument schema for search_conversation_attachments tool."""
    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="用于在当前会话已上传并解析的附件中检索相关技术事实、规范、条款或具体内容的查询问题",
    )
    top_k: int = Field(
        default=4,
        ge=1,
        le=20,
        description="期望返回的高相关证据分块数量(默认4，上限20)",
    )
    attachment_id: Optional[str] = Field(
        default=None,
        description="可选，限定检索指定附件UUID。若不指定则在当前会话的所有可用附件中联合检索",
    )


class SearchConversationAttachmentsTool(BaseTool):
    """
    SearchConversationAttachmentsTool encapsulates Conversation-scoped attachment retrieval
    for LangGraph Agent Runtime.
    Features:
    1. Strict Conversation isolation: conversation_id injected from server runtime context only.
    2. Attachment ownership security check: verifies attachment_id belongs to current conversation.
    3. Reuses Stage 2 AttachmentRetrievalService (dual-track full-text vs BM25).
    4. Registers evidence into the Run-scoped EvidenceRegistry for unified [Ref-X] sequencing.
    5. Preserves detailed provenance (PDF page, DOCX heading/block, Markdown headings, TXT lines).
    """

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        db: Optional[AsyncSession] = None,
        evidence_registry: Optional[EvidenceRegistry] = None,
        retrieval_service: Optional[AttachmentRetrievalService] = None,
    ):
        self.conversation_id = conversation_id
        self.db = db
        self.evidence_registry = evidence_registry
        self.retrieval_service = retrieval_service or attachment_retrieval_service

    @property
    def name(self) -> str:
        return "search_conversation_attachments"

    @property
    def description(self) -> str:
        return (
            "在当前会话中用户已上传的临时文档与附件（如 PDF、DOCX、Markdown、TXT）中进行精准检索与内容定位。"
            "当用户提问涉及上传文件的内容、总结、参数比对、条款查找或文档分析时，优先调用此工具。"
        )

    @property
    def args_schema(self) -> Type[BaseModel]:
        return SearchConversationAttachmentsInput

    def with_conversation_id(self, conversation_id: str) -> "SearchConversationAttachmentsTool":
        """Inject server-verified conversation_id."""
        self.conversation_id = conversation_id
        return self

    def with_evidence_registry(self, registry: EvidenceRegistry) -> "SearchConversationAttachmentsTool":
        """Attach Run-scoped EvidenceRegistry instance for global [Ref-X] tracking."""
        self.evidence_registry = registry
        return self

    def with_session(self, db: AsyncSession) -> "SearchConversationAttachmentsTool":
        """Attach existing database session."""
        self.db = db
        return self

    async def _run(
        self,
        query: str,
        top_k: int = 4,
        attachment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute attachment search using conversation-scoped retrieval service."""
        if self.db is not None:
            return await self._execute_search(self.db, query, top_k, attachment_id)
        else:
            async with async_session_factory() as session:
                return await self._execute_search(session, query, top_k, attachment_id)

    async def _execute_search(
        self,
        session: AsyncSession,
        query: str,
        top_k: int,
        attachment_id: Optional[str],
    ) -> Dict[str, Any]:
        conv_id = self.conversation_id
        if not conv_id:
            return {
                "query": query,
                "conversation_id": None,
                "evidence": [],
                "evidence_count": 0,
                "message": "当前执行上下文无有效会话ID，无法检索附件。",
            }

        # 1. Fetch attachments for current conversation
        stmt = (
            select(ConversationAttachment)
            .where(ConversationAttachment.conversation_id == conv_id)
            .order_by(ConversationAttachment.created_at.asc())
        )
        res = await session.execute(stmt)
        conv_attachments = list(res.scalars().all())

        # 2. Security validation if attachment_id is specified by model
        if attachment_id:
            matched = [a for a in conv_attachments if a.id == attachment_id]
            if not matched:
                # Check if attachment exists in another conversation (security breach check)
                cross_stmt = select(ConversationAttachment).where(ConversationAttachment.id == attachment_id)
                cross_res = await session.execute(cross_stmt)
                foreign_att = cross_res.scalar_one_or_none()
                if foreign_att:
                    logger.warning(
                        f"Security rejection: Tool attempted to access attachment '{attachment_id}' "
                        f"belonging to conversation '{foreign_att.conversation_id}' from current conversation '{conv_id}'."
                    )
                    raise PermissionError(
                        f"Security rejection: Attachment '{attachment_id}' does not belong to current conversation."
                    )
                else:
                    return {
                        "query": query,
                        "conversation_id": conv_id,
                        "attachment_id": attachment_id,
                        "evidence": [],
                        "evidence_count": 0,
                        "message": f"未找到指定的附件ID '{attachment_id}'。",
                    }
            target_attachments = matched
        else:
            if not conv_attachments:
                return {
                    "query": query,
                    "conversation_id": conv_id,
                    "evidence": [],
                    "evidence_count": 0,
                    "message": "当前会话未上传任何附件文档。若需分析请先上传文件。",
                }
            target_attachments = conv_attachments

        # 3. Filter completed attachments
        completed_attachments = [
            a for a in target_attachments if a.parse_status == "COMPLETED"
        ]
        if not completed_attachments:
            return {
                "query": query,
                "conversation_id": conv_id,
                "attachment_id": attachment_id,
                "evidence": [],
                "evidence_count": 0,
                "message": "指定的附件尚未完成解析或解析失败，无法检索。",
            }

        # 4. Execute conversation-scoped retrieval using AttachmentRetrievalService
        att_res = self.retrieval_service.prepare_attachment_context(
            conversation_id=conv_id,
            query=query,
            attachments=completed_attachments,
            top_k=top_k,
        )

        selected_chunks = att_res.selected_chunks

        # 5. Global Run-scoped Evidence Registration
        if self.evidence_registry is not None:
            evidence_items = self.evidence_registry.register_chunks(selected_chunks)
        else:
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
                    source_type=getattr(c, "source_type", "attachment"),
                    heading_path=getattr(c, "heading_path", None),
                    block_index=getattr(c, "block_index", None),
                    line_start=getattr(c, "line_start", None),
                    line_end=getattr(c, "line_end", None),
                )
                for idx, c in enumerate(selected_chunks, start=1)
            ]

        msg = (
            f"成功在附件中检索到 {len(evidence_items)} 条相关证据分块。"
            if evidence_items
            else f"在已上传附件中未找到与 '{query}' 相关的证据内容。"
        )

        first_att = completed_attachments[0] if completed_attachments else None
        return {
            "query": query,
            "conversation_id": conv_id,
            "attachment_id": attachment_id or (first_att.id if len(completed_attachments) == 1 else None),
            "original_filename": first_att.original_filename if len(completed_attachments) == 1 else None,
            "strategy": att_res.strategy,
            "source_type": "attachment",
            "evidence": [e.model_dump() for e in evidence_items],
            "evidence_count": len(evidence_items),
            "evidence_refs": [f"[Ref-{e.ref_id}]" for e in evidence_items],
            "retrieved_chunks": len(selected_chunks),
            "returned_chunks": len(evidence_items),
            "total_attachment_tokens": att_res.total_attachment_tokens,
            "used_tokens": att_res.used_tokens,
            "chunks_pruned": att_res.chunks_pruned,
            "message": msg,
        }
