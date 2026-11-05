import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import jieba
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.retrieval.vector_store import ScoredChunk


class TechnicalBM25Tokenizer:
    """
    Advanced Tokenizer tailored for mixed Chinese, English, technical symbols,
    underscored parameters, camelCase names, and hyphenated identifiers.
    """

    STOP_WORDS: Set[str] = {
        # Chinese common stop words and prepositions
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
        "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
        "自己", "这", "那", "与", "为", "及", "等", "之", "于", "或", "请", "问", "什么",
        "怎样", "如何", "哪些", "多少", "给", "但", "而", "对", "把", "把", "被", "从",
        # English common stop words
        "a", "an", "the", "is", "are", "was", "were", "in", "on", "at", "to", "for",
        "of", "with", "by", "about", "as", "into", "like", "through", "after", "over",
        "between", "out", "against", "during", "without", "before", "under", "around",
        "among", "it", "its", "they", "them", "their", "this", "that", "these", "those",
    }

    # Matches technical compound identifiers: e.g. min_idle, half_open, bge-small, timeout_ms, v1.5
    COMPOUND_REGEX = re.compile(r"[a-zA-Z0-9]+(?:[_\-\.][a-zA-Z0-9]+)+")

    # Matches camelCase transitions: e.g. ModelHealthStore -> Model, Health, Store
    CAMEL_REGEX = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z][a-z0-9]|\b)")

    # Pure whitespace and punctuation
    PUNCT_REGEX = re.compile(r"^[\s\W_]+$")

    @classmethod
    def tokenize(cls, text: str) -> List[str]:
        if not text:
            return []

        # 0. Clean soft PDF line wraps across letters (e.g. "T\nLS" -> "TLS")
        cleaned_text = re.sub(r"([A-Za-z0-9])\s*\n\s*([A-Za-z0-9])", r"\1\2", text)

        tokens: List[str] = []

        # 1. Technical compounds preservation (e.g. min_idle, BGE-small, HALF_OPEN, timeout_ms)
        raw_compounds = cls.COMPOUND_REGEX.findall(cleaned_text)
        for comp in raw_compounds:
            comp_lower = comp.lower()
            tokens.append(comp_lower)
            # Decompose into atomic sub-words: e.g. min_idle -> min, idle
            parts = re.split(r"[_\-\.]", comp_lower)
            for p in parts:
                if p and p not in cls.STOP_WORDS and len(p) >= 1:
                    tokens.append(p)

        # 2. CamelCase decomposition (e.g. ModelHealthStore -> modelhealthstore, model, health, store)
        words = re.findall(r"[a-zA-Z0-9]+", cleaned_text)
        for w in words:
            if any(c.isupper() for c in w) and any(c.islower() for c in w):
                tokens.append(w.lower())
                sub_parts = cls.CAMEL_REGEX.findall(w)
                if len(sub_parts) > 1:
                    for sp in sub_parts:
                        spl = sp.lower()
                        if spl not in cls.STOP_WORDS and len(spl) >= 1:
                            tokens.append(spl)

        # 3. Jieba segmentation for Chinese text and general vocabulary
        jieba_cuts = jieba.cut_for_search(cleaned_text)
        for jw in jieba_cuts:
            j_clean = jw.strip().lower()
            if not j_clean:
                continue
            if j_clean in cls.STOP_WORDS:
                continue
            if cls.PUNCT_REGEX.match(j_clean):
                continue
            tokens.append(j_clean)

        return tokens


class BM25KBIndex:
    """
    In-memory BM25 index for a single Knowledge Base, using Lucene's BM25 formula.
    Features:
    - Guaranteed positive IDF: log(1 + (N - df + 0.5) / (df + 0.5))
    - Sub-millisecond term-lookup via inverted index
    - Serializable to JSON for disk persistence
    """

    def __init__(self, knowledge_base_id: str, k1: float = 1.5, b: float = 0.75):
        self.knowledge_base_id = knowledge_base_id
        self.k1 = k1
        self.b = b

        self.chunks_meta: List[Dict[str, Any]] = []  # list of chunk dicts
        self.doc_lens: List[int] = []
        self.avg_doc_len: float = 0.0
        self.total_chunks: int = 0
        self.df: Dict[str, int] = Counter()  # term -> document frequency
        # Inverted index: term -> list of (doc_index, term_frequency)
        self.inverted_index: Dict[str, List[Tuple[int, int]]] = {}

    def build_from_chunks(self, chunks: List[Dict[str, Any]]) -> None:
        """
        Build BM25 index from a list of chunk metadata dictionaries.
        Each chunk dict must have: chunk_id, document_id, document_name, page_number, chunk_index, content.
        """
        self.chunks_meta = chunks
        self.total_chunks = len(chunks)
        self.doc_lens = []
        self.df = Counter()
        self.inverted_index = {}

        if self.total_chunks == 0:
            self.avg_doc_len = 0.0
            return

        for idx, c in enumerate(chunks):
            content = c.get("content", "")
            tokens = TechnicalBM25Tokenizer.tokenize(content)
            tf = Counter(tokens)
            doc_len = len(tokens)
            self.doc_lens.append(doc_len)

            for term, count in tf.items():
                self.df[term] += 1
                if term not in self.inverted_index:
                    self.inverted_index[term] = []
                self.inverted_index[term].append((idx, count))

        self.avg_doc_len = sum(self.doc_lens) / self.total_chunks if self.total_chunks > 0 else 0.0

    def search(self, query: str, top_k: int = 4) -> List[ScoredChunk]:
        """
        Execute BM25 query and return ScoredChunk items matching query terms.
        """
        if self.total_chunks == 0:
            return []

        query_tokens = TechnicalBM25Tokenizer.tokenize(query)
        if not query_tokens:
            return []

        # Count frequencies of terms in the query
        q_tf = Counter(query_tokens)
        scores: Dict[int, float] = Counter()

        for term, q_count in q_tf.items():
            if term not in self.inverted_index:
                continue

            n = self.df[term]
            # Lucene-style positive IDF
            idf = math.log(1.0 + (self.total_chunks - n + 0.5) / (n + 0.5))

            postings = self.inverted_index[term]
            for doc_idx, tf in postings:
                dl = self.doc_lens[doc_idx]
                len_norm = 1.0 - self.b + self.b * (dl / self.avg_doc_len) if self.avg_doc_len > 0 else 1.0
                tf_norm = (tf * (self.k1 + 1.0)) / (tf + self.k1 * len_norm)
                scores[doc_idx] += idf * tf_norm

        if not scores:
            return []

        # Rank documents
        ranked = scores.most_common(top_k)

        results: List[ScoredChunk] = []
        for doc_idx, score in ranked:
            if score <= 1e-6:
                continue
            meta = self.chunks_meta[doc_idx]
            results.append(
                ScoredChunk(
                    chunk_id=meta["chunk_id"],
                    document_id=meta["document_id"],
                    document_name=meta["document_name"],
                    page_number=meta["page_number"],
                    chunk_index=meta["chunk_index"],
                    content=meta["content"],
                    similarity_score=round(float(score), 4),
                )
            )

        return results

    def to_dict(self) -> Dict[str, Any]:
        """Export index to serializable dictionary."""
        return {
            "knowledge_base_id": self.knowledge_base_id,
            "k1": self.k1,
            "b": self.b,
            "total_chunks": self.total_chunks,
            "avg_doc_len": self.avg_doc_len,
            "doc_lens": self.doc_lens,
            "chunks_meta": self.chunks_meta,
            "df": dict(self.df),
            "inverted_index": {k: v for k, v in self.inverted_index.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BM25KBIndex":
        """Instantiate index from serialized dictionary."""
        kb_id = data.get("knowledge_base_id", "")
        idx = cls(kb_id, k1=data.get("k1", 1.5), b=data.get("b", 0.75))
        idx.total_chunks = data.get("total_chunks", 0)
        idx.avg_doc_len = data.get("avg_doc_len", 0.0)
        idx.doc_lens = data.get("doc_lens", [])
        idx.chunks_meta = data.get("chunks_meta", [])
        idx.df = Counter(data.get("df", {}))
        # Ensure postings are tuples (doc_idx, tf)
        raw_inv = data.get("inverted_index", {})
        idx.inverted_index = {
            k: [tuple(item) for item in postings]
            for k, postings in raw_inv.items()
        }
        return idx


class BM25IndexManager:
    """
    Manages in-memory and on-disk BM25 indexes per Knowledge Base.
    Ensures:
    - Lazy loading on demand from DB or disk cache
    - Fast invalidation on document addition/deletion
    - Crash recovery across service restarts
    - Multi-tenant strict KB isolation
    """

    def __init__(self, cache_dir: Optional[str] = None):
        if cache_dir is None:
            cache_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "data",
                "storage",
                "bm25",
            )
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._indices: Dict[str, BM25KBIndex] = {}

    def _get_cache_path(self, kb_id: str) -> Path:
        return self.cache_dir / f"{kb_id}.json"

    def invalidate(self, kb_id: str) -> None:
        """Invalidate in-memory and disk cache when chunks change."""
        if kb_id in self._indices:
            del self._indices[kb_id]
        p = self._get_cache_path(kb_id)
        if p.exists():
            try:
                p.unlink()
            except Exception as e:
                logger.warning(f"Failed to remove BM25 disk cache {p}: {e}")

    def remove_knowledge_base(self, kb_id: str) -> None:
        """Clean up index when knowledge base is deleted."""
        self.invalidate(kb_id)

    async def get_or_build_index(
        self,
        knowledge_base_id: str,
        db: AsyncSession,
        force_rebuild: bool = False,
    ) -> BM25KBIndex:
        """
        Fetch cached index from memory or disk, or build from database chunks.
        """
        if not force_rebuild and knowledge_base_id in self._indices:
            return self._indices[knowledge_base_id]

        cache_path = self._get_cache_path(knowledge_base_id)

        # Try loading from disk cache
        if not force_rebuild and cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                index = BM25KBIndex.from_dict(data)
                self._indices[knowledge_base_id] = index
                return index
            except Exception as exc:
                logger.warning(f"Corrupted BM25 disk cache for KB {knowledge_base_id}: {exc}, rebuilding...")

        # Build fresh index from SQLite DocumentChunk table
        stmt = (
            select(DocumentChunk, Document.file_name)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(
                DocumentChunk.knowledge_base_id == knowledge_base_id,
                Document.status == "COMPLETED",
            )
        )
        res = await db.execute(stmt)
        rows = res.all()

        chunk_list = []
        for chunk, doc_name in rows:
            chunk_list.append({
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "document_name": doc_name,
                "page_number": chunk.page_number,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
            })

        index = BM25KBIndex(knowledge_base_id)
        index.build_from_chunks(chunk_list)
        self._indices[knowledge_base_id] = index

        # Persist to disk cache
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(index.to_dict(), f, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"Failed to persist BM25 index to {cache_path}: {exc}")

        return index


# Global singleton instance
bm25_index_manager = BM25IndexManager()


class BM25Retriever:
    """
    Independent Sparse Retriever implementing BM25Okapi / Lucene BM25.
    Returns results strictly compatible with VectorRetriever (List[ScoredChunk]).
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.manager = bm25_index_manager

    async def search(
        self,
        knowledge_base_id: str,
        query: str,
        top_k: int = 4,
    ) -> List[ScoredChunk]:
        """
        Execute BM25 sparse keyword search on the given knowledge base.
        """
        index = await self.manager.get_or_build_index(knowledge_base_id, self.db)
        return index.search(query=query, top_k=top_k)
