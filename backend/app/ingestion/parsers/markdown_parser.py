import re
from pathlib import Path
from typing import List

from app.ingestion.parsers.base import BaseParser, ParsedDocument, ParsedPage
from app.ingestion.parsers.text_parser import TextParser


class MarkdownParser(TextParser):
    """
    Structure-aware Markdown Parser.
    Preserves heading hierarchies, code blocks, tables, and paragraphs.
    Splits into logical sections/pages by major headings without losing structure.
    """

    def parse(self, file_path: Path) -> ParsedDocument:
        if not file_path.exists():
            raise FileNotFoundError(f"Markdown file not found at {file_path}")

        # Try multiple encodings
        content = ""
        used_encoding = "utf-8"
        for encoding in ["utf-8", "utf-8-sig", "gb18030", "gbk"]:
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    content = f.read()
                used_encoding = encoding
                break
            except UnicodeDecodeError:
                continue

        lines = content.splitlines()
        if not lines or not content.strip():
            return ParsedDocument(
                file_name=file_path.name,
                file_type="markdown",
                total_pages=1,
                pages=[ParsedPage(page_number=1, text="", metadata={"encoding": used_encoding})],
            )

        # Segment by H1 (#) or H2 (##) headings to create logical readable sections
        pages: List[ParsedPage] = []
        current_section_lines: List[str] = []
        current_heading = "正文"
        current_heading_path: List[str] = []
        in_code_block = False
        section_idx = 1

        for line in lines:
            stripped = line.strip()

            # Track fenced code blocks
            if stripped.startswith("```"):
                in_code_block = not in_code_block

            # Check if this line is an H1 or H2 heading outside code block
            is_major_heading = False
            heading_level = 0
            if not in_code_block and stripped.startswith("#"):
                match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
                if match:
                    is_major_heading = True
                    heading_level = len(match.group(1))
                    heading_title = match.group(2).strip()

            if is_major_heading:
                # If we have accumulated enough content, flush previous section as a logical page
                accumulated_text = "\n".join(current_section_lines).strip()
                if accumulated_text and (len(accumulated_text) > 300 or heading_level <= 2):
                    pages.append(
                        ParsedPage(
                            page_number=section_idx,
                            text=accumulated_text,
                            metadata={
                                "heading": current_heading,
                                "heading_path": " > ".join(current_heading_path) or current_heading,
                                "encoding": used_encoding,
                                "is_markdown_section": True,
                            },
                        )
                    )
                    section_idx += 1
                    current_section_lines = []

                # Update heading breadcrumbs
                while len(current_heading_path) >= heading_level:
                    current_heading_path.pop()
                current_heading_path.append(heading_title)
                current_heading = heading_title

            current_section_lines.append(line)

        # Flush remaining lines
        remaining_text = "\n".join(current_section_lines).strip()
        if remaining_text:
            pages.append(
                ParsedPage(
                    page_number=section_idx,
                    text=remaining_text,
                    metadata={
                        "heading": current_heading,
                        "heading_path": " > ".join(current_heading_path) or current_heading,
                        "encoding": used_encoding,
                        "is_markdown_section": True,
                    },
                )
            )

        if not pages:
            pages = [ParsedPage(page_number=1, text=content.strip(), metadata={"encoding": used_encoding})]

        return ParsedDocument(
            file_name=file_path.name,
            file_type="markdown",
            total_pages=len(pages),
            pages=pages,
        )
