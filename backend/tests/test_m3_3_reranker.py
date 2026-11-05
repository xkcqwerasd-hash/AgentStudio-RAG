import asyncio
import json
import math
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.citation import CitationExtractor
from app.rag.context_budget import ContextBudgetManager
from app.rag.workflow import WorkflowRAGEngine
from app.retrieval.reranker import (
    BaseRerankerClient,
    CrossEncoderReranker,
    LocalONNXReranker,
    RerankResult,
    TestDoubleReranker,
    compute_stable_sigmoid,
    get_reranker_client,
)
from app.retrieval.rrf_fusion import HybridRetriever
from app.retrieval.vector_store import ScoredChunk
from app.schemas.chat import ChatRequest


def create_fake_chunk(
    chunk_id: str,
    doc_id: str = "doc-1",
    doc_name: str = "test_doc.md",
    page_number: int = 1,
    chunk_index: int = 0,
    content: str = "测试文本内容",
    score: float = 0.8,
    dense_rank: int = 1,
    bm25_rank: int = 1,
    dense_score: float = 0.8,
    bm25_score: float = 12.5,
    fusion_score: float = 0.032,
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
    )


# ==============================================================================
# 1. 模型初始化测试 (Model Initialization & Factory Singleton)
# ==============================================================================


def test_reranker_factory_singleton():
    """Test 1: Verify get_reranker_client returns singleton instance per provider."""
    client1 = get_reranker_client("test_double")
    client2 = get_reranker_client("test_double")
    assert client1 is client2
    assert isinstance(client1, TestDoubleReranker)


def test_test_double_reranker_initialization():
    """Test 2: TestDoubleReranker initializes with default model name."""
    client = TestDoubleReranker(model_name="test-reranker-v1")
    assert client.model_name == "test-reranker-v1"
    assert client.provider_name == "test_double"


def test_local_onnx_reranker_initialization():
    """Test 3: LocalONNXReranker initializes with configuration."""
    client = LocalONNXReranker(model_name="Xenova/bge-reranker-base", batch_size=8)
    assert client.model_name == "Xenova/bge-reranker-base"
    assert client.provider_name == "local_onnx"
    assert client.batch_size == 8


# ==============================================================================
# 2. 批量打分与 Sigmoid 归一化测试 (Batch Scoring & Sigmoid Normalization)
# ==============================================================================


def test_stable_sigmoid_extreme_inputs():
    """Test 4A (Fix-3): Verify compute_stable_sigmoid handles extreme logits without OverflowError."""
    # Negative extremes
    assert compute_stable_sigmoid(-1000.0) == 0.0
    assert compute_stable_sigmoid(-100.0) == 0.0
    assert compute_stable_sigmoid(-50.0) == 0.0
    # Center
    assert compute_stable_sigmoid(0.0) == 0.5
    # Positive extremes
    assert compute_stable_sigmoid(50.0) == 1.0
    assert compute_stable_sigmoid(100.0) == 1.0
    assert compute_stable_sigmoid(1000.0) == 1.0
    # Finite bounds & NaN safe
    assert compute_stable_sigmoid(float("nan")) == 0.0
    val = compute_stable_sigmoid(-710.0)  # Would throw OverflowError in math.exp without protection
    assert 0.0 <= val <= 1.0


@pytest.mark.asyncio
async def test_reranker_raw_score_prevents_sigmoid_saturation_ties():
    """Test 4B (Fix-2): Raw logit sorting prevents Sigmoid saturation ties on high logits."""
    class SaturatedLogitClient(BaseRerankerClient):
        @property
        def model_name(self) -> str:
            return "saturated_test"

        @property
        def provider_name(self) -> str:
            return "mock"

        async def rerank(self, query, candidates, top_k=None):
            # c_high has higher raw logit (20.0), but lower fusion score (0.001)
            # c_mid has lower raw logit (15.0), but higher fusion score (0.050)
            # In Sigmoid with 6 decimal rounding, both squash to 1.000000.
            # Fix-2 guarantees c_high ranks #1 due to raw logit priority.
            items = []
            for c, lgt in zip(candidates, [15.0, 20.0]):
                sig = round(compute_stable_sigmoid(lgt), 6)
                items.append(ScoredChunk(
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    document_name=c.document_name,
                    page_number=c.page_number,
                    chunk_index=c.chunk_index,
                    content=c.content,
                    similarity_score=sig,
                    fusion_score=c.fusion_score,
                    reranker_score=sig,
                    reranker_raw_score=lgt,
                ))
            # Sort with Fix-2 priority
            items.sort(
                key=lambda x: (
                    -(x.reranker_raw_score if x.reranker_raw_score is not None else -1e9),
                    -(x.fusion_score if x.fusion_score is not None else -1e9),
                    x.chunk_id,
                )
            )
            for idx, item in enumerate(items, start=1):
                item.reranker_rank = idx
            return items

    c1 = create_fake_chunk("c1_mid", fusion_score=0.050)
    c2 = create_fake_chunk("c2_high", fusion_score=0.001)

    reranker = CrossEncoderReranker(client=SaturatedLogitClient())
    res = await reranker.rerank(query="test", candidates=[c1, c2], top_k=2)

    # c2_high has higher logit (20.0 vs 15.0), so it must be rank 1 despite both having reranker_score = 1.000000
    assert res.chunks[0].chunk_id == "c2_high"
    assert res.chunks[1].chunk_id == "c1_mid"
    assert res.chunks[0].reranker_rank == 1


@pytest.mark.asyncio
async def test_batch_scoring_calibration():
    """Test 4: Batch scoring returns normalized scores in [0.0, 1.0] and raw logits."""
    client = TestDoubleReranker()
    query = "系统的超时阈值是多少？"
    c1 = create_fake_chunk("c1", content="系统超时阈值默认配置为5000毫秒，超时后进入降级流程。")
    c2 = create_fake_chunk("c2", content="员工请假需要提前三个工作日提交飞书审批。")
    c3 = create_fake_chunk("c3", content="数据库连接池最小空闲连接数 min_idle 为 5。")

    results = await client.rerank(query=query, candidates=[c1, c2, c3], top_k=3)
    assert len(results) == 3
    # c1 matches "超时" -> highest score
    assert results[0].chunk_id == "c1"
    for r in results:
        assert r.reranker_score is not None
        assert 0.0 <= r.reranker_score <= 1.0
        assert r.reranker_raw_score is not None
        assert r.reranker_rank is not None


# ==============================================================================
# 3. 排序稳定性与 Tie-Breaking (Ranking Stability)
# ==============================================================================


@pytest.mark.asyncio
async def test_ranking_stability_and_tie_breaking():
    """Test 5: Identical score candidates have deterministic rank ordering by fusion score and chunk_id."""
    client = TestDoubleReranker()
    # Create chunks with identical content and fusion_score to test tie-breaking
    c1 = create_fake_chunk("c_beta", content="无相关内容", fusion_score=0.01)
    c2 = create_fake_chunk("c_alpha", content="无相关内容", fusion_score=0.01)
    c3 = create_fake_chunk("c_gamma", content="无相关内容", fusion_score=0.02)

    res1 = await client.rerank("任意问题", [c1, c2, c3])
    res2 = await client.rerank("任意问题", [c1, c2, c3])

    # Higher fusion score (c_gamma) comes first; for equal scores, chunk_id tie-breaking: c_alpha before c_beta
    assert [x.chunk_id for x in res1] == [x.chunk_id for x in res2]
    assert res1[0].chunk_id == "c_gamma"
    assert res1[1].chunk_id == "c_alpha"
    assert res1[2].chunk_id == "c_beta"


# ==============================================================================
# 4. 空候选集与边界处理 (Empty Candidate Handling)
# ==============================================================================


@pytest.mark.asyncio
async def test_empty_candidates_handling():
    """Test 6: Empty candidate list returns empty RerankResult immediately with zero duration."""
    reranker = CrossEncoderReranker(client=TestDoubleReranker())
    result = await reranker.rerank(query="任意查询", candidates=[])
    assert result.chunks == []
    assert result.candidates_in == 0
    assert result.candidates_out == 0
    assert result.duration_ms == 0.0
    assert not result.degraded


@pytest.mark.asyncio
async def test_empty_query_handling():
    """Test 7: Empty or whitespace query returns empty RerankResult safely."""
    reranker = CrossEncoderReranker(client=TestDoubleReranker())
    c1 = create_fake_chunk("c1")
    result = await reranker.rerank(query="   ", candidates=[c1])
    assert result.chunks == []
    assert result.candidates_in == 0


# ==============================================================================
# 5. 元数据保留验证 (Candidate Metadata Preservation)
# ==============================================================================


@pytest.mark.asyncio
async def test_candidate_metadata_preservation():
    """Test 8: Ensure all original metadata (doc_id, page_number, ranks, fusion_score) are intact."""
    reranker = CrossEncoderReranker(client=TestDoubleReranker())
    c = create_fake_chunk(
        chunk_id="chk-999",
        doc_id="doc-888",
        doc_name="architecture_spec.md",
        page_number=42,
        chunk_index=7,
        content="JWT认证支持RS256非对称加密签名。",
        dense_rank=3,
        bm25_rank=2,
        dense_score=0.88,
        bm25_score=14.2,
        fusion_score=0.0315,
    )

    result = await reranker.rerank(query="JWT认证机制", candidates=[c], top_k=1)
    assert len(result.chunks) == 1
    out = result.chunks[0]
    assert out.chunk_id == "chk-999"
    assert out.document_id == "doc-888"
    assert out.document_name == "architecture_spec.md"
    assert out.page_number == 42
    assert out.chunk_index == 7
    assert out.content == "JWT认证支持RS256非对称加密签名。"
    assert out.dense_rank == 3
    assert out.bm25_rank == 2
    assert out.dense_score == 0.88
    assert out.bm25_score == 14.2
    assert out.fusion_score == 0.0315
    assert out.reranker_rank == 1
    assert out.reranker_score is not None
    assert out.reranker_raw_score is not None


# ==============================================================================
# 6. Reranker 异常降级测试 (Error Fallback Degradation)
# ==============================================================================


@pytest.mark.asyncio
async def test_reranker_exception_fallback():
    """Test 9: When underlying reranker throws an error, gracefully fallback to original RRF order."""
    class FaultyClient(BaseRerankerClient):
        @property
        def model_name(self) -> str:
            return "faulty-model"

        @property
        def provider_name(self) -> str:
            return "faulty"

        async def rerank(self, query, candidates, top_k=None):
            raise RuntimeError("CUDA Out of Memory simulation")

    c1 = create_fake_chunk("c1", content="第一条")
    c2 = create_fake_chunk("c2", content="第二条")
    c3 = create_fake_chunk("c3", content="第三条")

    reranker = CrossEncoderReranker(client=FaultyClient())
    result = await reranker.rerank(query="测试提问", candidates=[c1, c2, c3], top_k=2)

    assert result.degraded is True
    assert "CUDA Out of Memory" in (result.degradation_reason or "")
    assert len(result.chunks) == 2
    assert [x.chunk_id for x in result.chunks] == ["c1", "c2"]  # Preserved original RRF slice


# ==============================================================================
# 7. 超时降级测试 (Timeout Fallback Degradation)
# ==============================================================================


@pytest.mark.asyncio
async def test_reranker_timeout_fallback():
    """Test 10: When reranker exceeds timeout, fallback to RRF ranking with degradation_reason='timeout'."""
    class SlowClient(BaseRerankerClient):
        @property
        def model_name(self) -> str:
            return "slow-model"

        @property
        def provider_name(self) -> str:
            return "slow"

        async def rerank(self, query, candidates, top_k=None):
            await asyncio.sleep(0.5)
            return candidates

    c1 = create_fake_chunk("c1")
    c2 = create_fake_chunk("c2")

    reranker = CrossEncoderReranker(client=SlowClient())
    result = await reranker.rerank(query="测试", candidates=[c1, c2], top_k=2, timeout=0.05)

    assert result.degraded is True
    assert result.degradation_reason == "timeout"
    assert len(result.chunks) == 2
    assert [x.chunk_id for x in result.chunks] == ["c1", "c2"]


# ==============================================================================
# 8. KnowledgeBase 严格隔离测试 (Knowledge Base Isolation)
# ==============================================================================


@pytest.mark.asyncio
async def test_reranker_knowledge_base_isolation(db_session: AsyncSession):
    """Test 11: End-to-end retrieval with reranker maintains strict KB isolation."""
    emb_client = get_embedding_client()
    # Create two knowledge bases
    kb1 = KnowledgeBase(
        name="KB 1",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    kb2 = KnowledgeBase(
        name="KB 2",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    db_session.add_all([kb1, kb2])
    await db_session.flush()

    doc1 = Document(knowledge_base_id=kb1.id, file_name="doc1.md", file_type="md", file_path="/doc1.md", status="COMPLETED")
    doc2 = Document(knowledge_base_id=kb2.id, file_name="doc2.md", file_type="md", file_path="/doc2.md", status="COMPLETED")
    db_session.add_all([doc1, doc2])
    await db_session.flush()

    content1 = "KB1: 系统的熔断机制包含 CLOSED, OPEN, HALF_OPEN 三种状态。"
    content2 = "KB2: 考勤机制包含弹性工作制与年假规则。"
    embs1 = await emb_client.embed_documents([content1])
    embs2 = await emb_client.embed_documents([content2])

    chunk1 = DocumentChunk(
        knowledge_base_id=kb1.id,
        document_id=doc1.id,
        chunk_index=0,
        content=content1,
        embedding_json=json.dumps(embs1[0]),
    )
    chunk2 = DocumentChunk(
        knowledge_base_id=kb2.id,
        document_id=doc2.id,
        chunk_index=0,
        content=content2,
        embedding_json=json.dumps(embs2[0]),
    )
    db_session.add_all([chunk1, chunk2])
    await db_session.commit()

    # Search KB1
    engine = WorkflowRAGEngine(db_session)
    engine.reranker = CrossEncoderReranker(client=TestDoubleReranker())

    req = ChatRequest(knowledge_base_id=kb1.id, query="系统的熔断状态有哪些？", top_k=4)
    resp = await engine.execute(req)

    # All citations and execution must belong strictly to KB1
    assert "KB1" in resp.answer or resp.execution_info.retrieved_chunks_count > 0
    assert resp.execution_info.reranker_enabled is True
    assert resp.execution_info.reranker_candidates_in >= 1

    # Check chunks retrieved do not include KB2 chunks
    for c in resp.citations:
        assert c.document_name == "doc1.md"


# ==============================================================================
# 9. Context Budget 动态集成验证 (Token Budget Integration)
# ==============================================================================


@pytest.mark.asyncio
async def test_reranker_context_budget_integration():
    """Test 12: Reranked chunks are properly passed to ContextBudgetManager and pruned if budget exceeded."""
    budget_mgr = ContextBudgetManager()
    c1 = create_fake_chunk("c1", content="第一段高相关文本 " * 50, score=0.99)
    c2 = create_fake_chunk("c2", content="第二段中相关文本 " * 50, score=0.85)
    c3 = create_fake_chunk("c3", content="第三段低相关文本 " * 50, score=0.70)

    # Rerank
    reranker = CrossEncoderReranker(client=TestDoubleReranker())
    rerank_res = await reranker.rerank("测试问题", [c1, c2, c3], top_k=3)
    assert len(rerank_res.chunks) == 3

    # Feed into ContextBudgetManager with low budget to force pruning
    plan = budget_mgr.allocate_and_assemble(
        query="测试问题",
        system_prompt="You are a helpful assistant.",
        raw_history_messages=[],
        retrieved_chunks=rerank_res.chunks,
    )

    # Kept chunks must maintain reranker rank order
    assert len(plan.kept_chunks) >= 1
    assert plan.kept_chunks[0].chunk_id == rerank_res.chunks[0].chunk_id


# ==============================================================================
# 10. Citation 一致性验证 (Citation Consistency)
# ==============================================================================


def test_reranker_citation_consistency():
    """Test 13: Citations generated against reranked kept chunks match exactly."""
    c1 = create_fake_chunk("chunk-alpha", doc_name="arch.md", page_number=5, content="熔断阈值设为5000ms。")
    c1.reranker_score = 0.95
    c1.reranker_rank = 1

    llm_output = "根据系统配置规范，熔断阈值设为5000ms [Ref-1]。"
    citations = CitationExtractor.extract_citations(llm_output, [c1])

    assert len(citations) == 1
    assert citations[0].citation_index == 1
    assert citations[0].chunk_id == "chunk-alpha"
    assert citations[0].document_name == "arch.md"
    assert citations[0].page_number == 5


# ==============================================================================
# 11. SSE Trace 完整性验证 (SSE Trace Event)
# ==============================================================================


@pytest.mark.asyncio
async def test_reranker_sse_trace_event(db_session: AsyncSession):
    """Test 14: execute_stream yields retrieval and trace events with complete reranker fields."""
    emb_client = get_embedding_client()
    kb = KnowledgeBase(
        name="KB Trace",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="doc.md", file_type="md", file_path="/doc.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    content = "Redis哨兵用于高可用容灾切换与主从自动故障转移。"
    embs = await emb_client.embed_documents([content])

    chunk = DocumentChunk(
        knowledge_base_id=kb.id,
        document_id=doc.id,
        chunk_index=0,
        content=content,
        embedding_json=json.dumps(embs[0]),
    )
    db_session.add(chunk)
    await db_session.commit()

    engine = WorkflowRAGEngine(db_session)
    engine.reranker = CrossEncoderReranker(client=TestDoubleReranker())

    events = []
    req = ChatRequest(knowledge_base_id=kb.id, query="Redis哨兵的作用是什么？", top_k=4)
    async for event in engine.execute_stream(req):
        events.append(event)

    event_types = [e["event"] for e in events]
    assert "start" in event_types
    assert "retrieval" in event_types
    assert "token" in event_types
    assert "trace" in event_types
    assert "done" in event_types

    # Check retrieval event data
    retrieval_evt = next(e for e in events if e["event"] == "retrieval")
    rdata = retrieval_evt["data"]
    assert rdata["reranker_enabled"] is True
    assert "reranker_duration_ms" in rdata
    assert "reranker_candidates_in" in rdata
    assert "reranker_candidates_out" in rdata
    assert rdata["reranker_degraded"] is False

    # Check trace event data
    trace_evt = next(e for e in events if e["event"] == "trace")
    tdata = trace_evt["data"]
    assert tdata["reranker_enabled"] is True
    assert tdata["reranker_candidates_in"] >= 1
    assert tdata["reranker_candidates_out"] >= 1
    assert tdata["reranker_degraded"] is False


# ==============================================================================
# 12. 多轮 Query Rewriting 联动 (Rewritten Query Used in Reranker)
# ==============================================================================


@pytest.mark.asyncio
async def test_reranker_with_query_rewriter(db_session: AsyncSession):
    """Test 15: Rewritten query is passed into reranker for accurate relevance scoring."""
    emb_client = get_embedding_client()
    kb = KnowledgeBase(
        name="KB Rewrite",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    db_session.add(kb)
    await db_session.flush()

    conv = Conversation(knowledge_base_id=kb.id, title="多轮测试")
    db_session.add(conv)
    await db_session.flush()

    # Previous turns
    m1 = Message(conversation_id=conv.id, role="user", content="系统熔断器有什么工作状态？")
    m2 = Message(conversation_id=conv.id, role="assistant", content="系统包含 CLOSED、OPEN 和 HALF_OPEN 三种熔断状态。")
    db_session.add_all([m1, m2])

    doc = Document(knowledge_base_id=kb.id, file_name="lock.md", file_type="md", file_path="/lock.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    content = "系统三态熔断器在错误率超过50%时自动进入 OPEN 状态并阻断请求。"
    embs = await emb_client.embed_documents([content])

    chunk = DocumentChunk(
        knowledge_base_id=kb.id,
        document_id=doc.id,
        chunk_index=0,
        content=content,
        embedding_json=json.dumps(embs[0]),
    )
    db_session.add(chunk)
    await db_session.commit()

    engine = WorkflowRAGEngine(db_session)
    engine.reranker = CrossEncoderReranker(client=TestDoubleReranker())

    # User follow-up query: "它什么时候进入 OPEN 状态？"
    req = ChatRequest(
        knowledge_base_id=kb.id,
        conversation_id=conv.id,
        query="它什么时候进入 OPEN 状态？",
        top_k=4,
    )
    resp = await engine.execute(req)

    assert resp.execution_info.is_rewritten is True
    assert "OPEN" in (resp.execution_info.rewritten_query or "")
    assert resp.execution_info.reranker_enabled is True
    assert resp.execution_info.reranker_candidates_in >= 1


# ==============================================================================
# 13. 并发安全测试 (Concurrency Safety)
# ==============================================================================


@pytest.mark.asyncio
async def test_reranker_concurrency_safety():
    """Test 16: Multiple concurrent reranking requests complete safely without race conditions."""
    reranker = CrossEncoderReranker(client=TestDoubleReranker())

    candidates = [
        create_fake_chunk(f"chk-{i}", content=f"并发测试候选分块内容编号 {i}")
        for i in range(10)
    ]

    async def run_one(idx: int):
        query = f"并发请求查询词第 {idx} 号"
        res = await reranker.rerank(query=query, candidates=candidates, top_k=4)
        assert len(res.chunks) == 4
        assert res.degraded is False
        return res

    # Run 10 parallel requests
    tasks = [run_one(i) for i in range(10)]
    all_results = await asyncio.gather(*tasks)
    assert len(all_results) == 10
    for r in all_results:
        assert len(r.chunks) == 4
