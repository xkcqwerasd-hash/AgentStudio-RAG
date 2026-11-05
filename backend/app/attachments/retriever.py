import hashlib
import math
import threading
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from app.attachments.chunker import AttachmentChunk
from app.attachments.evidence import AttachmentScoredChunk, chunk_to_scored_chunk
from app.core.logging import logger
from app.retrieval.bm25_retriever import TechnicalBM25Tokenizer


class EphemeralBM25Index:
    """
    In-memory Conversation-scoped BM25 Index for temporary attachments.
    - Completely isolated per conversation_id.
    - Operates entirely in memory; never writes to permanent KnowledgeBase tables.
    - Lucene-style BM25 formulation with positive IDF.
    """

    def __init__(
        self,
        conversation_id: str,
        k1: float = 1.5,
        b: float = 0.75,
    ):
        self.conversation_id = conversation_id
        self.k1 = k1
        self.b = b

        self.chunks: List[AttachmentChunk] = []
        self.doc_lens: List[int] = []
        self.avg_doc_len: float = 0.0
        self.total_chunks: int = 0
        self.df: Counter = Counter()
        self.inverted_index: Dict[str, List[Tuple[int, int]]] = {}
        self.version_hash: str = ""

    def build(self, chunks: List[AttachmentChunk]) -> None:
        """
        Build in-memory BM25 index from a list of AttachmentChunk objects.
        """
        self.chunks = chunks
        self.total_chunks = len(chunks)
        self.doc_lens = []
        self.df = Counter()
        self.inverted_index = {}

        if self.total_chunks == 0:
            self.avg_doc_len = 0.0
            self.version_hash = ""
            return

        # Compute deterministic version hash based on chunk IDs and contents
        hasher = hashlib.sha256()
        for c in chunks:
            hasher.update(c.chunk_id.encode("utf-8"))
            hasher.update(c.content.encode("utf-8"))
        self.version_hash = hasher.hexdigest()

        for idx, chunk in enumerate(chunks):
            tokens = TechnicalBM25Tokenizer.tokenize(chunk.content)
            tf = Counter(tokens)
            doc_len = len(tokens)
            self.doc_lens.append(doc_len)

            for term, count in tf.items():
                self.df[term] += 1
                if term not in self.inverted_index:
                    self.inverted_index[term] = []
                self.inverted_index[term].append((idx, count))

        self.avg_doc_len = (
            sum(self.doc_lens) / self.total_chunks if self.total_chunks > 0 else 0.0
        )

    def search(
        self,
        query: str,
        top_k: int = 4,
        deduplicate_content: bool = True,
    ) -> List[AttachmentScoredChunk]:
        """
        Perform BM25 search across all attachment chunks of this conversation.
        Returns ranked AttachmentScoredChunk items with content deduplication support.
        """
        if self.total_chunks == 0 or not query.strip():
            return []

        query_tokens = TechnicalBM25Tokenizer.tokenize(query)
        if not query_tokens:
            return []

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
                len_norm = (
                    1.0 - self.b + self.b * (dl / self.avg_doc_len)
                    if self.avg_doc_len > 0
                    else 1.0
                )
                tf_norm = (tf * (self.k1 + 1.0)) / (tf + self.k1 * len_norm)
                scores[doc_idx] += idf * tf_norm

        if not scores:
            return []

        # Rank all scored documents
        ranked = scores.most_common()

        results: List[AttachmentScoredChunk] = []
        seen_content: Set[str] = set()

        for rank_idx, (doc_idx, score) in enumerate(ranked, start=1):
            if score <= 1e-6:
                continue

            chunk = self.chunks[doc_idx]
            cleaned_content = chunk.content.strip()

            # Content deduplication check
            if deduplicate_content:
                if cleaned_content in seen_content:
                    continue
                seen_content.add(cleaned_content)

            scored = chunk_to_scored_chunk(
                chunk=chunk,
                similarity_score=round(float(score), 4),
                bm25_score=float(score),
                bm25_rank=rank_idx,
            )
            results.append(scored)

            if len(results) >= top_k:
                break

        return results


class AttachmentIndexManager:
    """
    Thread-safe manager for conversation-scoped ephemeral attachment indexes.
    Ensures:
    - Complete cross-conversation isolation.
    - Zero index leakage across different sessions.
    - Automatic cache invalidation upon attachment or conversation deletion.
    """

    def __init__(self):
        self._indexes: Dict[str, EphemeralBM25Index] = {}
        self._lock = threading.RLock()

    def get_or_build_index(
        self,
        conversation_id: str,
        chunks: List[AttachmentChunk],
    ) -> EphemeralBM25Index:
        """
        Retrieve cached index if up-to-date, or construct new index for conversation.
        """
        with self._lock:
            # Check if existing index exists and has matching chunks
            existing = self._indexes.get(conversation_id)
            if existing is not None:
                # Check if total chunks match and version hash matches
                hasher = hashlib.sha256()
                for c in chunks:
                    hasher.update(c.chunk_id.encode("utf-8"))
                    hasher.update(c.content.encode("utf-8"))
                current_hash = hasher.hexdigest() if chunks else ""

                if existing.version_hash == current_hash and existing.total_chunks == len(chunks):
                    return existing

            # Rebuild index
            idx = EphemeralBM25Index(conversation_id=conversation_id)
            idx.build(chunks)
            self._indexes[conversation_id] = idx
            return idx

    def get_index(self, conversation_id: str) -> Optional[EphemeralBM25Index]:
        """Get index for conversation if present."""
        with self._lock:
            return self._indexes.get(conversation_id)

    def invalidate_conversation(self, conversation_id: str) -> None:
        """
        Invalidate and remove cached index for a conversation.
        Called on attachment upload, deletion, or conversation removal.
        """
        with self._lock:
            if conversation_id in self._indexes:
                del self._indexes[conversation_id]
                logger.debug(f"Invalidated attachment index for conversation {conversation_id}")

    def clear(self) -> None:
        """Clear all in-memory indexes."""
        with self._lock:
            self._indexes.clear()

    @property
    def cached_conversation_count(self) -> int:
        with self._lock:
            return len(self._indexes)


# Global singleton instance
attachment_index_manager = AttachmentIndexManager()
