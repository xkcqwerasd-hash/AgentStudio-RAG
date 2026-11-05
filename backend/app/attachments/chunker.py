import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.ingestion.splitters.recursive_splitter import RecursiveCharacterTextSplitter
from app.models.conversation import ConversationAttachment
from app.rag.context_budget import estimate_tokens


@dataclass
class AttachmentChunk:
    """
    Standardized chunk extracted from a conversation attachment.
    Preserves exact provenance attributes without fabricating PDF page numbers for DOCX/TXT/MD.
    """
    chunk_id: str
    attachment_id: str
    conversation_id: str
    original_filename: str
    file_extension: str
    source_type: str
    chunk_index: int
    content: str
    char_length: int
    token_count: int
    page_number: Optional[int] = None       # Physical page (PDF only)
    heading_path: Optional[str] = None     # Section hierarchy (DOCX / Markdown)
    block_index: Optional[int] = None      # Block/paragraph sequence (DOCX)
    line_start: Optional[int] = None       # 1-based start line (TXT)
    line_end: Optional[int] = None         # 1-based end line (TXT)
    extra_metadata: Dict[str, Any] = field(default_factory=dict)


class AttachmentChunker:
    """
    Chunking engine for conversation attachments.
    Recursively splits parsed documents while preserving file-type specific provenance.
    """

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def chunk_attachment(
        self,
        attachment: ConversationAttachment,
    ) -> List[AttachmentChunk]:
        """
        Split a single attachment into AttachmentChunk items.
        Respects file type semantics:
        - PDF: preserves physical page_number.
        - DOCX: preserves heading_path and block_index (no fabricated page_number).
        - Markdown: preserves heading_path (no fabricated page_number).
        - TXT: preserves line_start and line_end (no fabricated page_number).
        """
        pages = attachment.parsed_pages
        if not pages:
            # Handle empty or unparsed attachment
            return []

        ext = (attachment.file_extension or "").lower()
        if not ext.startswith("."):
            ext = f".{ext}"

        source_type = self._detect_source_type(ext, attachment.mime_type)

        chunks: List[AttachmentChunk] = []
        global_chunk_idx = 0

        if source_type == "pdf":
            # PDF: chunk page by page to retain physical page_number
            for page in pages:
                page_text = page.get("text", "").strip()
                if not page_text:
                    continue
                p_num = page.get("page_number", 1)
                meta = page.get("metadata", {})

                split_pieces = self.splitter.split_text(page_text)
                for piece in split_pieces:
                    piece_clean = piece.strip()
                    if not piece_clean:
                        continue
                    c_id = f"att_chunk_{attachment.id[:8]}_{global_chunk_idx}"
                    chunks.append(
                        AttachmentChunk(
                            chunk_id=c_id,
                            attachment_id=attachment.id,
                            conversation_id=attachment.conversation_id,
                            original_filename=attachment.original_filename,
                            file_extension=ext,
                            source_type=source_type,
                            chunk_index=global_chunk_idx,
                            content=piece_clean,
                            char_length=len(piece_clean),
                            token_count=estimate_tokens(piece_clean),
                            page_number=p_num,
                            heading_path=None,
                            block_index=None,
                            line_start=None,
                            line_end=None,
                            extra_metadata=dict(meta),
                        )
                    )
                    global_chunk_idx += 1

        elif source_type == "docx":
            # DOCX: chunk section by section, preserving heading_path & block_index
            for page in pages:
                sec_text = page.get("text", "").strip()
                if not sec_text:
                    continue
                meta = page.get("metadata", {})
                h_path = meta.get("heading_path")
                b_idx = meta.get("block_index", page.get("page_number"))

                split_pieces = self.splitter.split_text(sec_text)
                for piece in split_pieces:
                    piece_clean = piece.strip()
                    if not piece_clean:
                        continue
                    c_id = f"att_chunk_{attachment.id[:8]}_{global_chunk_idx}"
                    chunks.append(
                        AttachmentChunk(
                            chunk_id=c_id,
                            attachment_id=attachment.id,
                            conversation_id=attachment.conversation_id,
                            original_filename=attachment.original_filename,
                            file_extension=ext,
                            source_type=source_type,
                            chunk_index=global_chunk_idx,
                            content=piece_clean,
                            char_length=len(piece_clean),
                            token_count=estimate_tokens(piece_clean),
                            page_number=None,  # Strictly NO fabricated physical page number
                            heading_path=h_path,
                            block_index=b_idx,
                            line_start=None,
                            line_end=None,
                            extra_metadata=dict(meta),
                        )
                    )
                    global_chunk_idx += 1

        elif source_type in ["markdown", "md"]:
            # Markdown: chunk section by section, preserving heading_path
            for page in pages:
                sec_text = page.get("text", "").strip()
                if not sec_text:
                    continue
                meta = page.get("metadata", {})
                h_path = meta.get("heading_path")

                split_pieces = self.splitter.split_text(sec_text)
                for piece in split_pieces:
                    piece_clean = piece.strip()
                    if not piece_clean:
                        continue
                    c_id = f"att_chunk_{attachment.id[:8]}_{global_chunk_idx}"
                    chunks.append(
                        AttachmentChunk(
                            chunk_id=c_id,
                            attachment_id=attachment.id,
                            conversation_id=attachment.conversation_id,
                            original_filename=attachment.original_filename,
                            file_extension=ext,
                            source_type=source_type,
                            chunk_index=global_chunk_idx,
                            content=piece_clean,
                            char_length=len(piece_clean),
                            token_count=estimate_tokens(piece_clean),
                            page_number=None,
                            heading_path=h_path,
                            block_index=None,
                            line_start=None,
                            line_end=None,
                            extra_metadata=dict(meta),
                        )
                    )
                    global_chunk_idx += 1

        else:
            # TXT / Other plain text: compute 1-based line_start and line_end
            full_text = attachment.extracted_text
            if not full_text:
                return []

            meta = pages[0].get("metadata", {}) if pages else {}
            split_pieces = self.splitter.split_text(full_text)

            search_cursor = 0
            for piece in split_pieces:
                piece_clean = piece.strip()
                if not piece_clean:
                    continue

                # Locate line range in full_text
                prefix = piece_clean[: min(30, len(piece_clean))]
                found_idx = full_text.find(prefix, search_cursor)
                if found_idx == -1:
                    found_idx = full_text.find(prefix)

                if found_idx != -1:
                    line_start = full_text[:found_idx].count("\n") + 1
                    line_end = line_start + piece_clean.count("\n")
                    search_cursor = found_idx + len(prefix)
                else:
                    line_start = 1
                    line_end = 1 + piece_clean.count("\n")

                c_id = f"att_chunk_{attachment.id[:8]}_{global_chunk_idx}"
                chunks.append(
                    AttachmentChunk(
                        chunk_id=c_id,
                        attachment_id=attachment.id,
                        conversation_id=attachment.conversation_id,
                        original_filename=attachment.original_filename,
                        file_extension=ext,
                        source_type="txt",
                        chunk_index=global_chunk_idx,
                        content=piece_clean,
                        char_length=len(piece_clean),
                        token_count=estimate_tokens(piece_clean),
                        page_number=None,
                        heading_path=None,
                        block_index=None,
                        line_start=line_start,
                        line_end=line_end,
                        extra_metadata=dict(meta),
                    )
                )
                global_chunk_idx += 1

        return chunks

    def chunk_attachments(
        self,
        attachments: List[ConversationAttachment],
    ) -> List[AttachmentChunk]:
        """
        Chunk multiple attachments belonging to a conversation into a unified list.
        """
        all_chunks: List[AttachmentChunk] = []
        for att in attachments:
            if att.parse_status != "COMPLETED":
                continue
            all_chunks.extend(self.chunk_attachment(att))
        return all_chunks

    @staticmethod
    def _detect_source_type(ext: str, mime_type: str) -> str:
        if ext == ".pdf" or "pdf" in mime_type:
            return "pdf"
        elif ext == ".docx" or "wordprocessingml" in mime_type:
            return "docx"
        elif ext in [".md", ".markdown"] or "markdown" in mime_type:
            return "markdown"
        return "txt"
