from pathlib import Path
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET
import zipfile

from app.ingestion.parsers.base import BaseParser, ParsedDocument, ParsedPage


# DOCX Security thresholds
MAX_DOCX_MEMBERS = 1000
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_DOCX_COMPRESSION_RATIO = 100.0

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocxParser(BaseParser):
    """
    Zero-dependency DOCX Parser using Python standard library zipfile and xml.etree.ElementTree.
    Extracts paragraphs, headings, and table cells in natural reading order.
    Includes built-in ZIP Bomb protection.
    """

    def parse(self, file_path: Path) -> ParsedDocument:
        if not file_path.exists():
            raise FileNotFoundError(f"DOCX file not found at {file_path}")

        # 1. Inspect ZIP archive integrity and guard against ZIP Bomb
        if not zipfile.is_zipfile(file_path):
            raise ValueError(f"文件不是合法的 DOCX / ZIP 归档格式: {file_path.name}")

        with zipfile.ZipFile(file_path, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > MAX_DOCX_MEMBERS:
                raise ValueError(
                    f"安全拦截：DOCX 压缩包内文件数量超过安全阈值 ({len(infolist)} > {MAX_DOCX_MEMBERS})"
                )

            total_uncompressed = 0
            for info in infolist:
                total_uncompressed += info.file_size
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > MAX_DOCX_COMPRESSION_RATIO and info.file_size > 1024 * 100:
                        raise ValueError(
                            f"安全拦截：检测到异常压缩比 ({ratio:.1f}x > {MAX_DOCX_COMPRESSION_RATIO}x)，疑似 ZIP Bomb 攻击"
                        )

            if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"安全拦截：DOCX 解压后总体积超过上限 ({total_uncompressed / 1024 / 1024:.1f}MB > {MAX_DOCX_UNCOMPRESSED_BYTES / 1024 / 1024:.0f}MB)"
                )

            # 2. Check for required word/document.xml
            if "word/document.xml" not in zf.namelist():
                raise ValueError("DOCX 结构损坏：未找到必需的 word/document.xml 文件")

            try:
                xml_content = zf.read("word/document.xml")
            except Exception as exc:
                raise ValueError(f"读取 DOCX 正文数据失败: {str(exc)}") from exc

        # 3. Parse XML document
        try:
            root = ET.fromstring(xml_content)
        except Exception as exc:
            raise ValueError(f"DOCX 正文 XML 解析错误: {str(exc)}") from exc

        body = root.find(f"{W_NS}body")
        if body is None:
            return ParsedDocument(
                file_name=file_path.name,
                file_type="docx",
                total_pages=0,
                pages=[],
            )

        # 4. Extract paragraphs and tables in sequence
        blocks: List[Dict[str, Any]] = []
        current_heading_path: List[str] = []

        for child in body:
            tag = child.tag
            if tag == f"{W_NS}p":
                # Paragraph
                p_text, is_heading, heading_level = self._parse_paragraph(child)
                if p_text:
                    if is_heading:
                        # Update heading breadcrumbs
                        while len(current_heading_path) >= heading_level:
                            current_heading_path.pop()
                        current_heading_path.append(p_text)

                    blocks.append({
                        "type": "heading" if is_heading else "paragraph",
                        "text": p_text,
                        "heading_path": list(current_heading_path),
                    })
            elif tag == f"{W_NS}tbl":
                # Table
                table_text = self._parse_table(child)
                if table_text:
                    blocks.append({
                        "type": "table",
                        "text": table_text,
                        "heading_path": list(current_heading_path),
                    })

        if not blocks:
            # Handle empty document
            return ParsedDocument(
                file_name=file_path.name,
                file_type="docx",
                total_pages=1,
                pages=[ParsedPage(page_number=1, text="", metadata={"block_type": "empty"})],
            )

        # 5. Group blocks into logical pages/sections (without fabricating PDF page numbers)
        # Each logical block or section of text is given a block index (1-based)
        pages: List[ParsedPage] = []
        current_page_text: List[str] = []
        current_page_char_count = 0
        current_heading = ""
        current_block_type = "paragraph"
        section_idx = 1

        for blk in blocks:
            b_text = blk["text"]
            b_type = blk["type"]
            h_path = blk.get("heading_path", [])
            heading_str = " > ".join(h_path) if h_path else ""

            # If block is heading or cumulative characters exceed ~800, start a new logical section
            if (b_type == "heading" and current_page_text) or (current_page_char_count >= 800 and current_page_text):
                full_sec_text = "\n\n".join(current_page_text).strip()
                if full_sec_text:
                    pages.append(
                        ParsedPage(
                            page_number=section_idx,
                            text=full_sec_text,
                            metadata={
                                "block_type": current_block_type,
                                "heading_path": current_heading,
                                "is_docx_section": True,
                            },
                        )
                    )
                    section_idx += 1
                current_page_text = []
                current_page_char_count = 0

            current_page_text.append(b_text)
            current_page_char_count += len(b_text)
            current_heading = heading_str
            current_block_type = b_type

        if current_page_text:
            full_sec_text = "\n\n".join(current_page_text).strip()
            if full_sec_text:
                pages.append(
                    ParsedPage(
                        page_number=section_idx,
                        text=full_sec_text,
                        metadata={
                            "block_type": current_block_type,
                            "heading_path": current_heading,
                            "is_docx_section": True,
                        },
                    )
                )

        if not pages:
            pages = [ParsedPage(page_number=1, text="", metadata={"is_docx_section": True})]

        return ParsedDocument(
            file_name=file_path.name,
            file_type="docx",
            total_pages=len(pages),
            pages=pages,
        )

    def _parse_paragraph(self, p_element: ET.Element) -> tuple[str, bool, int]:
        """Extract text from paragraph element and detect heading level."""
        # Check paragraph style
        is_heading = False
        heading_level = 1
        pPr = p_element.find(f"{W_NS}pPr")
        if pPr is not None:
            pStyle = pPr.find(f"{W_NS}pStyle")
            if pStyle is not None:
                val = pStyle.get(f"{W_NS}val", "")
                val_lower = val.lower()
                if "heading" in val_lower or "title" in val_lower or val.isdigit():
                    is_heading = True
                    for digit in "123456789":
                        if digit in val:
                            heading_level = int(digit)
                            break

        # Collect text runs
        text_parts: List[str] = []
        for node in p_element.iter():
            if node.tag == f"{W_NS}t" and node.text:
                text_parts.append(node.text)
            elif node.tag == f"{W_NS}tab":
                text_parts.append("\t")
            elif node.tag == f"{W_NS}br":
                text_parts.append("\n")

        p_text = "".join(text_parts).strip()
        return p_text, is_heading, heading_level

    def _parse_table(self, tbl_element: ET.Element) -> str:
        """Parse table element into clean markdown-style table text."""
        rows: List[List[str]] = []
        for tr in tbl_element.findall(f"{W_NS}tr"):
            row_cells: List[str] = []
            for tc in tr.findall(f"{W_NS}tc"):
                cell_text_parts = []
                for node in tc.iter(f"{W_NS}t"):
                    if node.text:
                        cell_text_parts.append(node.text)
                cell_str = " ".join("".join(cell_text_parts).split())
                row_cells.append(cell_str or "-")
            if row_cells:
                rows.append(row_cells)

        if not rows:
            return ""

        # Format as markdown table
        lines = []
        header = rows[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in rows[1:]:
            # Pad or trim row to match header columns count
            padded = row[: len(header)] + ["-"] * max(0, len(header) - len(row))
            lines.append("| " + " | ".join(padded) + " |")

        return "\n".join(lines)
