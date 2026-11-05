from pathlib import Path
import pymupdf

from app.ingestion.parsers.base import BaseParser, ParsedDocument, ParsedPage


class PDFParser(BaseParser):
    def parse(self, file_path: Path) -> ParsedDocument:
        if not file_path.exists():
            raise FileNotFoundError(f"PDF file not found at {file_path}")

        doc = pymupdf.open(file_path)
        pages: list[ParsedPage] = []

        try:
            for page_index in range(len(doc)):
                page = doc[page_index]
                text = page.get_text("text").strip()
                # If page has content, record it
                pages.append(
                    ParsedPage(
                        page_number=page_index + 1,
                        text=text,
                        metadata={
                            "rect": [page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1],
                        },
                    )
                )

            return ParsedDocument(
                file_name=file_path.name,
                file_type="pdf",
                total_pages=len(doc),
                pages=pages,
            )
        finally:
            doc.close()
