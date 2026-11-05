from pathlib import Path
from app.core.exceptions import UnsupportedFileTypeError
from app.ingestion.parsers.base import BaseParser
from app.ingestion.parsers.docx_parser import DocxParser
from app.ingestion.parsers.markdown_parser import MarkdownParser
from app.ingestion.parsers.pdf_parser import PDFParser
from app.ingestion.parsers.text_parser import TextParser


class ParserFactory:
    _registry = {
        ".pdf": PDFParser,
        ".docx": DocxParser,
        ".md": MarkdownParser,
        ".markdown": MarkdownParser,
        ".txt": lambda: TextParser(file_type="txt"),
    }

    @classmethod
    def get_parser(cls, file_path: Path) -> BaseParser:
        ext = file_path.suffix.lower()
        parser_cls_or_func = cls._registry.get(ext)
        if not parser_cls_or_func:
            raise UnsupportedFileTypeError(ext)
        return parser_cls_or_func()
