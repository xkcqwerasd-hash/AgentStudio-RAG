import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    EmbeddingServiceError,
    FileTooLargeError,
    LLMNotConfiguredError,
    UnsupportedFileTypeError,
)
from app.llm.embeddings import OpenAIEmbeddingClient, get_embedding_client
from app.llm.openai_client import OpenAILLMClient
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.workflow import WorkflowRAGEngine
from app.schemas.chat import ChatRequest


@pytest.mark.asyncio
async def test_empty_knowledge_base(db_session: AsyncSession):
    """Test Case 1: Empty knowledge base without any documents or chunks."""
    kb = KnowledgeBase(name="空知识库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    engine = WorkflowRAGEngine(db_session)
    req = ChatRequest(knowledge_base_id=kb.id, query="系统的架构是什么？")
    resp = await engine.execute(req)

    assert "尚未建立任何文档索引" in resp.answer
    assert len(resp.citations) == 0
    assert resp.execution_info.retrieved_chunks_count == 0


@pytest.mark.asyncio
async def test_non_existent_answer_query(db_session: AsyncSession):
    """Test Case 2: Query for which no relevant evidence exists in the knowledge base."""
    kb = KnowledgeBase(name="特定领域库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="微服务指南.md",
        file_type="markdown",
        file_size=100,
        file_path="/tmp/guide.md",
        status="COMPLETED",
        chunk_count=1,
        page_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # Insert chunk about microservices
    emb_client = get_embedding_client("test_double")
    content = "微服务网关使用 Spring Cloud Gateway，负责路由转发与鉴权认证。"
    emb = await emb_client.embed_documents([content])

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content=content,
        page_number=1,
        char_length=len(content),
    )
    chunk.embedding = emb[0]
    db_session.add(chunk)
    await db_session.commit()

    # Query for something completely unrelated
    engine = WorkflowRAGEngine(db_session)
    # Raising threshold or querying unrelated topic to yield no matches
    req = ChatRequest(
        knowledge_base_id=kb.id,
        query="火星探测器的火箭推进燃料配比是多少？",
        top_k=2,
    )
    resp = await engine.execute(req)

    assert "未找到与您提问相关的充分事实证据" in resp.answer
    assert len(resp.citations) == 0


@pytest.mark.asyncio
async def test_unsupported_file_format(client: AsyncClient, db_session: AsyncSession):
    """Test Case 3: Upload unsupported file format (e.g. .exe or .zip)."""
    kb = KnowledgeBase(name="文件格式测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    files = {"file": ("malicious.exe", b"binary content", "application/octet-stream")}
    res = await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/upload", files=files)

    assert res.status_code == 400
    assert "Unsupported file format '.exe'" in res.json()["detail"]


@pytest.mark.asyncio
async def test_llm_api_not_configured():
    """Test Case 4: LLM API key missing when using OpenAI client."""
    client = OpenAILLMClient(api_key="", base_url="https://api.openai.com/v1")
    with pytest.raises(LLMNotConfiguredError) as exc_info:
        await client.generate("Hello")
    assert "LLM API key is not configured" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_embedding_service_unavailable():
    """Test Case 5: Embedding service connection failure or unreachable host."""
    client = OpenAIEmbeddingClient(
        api_key="mock_key",
        base_url="http://127.0.0.1:59999",  # unroutable local test port
    )
    with pytest.raises(EmbeddingServiceError) as exc_info:
        await client.embed_documents(["测试文本"])
    assert "Embedding service" in str(exc_info.value.detail)
