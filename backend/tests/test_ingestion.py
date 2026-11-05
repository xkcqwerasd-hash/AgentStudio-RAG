import tempfile
from pathlib import Path
import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.ingestion.parsers.factory import ParserFactory
from app.ingestion.parsers.pdf_parser import PDFParser
from app.ingestion.parsers.text_parser import TextParser
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.splitters.recursive_splitter import RecursiveCharacterTextSplitter
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase


def test_recursive_splitter_chinese():
    splitter = RecursiveCharacterTextSplitter(chunk_size=50, chunk_overlap=10)
    text = (
        "AgentStudio-RAG 是企业级智能知识问答系统。"
        "支持文档入库、解析、分块、向量索引、多路检索与重排序。"
        "核心引擎由 Python FastAPI 和现代检索算法驱动。"
    )
    chunks = splitter.split_text(text)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 60


def test_text_parser():
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write("# 架构概述\n\n系统采用模块化设计，提供高效的知识问答能力。")
        temp_path = Path(f.name)

    try:
        parser = ParserFactory.get_parser(temp_path)
        assert isinstance(parser, TextParser)
        doc = parser.parse(temp_path)
        assert doc.total_pages == 1
        assert "架构概述" in doc.pages[0].text
    finally:
        temp_path.unlink()


def test_pdf_parser():
    # Create a 2-page test PDF in memory using PyMuPDF
    pdf_doc = pymupdf.open()
    page1 = pdf_doc.new_page()
    page1.insert_text((50, 50), "AgentStudio-RAG Page 1: Enterprise System Architecture.")
    page2 = pdf_doc.new_page()
    page2.insert_text((50, 50), "AgentStudio-RAG Page 2: Vector Retrieval and Re-ranking.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = Path(f.name)
    pdf_doc.save(str(pdf_path))
    pdf_doc.close()

    try:
        parser = ParserFactory.get_parser(pdf_path)
        assert isinstance(parser, PDFParser)
        doc = parser.parse(pdf_path)
        assert doc.total_pages == 2
        assert doc.pages[0].page_number == 1
        assert "Page 1" in doc.pages[0].text
        assert doc.pages[1].page_number == 2
        assert "Page 2" in doc.pages[1].text
    finally:
        pdf_path.unlink()


@pytest.mark.asyncio
async def test_ingestion_pipeline_full(db_session: AsyncSession):
    # 1. Create KB
    kb = KnowledgeBase(name="测试知识库", chunk_size=100, chunk_overlap=20)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    # 2. Create test markdown file
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("系统支持三态熔断器：CLOSED, OPEN, HALF_OPEN。第一状态为 CLOSED，正常调用；失败率超标进入 OPEN 状态。")
        file_path = Path(f.name)

    try:
        doc = Document(
            knowledge_base_id=kb.id,
            file_name="circuit_breaker.txt",
            file_type="txt",
            file_size=file_path.stat().st_size,
            file_path=str(file_path),
            status="PENDING",
        )
        db_session.add(doc)
        await db_session.commit()
        await db_session.refresh(doc)

        pipeline = IngestionPipeline(db_session)
        processed = await pipeline.process_document(doc.id)

        assert processed.status == "COMPLETED"
        assert processed.chunk_count > 0
        assert processed.page_count == 1

        # Check chunks in DB
        stmt = select(DocumentChunk).where(DocumentChunk.document_id == doc.id)
        chunks = (await db_session.execute(stmt)).scalars().all()
        assert len(chunks) == processed.chunk_count
        assert chunks[0].embedding is not None
        assert len(chunks[0].embedding) == kb.embedding_dimensions
    finally:
        file_path.unlink()
