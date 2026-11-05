import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.embeddings import OpenAIEmbeddingClient, TestDoubleEmbeddingClient
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.vector_store import VectorRetriever


@pytest.mark.asyncio
async def test_test_double_embedding_properties():
    """Verify TestDoubleEmbeddingClient generates L2-normalized 1024-dim vectors deterministically."""
    client = TestDoubleEmbeddingClient(dimension=1024)

    text1 = "微服务网关架构与连接池配置"
    text2 = "微服务网关架构与连接池配置"
    text3 = "火箭固体推进剂配方与轨道控制"

    vec1 = await client.embed_query(text1)
    vec2 = await client.embed_query(text2)
    vec3 = await client.embed_query(text3)

    # 1. Dimension check
    assert len(vec1) == 1024
    assert len(vec2) == 1024
    assert len(vec3) == 1024

    # 2. L2 Normalization check (norm should be ~ 1.0)
    norm1 = np.linalg.norm(np.array(vec1))
    assert abs(norm1 - 1.0) < 1e-4

    # 3. Determinism check
    assert vec1 == vec2

    # 4. Semantic similarity: identical text gives 1.0
    sim_identical = float(np.dot(vec1, vec2))
    assert abs(sim_identical - 1.0) < 1e-4

    # Unrelated text has significantly lower similarity
    sim_unrelated = float(np.dot(vec1, vec3))
    assert sim_unrelated < 0.15


@pytest.mark.asyncio
async def test_openai_embedding_client_configuration():
    """Verify OpenAIEmbeddingClient correctly initializes endpoints, model, and headers."""
    client = OpenAIEmbeddingClient(
        api_key="test_key_12345",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-large",
    )
    assert client.api_key == "test_key_12345"
    assert client.base_url == "https://api.example.com/v1"
    assert client.model == "text-embedding-3-large"


@pytest.mark.asyncio
async def test_vector_dimension_mismatch_safety(db_session: AsyncSession):
    """
    Verify VectorRetriever does not crash when encountering chunks
    with mismatched embedding dimensions (e.g. from an old model).
    """
    kb = KnowledgeBase(name="维度测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="dim_doc.txt",
        file_type="txt",
        file_size=100,
        file_path="/tmp/dim_doc.txt",
        status="COMPLETED",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    # Chunk with 512 dimensions (mismatched)
    bad_chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="512维度的错误切块",
        page_number=1,
        char_length=15,
    )
    bad_chunk.embedding = [0.1] * 512

    # Chunk with 1024 dimensions (valid)
    good_chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=1,
        content="1024维度的正确切块",
        page_number=1,
        char_length=15,
    )
    good_chunk.embedding = [0.1] * 1024

    db_session.add_all([bad_chunk, good_chunk])
    await db_session.commit()

    retriever = VectorRetriever(db_session)
    # Query with 1024 dimension vector
    query_vector = [0.1] * 1024
    results = await retriever.search(kb.id, query_vector, top_k=2)

    # Bad chunk was safely skipped, good chunk was returned!
    assert len(results) == 1
    assert results[0].chunk_id == good_chunk.id
