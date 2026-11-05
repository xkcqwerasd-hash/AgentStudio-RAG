from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class ParsedPage:
    page_number: int  # 1-indexed
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    file_name: str
    file_type: str
    total_pages: int
    pages: List[ParsedPage]


class BaseParser(ABC):
    @abstractmethod
    def parse(self, file_path: Path) -> ParsedDocument:
        """Parse file and return structured document with pages and metadata."""
        pass
