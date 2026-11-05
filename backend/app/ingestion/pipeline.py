from pathlib import Path
from typing import List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.ingestion.parsers.factory import ParserFactory
from app.ingestion.splitters.recursive_splitter import RecursiveCharacterTextSplitter
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase


class IngestionPipeline:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def process_document(self, document_id: str) -> Document:
        """
        Execute full ingestion lifecycle: Parse -> Chunk -> Embed -> Index
        """
        stmt = select(Document).where(Document.id == document_id)
        result = await self.db.execute(stmt)
        doc = result.scalar_one_or_none()
        if not doc:
            raise ValueError(f"Document {document_id} not found.")

        # Get knowledge base settings
        kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
        kb_result = await self.db.execute(kb_stmt)
        kb = kb_result.scalar_one_or_none()
        chunk_size = kb.chunk_size if kb else 500
        chunk_overlap = kb.chunk_overlap if kb else 50

        try:
            # 1. PARSING
            doc.status = "PARSING"
            await self.db.commit()
            await self.db.refresh(doc)
            logger.info(f"[Ingestion] Parsing document {doc.id} ({doc.file_name})...")

            file_path = Path(doc.file_path)
            parser = ParserFactory.get_parser(file_path)
            parsed_doc = parser.parse(file_path)

            doc.page_count = parsed_doc.total_pages

            # 2. CHUNKING
            doc.status = "CHUNKING"
            await self.db.commit()
            await self.db.refresh(doc)
            logger.info(f"[Ingestion] Chunking document {doc.id} (pages: {doc.page_count})...")

            splitter = RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )

            raw_chunks: List[dict] = []
            chunk_idx = 0
            for page in parsed_doc.pages:
                page_chunks = splitter.split_text(page.text)
                for chunk_text in page_chunks:
                    if not chunk_text.strip():
                        continue
                    raw_chunks.append({
                        "chunk_index": chunk_idx,
                        "content": chunk_text,
                        "page_number": page.page_number,
                        "char_length": len(chunk_text),
                        "extra_metadata": {
                            "file_name": doc.file_name,
                            "page_metadata": page.metadata,
                        },
                    })
                    chunk_idx += 1

            if not raw_chunks:
                # Handle empty document
                doc.status = "COMPLETED"
                doc.chunk_count = 0
                await self.db.commit()
                await self.db.refresh(doc)
                return doc

            # 3. EMBEDDING
            doc.status = "EMBEDDING"
            await self.db.commit()
            await self.db.refresh(doc)
            logger.info(f"[Ingestion] Embedding {len(raw_chunks)} chunks for document {doc.id}...")

            embedding_client = get_embedding_client(
                provider=kb.embedding_provider if kb else None,
                model_name=kb.embedding_model if kb else None,
            )
            chunk_texts = [c["content"] for c in raw_chunks]
            embeddings = await embedding_client.embed_documents(chunk_texts)

            # 4. INDEXING / SAVING TO DATABASE
            for i, chunk_data in enumerate(raw_chunks):
                chunk_model = DocumentChunk(
                    document_id=doc.id,
                    knowledge_base_id=doc.knowledge_base_id,
                    chunk_index=chunk_data["chunk_index"],
                    content=chunk_data["content"],
                    page_number=chunk_data["page_number"],
                    char_length=chunk_data["char_length"],
                )
                chunk_model.embedding = embeddings[i]
                chunk_model.extra_metadata = chunk_data["extra_metadata"]
                self.db.add(chunk_model)

            doc.chunk_count = len(raw_chunks)
            doc.status = "COMPLETED"
            doc.error_message = None
            await self.db.commit()
            await self.db.refresh(doc)
            logger.info(f"[Ingestion] Completed processing document {doc.id} ({doc.chunk_count} chunks)")

            # Invalidate BM25 index for this knowledge base to trigger synchronization
            try:
                from app.retrieval.bm25_retriever import bm25_index_manager
                bm25_index_manager.invalidate(doc.knowledge_base_id)
            except Exception as e:
                logger.warning(f"Failed to invalidate BM25 index for KB {doc.knowledge_base_id}: {e}")

            return doc

        except Exception as exc:
            logger.error(f"[Ingestion] Failed processing document {doc.id}: {exc}", exc_info=True)
            doc.status = "FAILED"
            doc.error_message = str(exc)
            await self.db.commit()
            await self.db.refresh(doc)
            raise
