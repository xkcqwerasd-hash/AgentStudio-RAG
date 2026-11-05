import asyncio
import json
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, patch

from app.agent.tools.base import BaseTool
from app.agent.tools.evidence import EvidenceItem, EvidenceRegistry
from app.agent.tools.knowledge_search import (
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseTool,
)
from app.agent.tools.registry import DuplicateToolError, ToolRegistry
from app.agent.tools.schemas import ToolResult
from app.core.config import settings
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.citation import CitationExtractor
from app.retrieval.reranker import CrossEncoderReranker, RerankResult
from app.retrieval.rrf_fusion import HybridRetrievalResult
from app.retrieval.vector_store import ScoredChunk


def create_fake_chunk(
    chunk_id: str,
    doc_id: str = "doc-1",
    doc_name: str = "test_spec.md",
    page_number: int = 1,
    chunk_index: int = 0,
    content: str = "微服务超时阈值设置为 5000ms",
    score: float = 0.88,
    dense_rank: int = 1,
    bm25_rank: int = 1,
    dense_score: float = 0.85,
    bm25_score: float = 14.2,
    fusion_score: float = 0.032,
    reranker_score: float = 0.95,
    reranker_raw_score: float = 3.2,
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        document_name=doc_name,
        page_number=page_number,
        chunk_index=chunk_index,
        content=content,
        similarity_score=score,
        dense_rank=dense_rank,
        bm25_rank=bm25_rank,
        dense_score=dense_score,
        bm25_score=bm25_score,
        fusion_score=fusion_score,
        reranker_score=reranker_score,
        reranker_raw_score=reranker_raw_score,
    )


# ==============================================================================
# 1. Tool 注册成功测试
# ==============================================================================


def test_tool_registration_success():
    """Test 1: Verify tool registration, discovery, listing and unregistration."""
    registry = ToolRegistry()
    assert len(registry.list_tools()) == 0

    tool = SearchKnowledgeBaseTool()
    registry.register(tool)

    assert registry.has("search_knowledge_base") is True
    assert registry.get("search_knowledge_base") is tool
    assert "search_knowledge_base" in registry.list_tool_names()
    assert len(registry.list_tools()) == 1

    # Unregister
    registry.unregister("search_knowledge_base")
    assert registry.has("search_knowledge_base") is False
    assert registry.get("search_knowledge_base") is None
    assert len(registry.list_tools()) == 0


# ==============================================================================
# 2. 重复注册异常测试
# ==============================================================================


def test_tool_duplicate_registration_error():
    """Test 2: Verify duplicate tool registration raises DuplicateToolError."""
    registry = ToolRegistry()
    tool1 = SearchKnowledgeBaseTool()
    tool2 = SearchKnowledgeBaseTool()

    registry.register(tool1)

    with pytest.raises(DuplicateToolError) as exc_info:
        registry.register(tool2)

    assert "already registered" in str(exc_info.value)
    assert "search_knowledge_base" in str(exc_info.value)


# ==============================================================================
# 3. OpenAI JSON Schema 生成测试
# ==============================================================================


def test_openai_json_schema_generation():
    """Test 3: Verify OpenAI Function Calling JSON schema complies with OpenAI specifications."""
    tool = SearchKnowledgeBaseTool()
    schema = tool.to_openai_tool()

    assert schema["type"] == "function"
    assert "function" in schema
    fn = schema["function"]
    assert fn["name"] == "search_knowledge_base"
    assert isinstance(fn["description"], str) and len(fn["description"]) > 10

    params = fn["parameters"]
    assert params["type"] == "object"
    assert "properties" in params
    props = params["properties"]

    # Verify parameters
    assert "knowledge_base_id" in props
    assert props["knowledge_base_id"]["type"] == "string"
    assert "query" in props
    assert props["query"]["type"] == "string"
    assert "top_k" in props
    assert props["top_k"]["type"] == "integer"
    assert props["top_k"]["default"] == 4

    # Verify required fields
    assert "knowledge_base_id" in params["required"]
    assert "query" in params["required"]
    assert "top_k" not in params["required"]

    # Registry export
    registry = ToolRegistry()
    registry.register(tool)
    tools_list = registry.to_openai_tools()
    assert len(tools_list) == 1
    assert tools_list[0] == schema


# ==============================================================================
# 4. 参数验证成功与失败测试
# ==============================================================================


@pytest.mark.asyncio
async def test_parameter_validation_success_and_failures():
    """Test 4: Verify Pydantic validation handles valid, missing, out-of-bounds, and malformed inputs."""
    tool = SearchKnowledgeBaseTool()
    registry = ToolRegistry()
    registry.register(tool)

    # 4a. Missing required parameter (knowledge_base_id)
    res_missing_kb = await tool.run(query="测试问题")
    assert res_missing_kb.success is False
    assert "Invalid arguments" in res_missing_kb.error
    assert "knowledge_base_id" in res_missing_kb.error

    # 4b. Missing required parameter (query)
    res_missing_q = await tool.run(knowledge_base_id="kb-123")
    assert res_missing_q.success is False
    assert "Invalid arguments" in res_missing_q.error
    assert "query" in res_missing_q.error

    # 4c. Out of bounds: top_k < 1
    res_low_k = await tool.run(knowledge_base_id="kb-123", query="测试", top_k=0)
    assert res_low_k.success is False
    assert "Invalid arguments" in res_low_k.error

    # 4d. Out of bounds: top_k > 20
    res_high_k = await tool.run(knowledge_base_id="kb-123", query="测试", top_k=25)
    assert res_high_k.success is False
    assert "Invalid arguments" in res_high_k.error

    # 4e. Malformed JSON string via registry.execute
    res_malformed_json = await registry.execute("search_knowledge_base", "not-a-{json")
    assert res_malformed_json.success is False
    assert "Malformed JSON" in res_malformed_json.error

    # 4f. Non-dict JSON string via registry.execute
    res_non_dict_json = await registry.execute("search_knowledge_base", "[\"item\"]")
    assert res_non_dict_json.success is False
    assert "must be a JSON object" in res_non_dict_json.error

    # 4g. Unregistered tool rejection
    res_unregistered = await registry.execute("malicious_unregistered_tool", "{}")
    assert res_unregistered.success is False
    assert "Security rejection" in res_unregistered.error


# ==============================================================================
# 5. Tool 正常执行返回测试
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_normal_execution_success(db_session: AsyncSession):
    """Test 5: Verify normal execution produces structured ToolResult with evidence and timing."""
    kb = KnowledgeBase(name="Test-KB", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="spec.md", file_type="md", file_path="/tmp/spec.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="网关服务超时配置为 3000ms，熔断阈值为 50%",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add(chunk)
    await db_session.commit()

    tool = SearchKnowledgeBaseTool(db=db_session)
    result = await tool.run(knowledge_base_id=kb.id, query="网关超时", top_k=4)

    assert result.success is True
    assert result.tool_name == "search_knowledge_base"
    assert result.duration_ms >= 0.0
    assert result.error is None

    data = result.data
    assert data["query"] == "网关超时"
    assert data["knowledge_base_id"] == kb.id
    assert data["knowledge_base_name"] == "Test-KB"
    assert data["evidence_count"] >= 1
    assert data["candidates_retrieved"] >= 1
    assert "total_retrieval_duration_ms" in data
    assert data["degraded"] is False

    # Verify tool message serialization
    msg_content = result.to_tool_message_content()
    parsed_msg = json.loads(msg_content)
    assert parsed_msg["knowledge_base_id"] == kb.id
    assert len(parsed_msg["evidence"]) >= 1


# ==============================================================================
# 6. 严格知识库隔离测试
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_knowledge_base_strict_isolation(db_session: AsyncSession):
    """Test 6: Verify strict KB isolation: querying KB-A never recalls chunks from KB-B."""
    kb_a = KnowledgeBase(name="Tenant-A-KB", embedding_model=settings.EMBEDDING_MODEL_NAME)
    kb_b = KnowledgeBase(name="Tenant-B-KB", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add_all([kb_a, kb_b])
    await db_session.flush()

    doc_a = Document(knowledge_base_id=kb_a.id, file_name="doc_a.txt", file_type="txt", file_path="/tmp/a.txt", status="COMPLETED")
    doc_b = Document(knowledge_base_id=kb_b.id, file_name="doc_b.txt", file_type="txt", file_path="/tmp/b.txt", status="COMPLETED")
    db_session.add_all([doc_a, doc_b])
    await db_session.flush()

    chunk_a = DocumentChunk(
        document_id=doc_a.id,
        knowledge_base_id=kb_a.id,
        content="租户A密码策略：至少12位且包含特殊字符",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    chunk_b = DocumentChunk(
        document_id=doc_b.id,
        knowledge_base_id=kb_b.id,
        content="租户B机密技术方案：数据库主从复制密钥为 secret_key_b",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add_all([chunk_a, chunk_b])
    await db_session.commit()

    tool = SearchKnowledgeBaseTool(db=db_session)

    # Search in Tenant-A KB with query matching Tenant-B content
    res_a = await tool.run(knowledge_base_id=kb_a.id, query="数据库主从复制密钥", top_k=5)
    assert res_a.success is True
    for item in res_a.data["evidence"]:
        assert item["document_name"] == "doc_a.txt"
        assert "secret_key_b" not in item["content"]

    # Search in Tenant-B KB
    res_b = await tool.run(knowledge_base_id=kb_b.id, query="密码策略", top_k=5)
    assert res_b.success is True
    for item in res_b.data["evidence"]:
        assert item["document_name"] == "doc_b.txt"
        assert "租户A密码策略" not in item["content"]


# ==============================================================================
# 7. 空知识库处理测试
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_empty_knowledge_base(db_session: AsyncSession):
    """Test 7: Verify empty knowledge base gracefully returns empty evidence without crashing."""
    empty_kb = KnowledgeBase(name="Empty-KB", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(empty_kb)
    await db_session.commit()

    tool = SearchKnowledgeBaseTool(db=db_session)
    result = await tool.run(knowledge_base_id=empty_kb.id, query="任何问题", top_k=4)

    assert result.success is True
    data = result.data
    assert data["evidence"] == []
    assert data["evidence_count"] == 0
    assert data["candidates_retrieved"] == 0
    assert "当前为空" in data["message"]
    assert data["degraded"] is False


# ==============================================================================
# 8. 不存在的知识库处理测试
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_nonexistent_knowledge_base(db_session: AsyncSession):
    """Test 8: Verify querying nonexistent KB returns structured error without crashing."""
    tool = SearchKnowledgeBaseTool(db=db_session)
    result = await tool.run(knowledge_base_id="nonexistent-kb-id-9999", query="任何问题")

    assert result.success is False
    assert "not found" in result.error.lower() or "does not exist" in result.error.lower()
    assert result.tool_name == "search_knowledge_base"


# ==============================================================================
# 9. 零召回处理测试
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_no_recall_query(db_session: AsyncSession):
    """Test 9: Verify query returning 0 candidates preserves metadata and success=True."""
    kb = KnowledgeBase(name="KB-With-Doc", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="doc.txt", file_type="txt", file_path="/tmp/d.txt", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="常规技术文档",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add(chunk)
    await db_session.commit()

    tool = SearchKnowledgeBaseTool(db=db_session)

    # Mock hybrid retriever to return empty candidate list
    with patch("app.agent.tools.knowledge_search.HybridRetriever.search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = HybridRetrievalResult(
            chunks=[],
            dense_duration_ms=5.0,
            bm25_duration_ms=4.0,
            rrf_duration_ms=1.0,
            total_duration_ms=10.0,
            dense_chunks_count=0,
            bm25_chunks_count=0,
            degradation_status="none",
        )

        result = await tool.run(knowledge_base_id=kb.id, query="毫无关系的偏门生僻词")

        assert result.success is True
        assert result.data["evidence"] == []
        assert result.data["evidence_count"] == 0
        assert "未找到与问题" in result.data["message"]
        assert result.data["degraded"] is False


# ==============================================================================
# 10. Reranker 异常与降级测试
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_reranker_error_and_degradation(db_session: AsyncSession):
    """Test 10: Verify Reranker failure triggers fallback to RRF candidates with degraded=True."""
    kb = KnowledgeBase(name="KB-Degrade-Test", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="spec.md", file_type="md", file_path="/tmp/s.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="核心微服务架构设计说明",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add(chunk)
    await db_session.commit()

    # Create a mock reranker that simulates runtime degradation
    mock_reranker = CrossEncoderReranker()
    candidate = create_fake_chunk("c-1", content="核心微服务架构设计说明", score=0.032)

    async def mock_degraded_rerank(query, candidates, top_k):
        return RerankResult(
            chunks=candidates[:top_k],
            duration_ms=15.0,
            candidates_in=len(candidates),
            candidates_out=len(candidates[:top_k]),
            model_name="test_reranker",
            degraded=True,
            degradation_reason="ONNX runtime execution timeout, fallback to RRF",
        )

    mock_reranker.rerank = AsyncMock(side_effect=mock_degraded_rerank)

    tool = SearchKnowledgeBaseTool(db=db_session, reranker=mock_reranker)
    result = await tool.run(knowledge_base_id=kb.id, query="架构设计", top_k=2)

    assert result.success is True
    assert result.data["degraded"] is True
    assert "timeout" in result.data["degradation_reason"]
    assert len(result.data["evidence"]) >= 1


# ==============================================================================
# 11. 结构化 Evidence 完整性测试
# ==============================================================================


def test_structured_evidence_field_integrity():
    """Test 11: Verify EvidenceItem retains all ScoredChunk fields and converts back losslessly."""
    raw_chunk = create_fake_chunk(
        chunk_id="chunk-abc-123",
        doc_id="doc-xyz-789",
        doc_name="architecture_v2.pdf",
        page_number=12,
        chunk_index=3,
        content="系统在峰值时采用自适应限流算法",
        score=0.92,
        dense_rank=1,
        bm25_rank=2,
        dense_score=0.89,
        bm25_score=15.4,
        fusion_score=0.0325,
        reranker_score=0.965,
        reranker_raw_score=3.85,
    )

    registry = EvidenceRegistry()
    evidence_items = registry.register_chunks([raw_chunk])
    assert len(evidence_items) == 1
    it = evidence_items[0]

    # Verify all fields
    assert it.ref_id == 1
    assert it.tag == "[Ref-1]"
    assert it.chunk_id == "chunk-abc-123"
    assert it.document_id == "doc-xyz-789"
    assert it.document_name == "architecture_v2.pdf"
    assert it.page_number == 12
    assert it.chunk_index == 3
    assert it.content == "系统在峰值时采用自适应限流算法"
    assert it.similarity_score == 0.92
    assert it.dense_rank == 1
    assert it.bm25_rank == 2
    assert it.dense_score == 0.89
    assert it.bm25_score == 15.4
    assert it.fusion_score == 0.0325
    assert it.reranker_score == 0.965
    assert it.reranker_raw_score == 3.85

    # Lossless conversion back to ScoredChunk
    converted = it.to_scored_chunk()
    assert converted.chunk_id == raw_chunk.chunk_id
    assert converted.similarity_score == raw_chunk.similarity_score
    assert converted.dense_rank == raw_chunk.dense_rank
    assert converted.bm25_rank == raw_chunk.bm25_rank
    assert converted.reranker_raw_score == raw_chunk.reranker_raw_score


# ==============================================================================
# 12. EvidenceRegistry 全局引用与去重测试
# ==============================================================================


def test_evidence_registry_cross_call_dedup_and_global_indexing():
    """
    Test 12: Verify EvidenceRegistry cross-call deduplication and sequential [Ref-X] global indexing:
    - Call 1 recalls Chunk A, Chunk B -> [Ref-1], [Ref-2]
    - Call 2 recalls Chunk B, Chunk C -> Chunk B reuses [Ref-2], Chunk C gets [Ref-3]
    - Total registry count is 3.
    - CitationExtractor can correctly resolve citations using registry.get_all_chunks().
    """
    chunk_a = create_fake_chunk("chunk-A", content="服务配置中心规范 A")
    chunk_b = create_fake_chunk("chunk-B", content="链路追踪采样率规范 B")
    chunk_c = create_fake_chunk("chunk-C", content="日志脱敏加密规范 C")

    registry = EvidenceRegistry()

    # Call 1: Chunk A and Chunk B
    items_call1 = registry.register_chunks([chunk_a, chunk_b])
    assert len(items_call1) == 2
    assert items_call1[0].tag == "[Ref-1]"
    assert items_call1[0].chunk_id == "chunk-A"
    assert items_call1[1].tag == "[Ref-2]"
    assert items_call1[1].chunk_id == "chunk-B"

    # Call 2: Chunk B and Chunk C
    items_call2 = registry.register_chunks([chunk_b, chunk_c])
    assert len(items_call2) == 2
    # Chunk B must reuse Ref-2!
    assert items_call2[0].tag == "[Ref-2]"
    assert items_call2[0].chunk_id == "chunk-B"
    # Chunk C must receive next sequential global index Ref-3!
    assert items_call2[1].tag == "[Ref-3]"
    assert items_call2[1].chunk_id == "chunk-C"

    # Registry state
    assert registry.total_evidence_count == 3
    all_chunks = registry.get_all_chunks()
    assert len(all_chunks) == 3
    assert all_chunks[0].chunk_id == "chunk-A"
    assert all_chunks[1].chunk_id == "chunk-B"
    assert all_chunks[2].chunk_id == "chunk-C"

    # Verify lookup by ref_id
    assert registry.get_chunk_by_ref_id(1).chunk_id == "chunk-A"
    assert registry.get_chunk_by_ref_id(2).chunk_id == "chunk-B"
    assert registry.get_chunk_by_ref_id(3).chunk_id == "chunk-C"
    assert registry.get_chunk_by_ref_id(4) is None

    # Verify CitationExtractor integration
    answer_text = "根据系统配置 [Ref-2]，链路追踪采样率应保持全量记录。"
    citations = CitationExtractor.extract_citations(answer_text, all_chunks)
    assert len(citations) == 1
    assert citations[0].citation_index == 2
    assert citations[0].chunk_id == "chunk-B"
    assert citations[0].document_name == "test_spec.md"

    # Verify text formatting
    formatted_text = registry.format_evidence_text(items_call1)
    assert "[Ref-1] 来源文件: test_spec.md" in formatted_text
    assert "[Ref-2] 来源文件: test_spec.md" in formatted_text
