from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field

from app.attachments.chunker import AttachmentChunk
from app.retrieval.vector_store import ScoredChunk


@dataclass
class AttachmentScoredChunk(ScoredChunk):
    """
    Subclass of ScoredChunk enriched with attachment provenance details.
    Guarantees 100% backward and forward compatibility with CitationExtractor,
    EvidenceRegistry, and ContextBudgetManager.
    """
    attachment_id: str = ""
    conversation_id: str = ""
    source_type: str = "attachment"
    heading_path: Optional[Union[List[str], str]] = None
    block_index: Optional[int] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None


class AttachmentEvidenceItem(BaseModel):
    """
    Structured attachment evidence item tracking source attribution without data loss.
    Provides human-readable provenance strings tailored to PDF, DOCX, Markdown, and TXT.
    """
    ref_id: int = Field(..., description="Citation index (1-based, e.g. [Ref-1])")
    chunk_id: str = Field(..., description="Unique chunk UUID")
    attachment_id: str = Field(..., description="Source attachment UUID")
    conversation_id: str = Field(..., description="Conversation UUID")
    original_filename: str = Field(..., description="Original attachment file name")
    source_type: str = Field(..., description="File category: pdf / docx / markdown / txt")
    content: str = Field(..., description="Full text content of the chunk")
    similarity_score: float = Field(default=1.0, description="Normalized relevance score [0, 1]")
    page_number: Optional[int] = Field(default=None, description="Physical page number (PDF only)")
    heading_path: Optional[Union[List[str], str]] = Field(default=None, description="Heading path (DOCX / Markdown)")
    block_index: Optional[int] = Field(default=None, description="Section / block index (DOCX)")
    line_start: Optional[int] = Field(default=None, description="Start line (TXT)")
    line_end: Optional[int] = Field(default=None, description="End line (TXT)")
    bm25_score: Optional[float] = None
    bm25_rank: Optional[int] = None

    @property
    def tag(self) -> str:
        """Formatted reference tag, e.g. [Ref-1]."""
        return f"[Ref-{self.ref_id}]"

    @property
    def provenance_label(self) -> str:
        """
        Precise source location label:
        - PDF: Physical page number
        - DOCX: Heading path and block index
        - Markdown: Heading path
        - TXT: Line range
        """
        if self.source_type == "pdf" and self.page_number is not None:
            return f"页码: 第 {self.page_number} 页"
        elif self.source_type == "docx":
            parts = []
            if self.heading_path:
                parts.append(f"章节: {self.heading_path}")
            if self.block_index is not None:
                parts.append(f"段落: #{self.block_index}")
            return " | ".join(parts) if parts else "文档段落"
        elif self.source_type in ["markdown", "md"]:
            return f"章节: {self.heading_path}" if self.heading_path else "文档正文"
        elif self.source_type == "txt":
            if self.line_start is not None and self.line_end is not None:
                if self.line_start == self.line_end:
                    return f"行号: 第 {self.line_start} 行"
                return f"行号: 第 {self.line_start}-{self.line_end} 行"
            return "文本内容"
        elif self.page_number is not None:
            return f"页码: 第 {self.page_number} 页"
        return "附件正文"

    def format_reference_block(self) -> str:
        """Format chunk into prompt injection block with reference tag and provenance."""
        return (
            f"[Ref-{self.ref_id}] 来源文件: {self.original_filename} | {self.provenance_label} | 相关度: {self.similarity_score:.4f}\n"
            f"{self.content.strip()}"
        )

    def to_scored_chunk(self) -> AttachmentScoredChunk:
        """Convert to AttachmentScoredChunk for CitationExtractor verification."""
        # For Pydantic Citation compliance, default page_number to 1 if not physical PDF
        safe_page_num = self.page_number if self.page_number is not None else 1
        return AttachmentScoredChunk(
            chunk_id=self.chunk_id,
            document_id=self.attachment_id,
            document_name=self.original_filename,
            page_number=safe_page_num,
            chunk_index=self.ref_id - 1,
            content=self.content,
            similarity_score=self.similarity_score,
            attachment_id=self.attachment_id,
            conversation_id=self.conversation_id,
            source_type=self.source_type,
            heading_path=self.heading_path,
            block_index=self.block_index,
            line_start=self.line_start,
            line_end=self.line_end,
            bm25_score=self.bm25_score,
            bm25_rank=self.bm25_rank,
        )


def chunk_to_scored_chunk(
    chunk: AttachmentChunk,
    similarity_score: float = 1.0,
    bm25_score: Optional[float] = None,
    bm25_rank: Optional[int] = None,
) -> AttachmentScoredChunk:
    """Helper to convert an AttachmentChunk into an AttachmentScoredChunk."""
    safe_page_num = chunk.page_number if chunk.page_number is not None else 1
    return AttachmentScoredChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.attachment_id,
        document_name=chunk.original_filename,
        page_number=safe_page_num,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        similarity_score=similarity_score,
        attachment_id=chunk.attachment_id,
        conversation_id=chunk.conversation_id,
        source_type=chunk.source_type,
        heading_path=chunk.heading_path,
        block_index=chunk.block_index,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        bm25_score=bm25_score,
        bm25_rank=bm25_rank,
    )
