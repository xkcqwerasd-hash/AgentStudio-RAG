from dataclasses import dataclass
from typing import List


@dataclass
class ChunkItem:
    chunk_index: int
    content: str
    page_number: int
    char_length: int


class RecursiveCharacterTextSplitter:
    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        separators: List[str] | None = None,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or [
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            ". ",
            "! ",
            "? ",
            "；",
            "; ",
            "，",
            ", ",
            " ",
            "",
        ]

    def split_text(self, text: str) -> List[str]:
        """Split a single text string recursively based on separators."""
        text = text.strip()
        if not text:
            return []

        if len(text) <= self.chunk_size:
            return [text]

        return self._split(text, self.separators)

    def _split(self, text: str, separators: List[str]) -> List[str]:
        final_chunks: List[str] = []
        separator = separators[-1]
        new_separators = []

        for i, _s in enumerate(separators):
            if _s == "":
                separator = _s
                break
            if _s in text:
                separator = _s
                new_separators = separators[i + 1 :]
                break

        splits = text.split(separator) if separator else list(text)

        good_splits: List[str] = []
        for s in splits:
            if not s:
                continue
            if len(s) < self.chunk_size:
                good_splits.append(s)
            else:
                if good_splits:
                    merged = self._merge_splits(good_splits, separator)
                    final_chunks.extend(merged)
                    good_splits = []
                if not new_separators:
                    # Forced character slice
                    for start in range(0, len(s), self.chunk_size - self.chunk_overlap):
                        final_chunks.append(s[start : start + self.chunk_size])
                else:
                    other_info = self._split(s, new_separators)
                    final_chunks.extend(other_info)

        if good_splits:
            merged = self._merge_splits(good_splits, separator)
            final_chunks.extend(merged)

        return [c.strip() for c in final_chunks if c.strip()]

    def _merge_splits(self, splits: List[str], separator: str) -> List[str]:
        docs: List[str] = []
        current_doc: List[str] = []
        total = 0

        for d in splits:
            _len = len(d)
            if total + _len + (len(separator) if current_doc else 0) > self.chunk_size:
                if total > 0:
                    doc = separator.join(current_doc)
                    if doc:
                        docs.append(doc)
                    # Keep overlap
                    while total > self.chunk_overlap or (
                        total + _len + (len(separator) if current_doc else 0) > self.chunk_size
                        and total > 0
                    ):
                        popped = current_doc.pop(0)
                        total -= len(popped) + (len(separator) if current_doc else 0)
            current_doc.append(d)
            total += _len + (len(separator) if len(current_doc) > 1 else 0)

        doc = separator.join(current_doc)
        if doc:
            docs.append(doc)
        return docs
