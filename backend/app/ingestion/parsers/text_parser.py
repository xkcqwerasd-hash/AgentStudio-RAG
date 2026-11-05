from pathlib import Path
from app.ingestion.parsers.base import BaseParser, ParsedDocument, ParsedPage


class TextParser(BaseParser):
    def __init__(self, file_type: str = "txt"):
        self.file_type = file_type

    def parse(self, file_path: Path) -> ParsedDocument:
        if not file_path.exists():
            raise FileNotFoundError(f"File not found at {file_path}")

        # Try UTF-8 with fallback to gbk for Chinese documents
        content = ""
        for encoding in ["utf-8", "utf-8-sig", "gb18030", "gbk"]:
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue

        # For text / markdown, page 1 represents the entire document
        pages = [
            ParsedPage(
                page_number=1,
                text=content.strip(),
                metadata={"encoding": encoding},
            )
        ]

        return ParsedDocument(
            file_name=file_path.name,
            file_type=self.file_type,
            total_pages=1,
            pages=pages,
        )
