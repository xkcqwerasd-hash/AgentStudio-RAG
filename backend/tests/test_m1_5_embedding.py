import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    EmbeddingServiceError,
    VectorDimensionMismatchError,
    VectorModelMismatchError,
)
from app.ingestion.reindex import ReindexService
from app.llm.embeddings import (
    LocalBGEEmbeddingClient,
    OpenAIEmbeddingClient,
    TestDoubleEmbeddingClient,
    get_embedding_client,
    validate_vector,
)
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.workflow import WorkflowRAGEngine
from app.retrieval.vector_store import VectorRetriever
from app.schemas.chat import ChatRequest


@pytest.mark.asyncio
async def test_local_bge_client_properties():
    """Verify LocalBGEEmbeddingClient initializes correctly with fastembed ONNX model."""
    client = LocalBGEEmbeddingClient(
        model_name="BAAI/bge-small-zh-v1.5",
        dimension=512,
    )
    assert client.provider_name == "local_bge"
    assert client.model_name == "BAAI/bge-small-zh-v1.5"
    assert client.dimensions == 512

    # Embed query
    vec = await client.embed_query("微服务网关路由与熔断降级")
    assert len(vec) == 512
    assert not any(np.isnan(v) or np.isinf(v) for v in vec)

    # L2 norm check (BGE outputs normalized embeddings)
    norm = np.linalg.norm(np.array(vec, dtype=np.float32))
    assert abs(norm - 1.0) < 1e-3


@pytest.mark.asyncio
async def test_local_bge_semantic_similarity():
    """
    Verify real semantic retrieval ability of LocalBGEEmbeddingClient:
    Semantically related sentences (paraphrases) must have higher cosine similarity
    than unrelated sentences.
    """
    client = LocalBGEEmbeddingClient(model_name="BAAI/bge-small-zh-v1.5", dimension=512)

    query = "如何配置系统断路器的失败重试与超时阈值？"
    doc_match = "微服务治理规范：熔断保护机制在连续失败达到阈值后进入 OPEN 状态，超时判定设置为 3000ms。"
    doc_unrelated = "西红柿炒鸡蛋是常见的家常菜，建议先炒鸡蛋再下番茄块翻炒。"

    q_vec = np.array(await client.embed_query(query), dtype=np.float32)
    doc_vecs = await client.embed_documents([doc_match, doc_unrelated])
    m_vec = np.array(doc_vecs[0], dtype=np.float32)
    u_vec = np.array(doc_vecs[1], dtype=np.float32)

    sim_match = float(np.dot(q_vec, m_vec))
    sim_unrelated = float(np.dot(q_vec, u_vec))

    assert sim_match > 0.50, f"Expected strong semantic match > 0.50, got {sim_match}"
    assert sim_match > sim_unrelated + 0.20, (
        f"Semantic match ({sim_match}) should be significantly higher than unrelated ({sim_unrelated})"
    )


def test_validate_vector_constraints():
    """Verify validate_vector strictly catches dimension errors and NaN/Inf anomalies."""
    # Correct vector
    assert validate_vector([0.1, 0.2], 2, "test", "model") == [0.1, 0.2]

    # Dimension mismatch
    with pytest.raises(EmbeddingServiceError) as exc_dim:
        validate_vector([0.1, 0.2], 3, "test", "model")
    assert "Vector dimension mismatch" in str(exc_dim.value.detail)

    # NaN detection
    with pytest.raises(EmbeddingServiceError) as exc_nan:
        validate_vector([0.1, float("nan")], 2, "test", "model")
    assert "NaN values" in str(exc_nan.value.detail)

    # Inf detection
    with pytest.raises(EmbeddingServiceError) as exc_inf:
        validate_vector([0.1, float("inf")], 2, "test", "model")
    assert "Inf values" in str(exc_inf.value.detail)


@pytest.mark.asyncio
async def test_model_mismatch_interception(db_session: AsyncSession):
    """
    Verify pre-retrieval model mismatch check:
    If a KB was indexed with a different model or dimension,
    querying it raises VectorModelMismatchError with HTTP 409 and reindex instructions.
    """
    # Create KB recorded with a different model
    kb = KnowledgeBase(
        name="异构向量库",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        index_version=1,
    )
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="spec.md",
        file_type="markdown",
        file_size=200,
        file_path="/tmp/spec.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="示例内容",
        page_number=1,
        char_length=4,
    )
    chunk.embedding = [0.01] * 1536
    db_session.add(chunk)
    await db_session.commit()

    # Active engine uses test_double or local_bge (not text-embedding-3-small 1536d)
    engine = WorkflowRAGEngine(db_session)
    req = ChatRequest(
        knowledge_base_id=kb.id,
        query="测试提问",
    )

    with pytest.raises(VectorModelMismatchError) as exc_info:
        await engine.execute(req)

    assert exc_info.value.status_code == 409
    assert "Vector model mismatch" in str(exc_info.value.detail)
    assert "Please trigger reindex" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_reindex_service_atomic_upgrade(db_session: AsyncSession):
    """
    Verify ReindexService upgrades existing chunks from test_double to local_bge:
    - Chunks are preserved without re-parsing
    - Vectors are replaced atomically with 512-dim local_bge embeddings
    - KB metadata is updated and index_version is incremented
    """
    kb = KnowledgeBase(
        name="升级测试库",
        embedding_provider="test_double",
        embedding_model="hash-projection-v1",
        embedding_dimensions=1024,
        index_version=1,
    )
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="arch.md",
        file_type="markdown",
        file_size=500,
        file_path="/tmp/arch.md",
        status="COMPLETED",
        chunk_count=2,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    test_double_client = TestDoubleEmbeddingClient(dimension=1024)
    content1 = "服务注册与发现中心采用高可用集群部署，心跳检测间隔为 5 秒。"
    content2 = "消息队列采用 Kafka 分区并发消费模式，保证全局消息有序性。"
    vecs_old = await test_double_client.embed_documents([content1, content2])

    c1 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content=content1,
        page_number=1,
        char_length=len(content1),
    )
    c1.embedding = vecs_old[0]

    c2 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=1,
        content=content2,
        page_number=1,
        char_length=len(content2),
    )
    c2.embedding = vecs_old[1]

    db_session.add_all([c1, c2])
    await db_session.commit()

    # Execute ReindexService
    service = ReindexService(db_session)
    reindex_res = await service.reindex_knowledge_base(
        kb_id=kb.id,
        target_provider="local_bge",
        target_model="BAAI/bge-small-zh-v1.5",
    )

    assert reindex_res.status == "COMPLETED"
    assert reindex_res.reindexed_chunks == 2
    assert reindex_res.previous_provider == "test_double"
    assert reindex_res.previous_dimensions == 1024
    assert reindex_res.new_provider == "local_bge"
    assert reindex_res.new_model == "BAAI/bge-small-zh-v1.5"
    assert reindex_res.new_dimensions == 512
    assert reindex_res.new_index_version == 2

    # Verify chunks in database have been converted to 512 dimensions
    chunks_stmt = select(DocumentChunk).where(DocumentChunk.knowledge_base_id == kb.id)
    chunks = (await db_session.execute(chunks_stmt)).scalars().all()
    assert len(chunks) == 2
    for c in chunks:
        assert len(c.embedding) == 512
        assert not any(np.isnan(v) for v in c.embedding)


@pytest.mark.asyncio
async def test_reindex_service_failure_atomicity(db_session: AsyncSession, monkeypatch):
    """
    Verify reindexing atomicity:
    If an embedding error occurs midway, no database changes are committed.
    Existing chunks and index version remain completely intact.
    """
    kb = KnowledgeBase(
        name="原子性测试库",
        embedding_provider="test_double",
        embedding_model="hash-projection-v1",
        embedding_dimensions=1024,
        index_version=1,
    )
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="test.txt",
        file_type="txt",
        file_size=50,
        file_path="/tmp/test.txt",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    orig_vec = [0.05] * 1024
    c = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="原子性测试原始内容",
        page_number=1,
        char_length=10,
    )
    c.embedding = orig_vec
    db_session.add(c)
    await db_session.commit()

    # Simulate embedding service crash during reindexing
    async def mock_embed_fail(*args, **kwargs):
        raise EmbeddingServiceError("Simulated remote API connection timeout during batch embedding")

    monkeypatch.setattr(LocalBGEEmbeddingClient, "embed_documents", mock_embed_fail)

    service = ReindexService(db_session)
    with pytest.raises(EmbeddingServiceError) as exc_info:
        await service.reindex_knowledge_base(
            kb_id=kb.id,
            target_provider="local_bge",
            target_model="BAAI/bge-small-zh-v1.5",
        )
    assert "Simulated remote API connection timeout" in str(exc_info.value.detail)

    # Verify chunk was NOT modified
    await db_session.refresh(c)
    assert len(c.embedding) == 1024
    assert c.embedding == orig_vec

    # Verify KB metadata was NOT modified
    await db_session.refresh(kb)
    assert kb.embedding_provider == "test_double"
    assert kb.embedding_dimensions == 1024
    assert kb.index_version == 1


@pytest.mark.asyncio
async def test_reindex_api_endpoint(client: AsyncClient, db_session: AsyncSession):
    """Verify POST /api/v1/knowledge-bases/{kb_id}/reindex endpoint."""
    # Create KB
    kb = KnowledgeBase(
        name="API重索引库",
        embedding_provider="test_double",
        embedding_model="hash-projection-v1",
        embedding_dimensions=1024,
        index_version=1,
    )
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    # Call reindex API
    resp = await client.post(
        f"/api/v1/knowledge-bases/{kb.id}/reindex",
        json={
            "target_provider": "local_bge",
            "target_model": "BAAI/bge-small-zh-v1.5",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["knowledge_base_id"] == kb.id
    assert data["new_provider"] == "local_bge"
    assert data["new_model"] == "BAAI/bge-small-zh-v1.5"
    assert data["new_dimensions"] == 512
    assert data["new_index_version"] == 2
    assert data["status"] == "COMPLETED"
