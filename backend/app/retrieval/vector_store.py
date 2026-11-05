from dataclasses import dataclass
from typing import List, Optional
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import VectorDimensionMismatchError
from app.models.chunk import DocumentChunk
from app.models.document import Document


@dataclass
class ScoredChunk:
    chunk_id: str
    document_id: str
    document_name: str
    page_number: int
    chunk_index: int
    content: str
    similarity_score: float
    dense_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    dense_score: Optional[float] = None
    bm25_score: Optional[float] = None
    fusion_score: Optional[float] = None
    reranker_score: Optional[float] = None
    reranker_rank: Optional[int] = None
    reranker_raw_score: Optional[float] = None


class VectorRetriever:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def search(
        self,
        knowledge_base_id: str,
        query_vector: List[float],
        top_k: int = 4,
        threshold: float = 0.0,
    ) -> List[ScoredChunk]:
        """
        Perform vector similarity search across all chunks in the specified knowledge base.
        """
        # Query all chunks in this knowledge base and join document for file_name
        stmt = (
            select(DocumentChunk, Document.file_name)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(
                DocumentChunk.knowledge_base_id == knowledge_base_id,
                Document.status == "COMPLETED",
            )
        )
        result = await self.db.execute(stmt)
        rows = result.all()

        if not rows:
            return []

        q_vec = np.array(query_vector, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm < 1e-9:
            return []

        scored_items: List[ScoredChunk] = []

        for chunk, doc_name in rows:
            c_vec = chunk.embedding
            if not c_vec:
                continue
            doc_vec = np.array(c_vec, dtype=np.float32)
            # Guard against dimension mismatch (skip corrupted or mismatched chunks safely)
            if doc_vec.shape != q_vec.shape:
                continue
            d_norm = np.linalg.norm(doc_vec)
            if d_norm < 1e-9:
                continue

            # Cosine similarity
            cos_sim = float(np.dot(q_vec, doc_vec) / (q_norm * d_norm))

            if cos_sim >= threshold:
                scored_items.append(
                    ScoredChunk(
                        chunk_id=chunk.id,
                        document_id=chunk.document_id,
                        document_name=doc_name,
                        page_number=chunk.page_number,
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        similarity_score=round(cos_sim, 4),
                    )
                )

        # Sort descending by similarity score
        scored_items.sort(key=lambda x: x.similarity_score, reverse=True)
        return scored_items[:top_k]
