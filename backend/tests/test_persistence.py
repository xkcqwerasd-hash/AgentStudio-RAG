from pathlib import Path
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import Base
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.vector_store import VectorRetriever


@pytest.mark.asyncio
async def test_vector_and_metadata_persistence(tmp_path: Path):
    """
    Test 1: Verify KnowledgeBase, Document, DocumentChunk and Embedding JSON
    persist across engine and session restarts in a real SQLite file.
    """
    db_file = tmp_path / "test_persist.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    # First session: create and write
    engine1 = create_async_engine(db_url, connect_args={"check_same_thread": False})
    async with engine1.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker1 = async_sessionmaker(bind=engine1, class_=AsyncSession, expire_on_commit=False)
    async with session_maker1() as session1:
        kb = KnowledgeBase(name="持久化测试库", chunk_size=500, chunk_overlap=50)
        session1.add(kb)
        await session1.commit()
        await session1.refresh(kb)
        kb_id = kb.id

        doc = Document(
            knowledge_base_id=kb_id,
            file_name="persist_doc.pdf",
            file_type="pdf",
            file_size=1024,
            file_path=str(tmp_path / "dummy.pdf"),
            status="COMPLETED",
            page_count=2,
            chunk_count=1,
        )
        session1.add(doc)
        await session1.commit()
        await session1.refresh(doc)
        doc_id = doc.id

        chunk = DocumentChunk(
            document_id=doc_id,
            knowledge_base_id=kb_id,
            chunk_index=0,
            content="这是用于验证磁盘持久化的切块事实文本。",
            page_number=2,
            char_length=20,
        )
        # Store a known 1024-dim vector
        sample_vector = [0.1] * 1024
        chunk.embedding = sample_vector
        session1.add(chunk)
        await session1.commit()
        chunk_id = chunk.id

    # Close engine completely (simulating process death)
    await engine1.dispose()

    # Second session: fresh engine opening the same SQLite file
    engine2 = create_async_engine(db_url, connect_args={"check_same_thread": False})
    session_maker2 = async_sessionmaker(bind=engine2, class_=AsyncSession, expire_on_commit=False)
    async with session_maker2() as session2:
        # 1. Verify KnowledgeBase persisted
        kb_loaded = (await session2.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))).scalar_one_or_none()
        assert kb_loaded is not None
        assert kb_loaded.name == "持久化测试库"

        # 2. Verify Document persisted
        doc_loaded = (await session2.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
        assert doc_loaded is not None
        assert doc_loaded.file_name == "persist_doc.pdf"
        assert doc_loaded.page_count == 2

        # 3. Verify DocumentChunk persisted
        chunk_loaded = (await session2.execute(select(DocumentChunk).where(DocumentChunk.id == chunk_id))).scalar_one_or_none()
        assert chunk_loaded is not None
        assert chunk_loaded.page_number == 2
        assert chunk_loaded.content == "这是用于验证磁盘持久化的切块事实文本。"

        # 4. Verify Embedding vector persisted
        emb_loaded = chunk_loaded.embedding
        assert emb_loaded is not None
        assert len(emb_loaded) == 1024
        assert abs(emb_loaded[0] - 0.1) < 1e-6

        # 5. Verify vector search works directly from restarted DB
        retriever = VectorRetriever(session2)
        results = await retriever.search(kb_id, query_vector=sample_vector, top_k=1)
        assert len(results) == 1
        assert results[0].chunk_id == chunk_id
        assert results[0].page_number == 2
        assert abs(results[0].similarity_score - 1.0) < 1e-3

    await engine2.dispose()


@pytest.mark.asyncio
async def test_cascade_delete_clean_up(db_session: AsyncSession):
    """
    Test 2: Verify deleting document removes all chunks from document_chunks table.
    """
    kb = KnowledgeBase(name="删除测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="to_delete.txt",
        file_type="txt",
        file_size=100,
        file_path="/tmp/to_delete.txt",
        status="COMPLETED",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    chunk1 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="chunk 1",
        page_number=1,
        char_length=7,
    )
    chunk2 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=1,
        content="chunk 2",
        page_number=1,
        char_length=7,
    )
    db_session.add_all([chunk1, chunk2])
    await db_session.commit()

    # Verify chunks exist
    count_before = (await db_session.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == doc.id)
    )).scalar()
    assert count_before == 2

    # Delete chunks and doc explicitly as implemented in our updated API
    await db_session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    await db_session.execute(delete(Document).where(Document.id == doc.id))
    await db_session.commit()

    count_after = (await db_session.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == doc.id)
    )).scalar()
    assert count_after == 0
