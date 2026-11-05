import json
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
from app.retrieval.rrf_fusion import (
    HybridRetrievalResult,
    HybridRetriever,
    rrf_fuse,
)
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
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        document_name=doc_name,
        page_number=page_number,
        chunk_index=chunk_index,
        content=content,
        similarity_score=score,
    )


# ==============================================================================
# Unit Tests: RRF Algorithm, Deduplication, Weights, Tie-breaking
# ==============================================================================


def test_rrf_formula_calculation():
    """Test 1: Basic RRF formula calculation with default and custom parameters."""
    c1 = create_fake_chunk("chunk-1", score=0.9)
    c2 = create_fake_chunk("chunk-2", score=0.8)
    c3 = create_fake_chunk("chunk-3", score=0.7)

    # Dense: [c1, c2]
    # BM25:  [c2, c3]
    dense_list = [c1, c2]
    bm25_list = [c2, c3]

    # Default k=60, w_dense=1.0, w_bm25=1.0
    # c2 is at rank 2 in dense, rank 1 in bm25:
    # RRF(c2) = 1/(60+2) + 1/(60+1) = 1/62 + 1/61 =~ 0.016129 + 0.016393 = 0.032522
    # c1 is rank 1 in dense only: RRF(c1) = 1/(60+1) =~ 0.016393
    # c3 is rank 2 in bm25 only:  RRF(c3) = 1/(60+2) =~ 0.016129
    fused = rrf_fuse(dense_list, bm25_list, k=60)
    assert len(fused) == 3
    assert fused[0].chunk_id == "chunk-2"  # In both -> highest score
    assert fused[1].chunk_id == "chunk-1"  # Rank 1 in one retriever
    assert fused[2].chunk_id == "chunk-3"  # Rank 2 in one retriever

    expected_c2_score = round(1.0 / 62 + 1.0 / 61, 6)
    assert abs(fused[0].similarity_score - expected_c2_score) < 1e-5
    assert fused[0].dense_rank == 2
    assert fused[0].bm25_rank == 1

    # Custom k=20, w_dense=2.0, w_bm25=0.5
    fused_custom = rrf_fuse(dense_list, bm25_list, k=20, dense_weight=2.0, bm25_weight=0.5)
    # c1: 2.0 / (20 + 1) = 2/21 =~ 0.095238
    # c2: 2.0 / (20 + 2) + 0.5 / (20 + 1) = 2/22 + 0.5/21 =~ 0.090909 + 0.023810 = 0.114719
    assert fused_custom[0].chunk_id == "chunk-2"
    expected_custom_c2 = round(2.0 / 22 + 0.5 / 21, 6)
    assert abs(fused_custom[0].similarity_score - expected_custom_c2) < 1e-5


def test_rrf_deduplication_same_chunk():
    """Test 2: Ensure identical chunks appearing in both retrievers are merged and deduplicated."""
    c_shared = create_fake_chunk("chunk-shared", score=0.95)
    dense_list = [c_shared]
    bm25_list = [c_shared]

    fused = rrf_fuse(dense_list, bm25_list, k=60)
    assert len(fused) == 1
    assert fused[0].chunk_id == "chunk-shared"
    assert fused[0].dense_rank == 1
    assert fused[0].bm25_rank == 1
    assert fused[0].dense_score == 0.95
    assert fused[0].bm25_score == 0.95


def test_rrf_merge_distinct_chunks():
    """Test 3: Ensure non-overlapping candidates from both paths are correctly merged."""
    c_dense1 = create_fake_chunk("chunk-d1", score=0.88)
    c_dense2 = create_fake_chunk("chunk-d2", score=0.75)
    c_bm25_1 = create_fake_chunk("chunk-b1", score=5.2)
    c_bm25_2 = create_fake_chunk("chunk-b2", score=3.1)

    dense_list = [c_dense1, c_dense2]
    bm25_list = [c_bm25_1, c_bm25_2]

    fused = rrf_fuse(dense_list, bm25_list, k=60)
    assert len(fused) == 4
    ids = [c.chunk_id for c in fused]
    assert "chunk-d1" in ids
    assert "chunk-d2" in ids
    assert "chunk-b1" in ids
    assert "chunk-b2" in ids


def test_rrf_single_retriever_empty():
    """Test 4: Handle single retriever returning empty results gracefully."""
    c1 = create_fake_chunk("chunk-1", score=0.8)
    c2 = create_fake_chunk("chunk-2", score=0.7)

    # Dense non-empty, BM25 empty
    fused_dense = rrf_fuse([c1, c2], [], k=60)
    assert len(fused_dense) == 2
    assert fused_dense[0].chunk_id == "chunk-1"
    assert fused_dense[0].bm25_rank is None

    # Dense empty, BM25 non-empty
    fused_bm25 = rrf_fuse([], [c1, c2], k=60)
    assert len(fused_bm25) == 2
    assert fused_bm25[0].chunk_id == "chunk-1"
    assert fused_bm25[0].dense_rank is None

    # Both empty
    assert rrf_fuse([], [], k=60) == []


def test_rrf_deterministic_tie_breaking():
    """Test 7: Ensure deterministic sorting when two items have identical fusion scores."""
    # c_dense only rank 1: 1/(60+1) = 1/61
    c_dense = create_fake_chunk("chunk-B", score=0.8)
    # c_bm25 only rank 1: 1/(60+1) = 1/61
    c_bm25 = create_fake_chunk("chunk-A", score=4.0)

    # c_dense has dense_rank=1, dense_score=0.8
    # c_bm25 has dense_rank=None -> 999999
    # Key 3 (better dense rank) will place c_dense ahead of c_bm25
    fused = rrf_fuse([c_dense], [c_bm25], k=60)
    assert len(fused) == 2
    assert fused[0].chunk_id == "chunk-B"
    assert fused[1].chunk_id == "chunk-A"

    # When scores and ranks are completely identical, sort by chunk_id
    c_x1 = create_fake_chunk("chunk-Z", score=0.5)
    c_x2 = create_fake_chunk("chunk-A", score=0.5)
    fused_alpha = rrf_fuse([c_x1], [c_x2], k=60)
    # c_x1 has dense_rank=1, dense_score=0.5
    # c_x2 has bm25_rank=1, dense_score=None
    assert fused_alpha[0].chunk_id == "chunk-Z"


def test_rrf_chunk_metadata_preservation():
    """Test 9: Full preservation of document metadata (doc_id, page, index, content)."""
    c = create_fake_chunk(
        chunk_id="chunk-meta",
        doc_id="doc-999",
        doc_name="finance_report.pdf",
        page_number=42,
        chunk_index=7,
        content="2024年第四季度营收增长15%",
        score=0.91,
    )
    fused = rrf_fuse([c], [], k=60)
    assert len(fused) == 1
    fc = fused[0]
    assert fc.chunk_id == "chunk-meta"
    assert fc.document_id == "doc-999"
    assert fc.document_name == "finance_report.pdf"
    assert fc.page_number == 42
    assert fc.chunk_index == 7
    assert fc.content == "2024年第四季度营收增长15%"
    assert fc.dense_rank == 1
    assert fc.dense_score == 0.91


# ==============================================================================
# Integration Tests: HybridRetriever Exception Degradation
# ==============================================================================


@pytest.mark.asyncio
async def test_hybrid_retriever_single_path_dense_failure_degrades_to_bm25(db_session: AsyncSession):
    """Test 5a: Dense fails (exception), system gracefully degrades to BM25 Only."""
    hybrid = HybridRetriever(db_session)
    c_bm25 = create_fake_chunk("chunk-bm25", score=3.5)

    # Mock vector_retriever.search to raise exception
    hybrid.vector_retriever.search = AsyncMock(side_effect=RuntimeError("Vector DB timeout"))
    hybrid.bm25_retriever.search = AsyncMock(return_value=[c_bm25])

    res = await hybrid.search(knowledge_base_id="kb-test", query="系统熔断", top_k=2)

    assert res.degradation_status == "dense_failed"
    assert len(res.chunks) == 1
    assert res.chunks[0].chunk_id == "chunk-bm25"
    assert res.chunks[0].bm25_rank == 1


@pytest.mark.asyncio
async def test_hybrid_retriever_single_path_bm25_failure_degrades_to_dense(db_session: AsyncSession):
    """Test 5b: BM25 fails (exception), system gracefully degrades to Dense Only."""
    hybrid = HybridRetriever(db_session)
    c_dense = create_fake_chunk("chunk-dense", score=0.88)

    hybrid.vector_retriever.search = AsyncMock(return_value=[c_dense])
    hybrid.bm25_retriever.search = AsyncMock(side_effect=IOError("Corrupted BM25 disk file"))

    res = await hybrid.search(knowledge_base_id="kb-test", query="系统熔断", top_k=2)

    assert res.degradation_status == "bm25_failed"
    assert len(res.chunks) == 1
    assert res.chunks[0].chunk_id == "chunk-dense"
    assert res.chunks[0].dense_rank == 1


@pytest.mark.asyncio
async def test_hybrid_retriever_both_failed(db_session: AsyncSession):
    """Test 6: Both retrievers fail, returns both_failed status without crashing."""
    hybrid = HybridRetriever(db_session)
    hybrid.vector_retriever.search = AsyncMock(side_effect=RuntimeError("Vector DB crash"))
    hybrid.bm25_retriever.search = AsyncMock(side_effect=IOError("Disk full"))

    res = await hybrid.search(knowledge_base_id="kb-test", query="系统熔断", top_k=2)

    assert res.degradation_status == "both_failed"
    assert res.chunks == []
    assert res.dense_chunks_count == 0
    assert res.bm25_chunks_count == 0


# ==============================================================================
# Database & Multi-tenant KnowledgeBase Strict Isolation
# ==============================================================================


@pytest.mark.asyncio
async def test_hybrid_retriever_knowledge_base_strict_isolation(db_session: AsyncSession):
    """Test 8: Strict multi-tenant KnowledgeBase isolation under Hybrid Retrieval."""
    # KB A
    kb_a = KnowledgeBase(name="KB-A", embedding_model=settings.EMBEDDING_MODEL_NAME)
    # KB B
    kb_b = KnowledgeBase(name="KB-B", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add_all([kb_a, kb_b])
    await db_session.flush()

    # Documents
    doc_a = Document(knowledge_base_id=kb_a.id, file_name="doc_a.txt", file_type="txt", file_path="/dummy/a.txt", status="COMPLETED")
    doc_b = Document(knowledge_base_id=kb_b.id, file_name="doc_b.txt", file_type="txt", file_path="/dummy/b.txt", status="COMPLETED")
    db_session.add_all([doc_a, doc_b])
    await db_session.flush()

    dummy_vec = [0.1] * settings.EMBEDDING_DIMENSIONS
    chunk_a = DocumentChunk(
        document_id=doc_a.id,
        knowledge_base_id=kb_a.id,
        content="知识库A专有规范：订单超时阈值设置5000ms",
        embedding=dummy_vec,
    )
    chunk_b = DocumentChunk(
        document_id=doc_b.id,
        knowledge_base_id=kb_b.id,
        content="知识库B专有规范：用户登录凭证过期时间30分钟",
        embedding=dummy_vec,
    )
    db_session.add_all([chunk_a, chunk_b])
    await db_session.commit()

    hybrid = HybridRetriever(db_session)

    # Search in KB A
    res_a = await hybrid.search(knowledge_base_id=kb_a.id, query="超时阈值5000ms", top_k=5)
    for c in res_a.chunks:
        assert c.document_name == "doc_a.txt"
        assert "凭证过期" not in c.content

    # Search in KB B
    res_b = await hybrid.search(knowledge_base_id=kb_b.id, query="登录凭证", top_k=5)
    for c in res_b.chunks:
        assert c.document_name == "doc_b.txt"
        assert "5000ms" not in c.content


# ==============================================================================
# Context Budget & Citation Integration
# ==============================================================================


def test_rrf_context_budget_integration():
    """Test 10: RRF fused chunks cleanly pass into ContextBudgetManager."""
    manager = ContextBudgetManager(retrieval_token_budget=100)
    c1 = create_fake_chunk("c-1", content="短内容一号 " * 5, score=0.032)
    c2 = create_fake_chunk("c-2", content="长内容二号超预算 " * 40, score=0.016)

    fused_chunks = [c1, c2]

    plan = manager.allocate_and_assemble(
        query="测试问题",
        system_prompt="系统提示词",
        raw_history_messages=[],
        retrieved_chunks=fused_chunks,
    )

    # c1 is kept within budget, c2 may be pruned
    assert len(plan.kept_chunks) >= 1
    assert plan.kept_chunks[0].chunk_id == "c-1"
    assert plan.budget_info.retrieval_chunks_kept >= 1


def test_citation_accuracy_with_rrf_chunks():
    """Test 11: CitationExtractor strictly maps [Ref-X] to RRF fused chunks in order."""
    c1 = create_fake_chunk("c-1", doc_name="arch.pdf", page_number=1, score=0.033)
    c2 = create_fake_chunk("c-2", doc_name="db.pdf", page_number=5, score=0.021)
    fused_chunks = [c1, c2]

    answer = "根据微服务架构规范 [Ref-1]，以及数据库调优指南 [Ref-2]，系统执行顺畅。"
    citations = CitationExtractor.extract_citations(answer, fused_chunks)

    assert len(citations) == 2
    assert citations[0].citation_index == 1
    assert citations[0].chunk_id == "c-1"
    assert citations[0].document_name == "arch.pdf"

    assert citations[1].citation_index == 2
    assert citations[1].chunk_id == "c-2"
    assert citations[1].document_name == "db.pdf"

    # Fabricated reference [Ref-99] must be stripped
    answer_fab = "虚假引用 [Ref-99] 和正常引用 [Ref-1]"
    citations_fab = CitationExtractor.extract_citations(answer_fab, fused_chunks)
    assert len(citations_fab) == 1
    assert citations_fab[0].citation_index == 1


# ==============================================================================
# Full Workflow RAG & SSE Trace Integration Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_workflow_rag_execute_populates_rrf_metrics(db_session: AsyncSession):
    """Test 12a: WorkflowRAGEngine.execute populates hybrid retrieval execution metrics."""
    emb_client = get_embedding_client()
    kb = KnowledgeBase(
        name="KB-RRF",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="spec.md", file_type="md", file_path="/dummy/spec.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    content = "网关默认连接超时时间为3000ms，最大重试次数为3次。"
    embs = await emb_client.embed_documents([content])
    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content=content,
        embedding=embs[0],
    )
    db_session.add(chunk)
    await db_session.commit()

    engine = WorkflowRAGEngine(db_session)
    req = ChatRequest(knowledge_base_id=kb.id, query="网关连接超时时间是多少？", top_k=2)

    resp = await engine.execute(req)
    assert resp.execution_info.retrieval_mode == "hybrid"
    assert resp.execution_info.dense_duration_ms is not None
    assert resp.execution_info.bm25_duration_ms is not None
    assert resp.execution_info.rrf_duration_ms is not None
    assert resp.execution_info.dense_chunks_count is not None
    assert resp.execution_info.bm25_chunks_count is not None
    assert resp.execution_info.degradation_status == "none"


@pytest.mark.asyncio
async def test_sse_stream_retrieval_event_has_rrf_metrics(client: AsyncClient, db_session: AsyncSession):
    """Test 12b: SSE Stream emits retrieval event with hybrid metrics breakdown."""
    emb_client = get_embedding_client()
    kb = KnowledgeBase(
        name="KB-SSE-RRF",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="api_spec.md", file_type="md", file_path="/dummy/api_spec.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    content = "限流阈值设定为每秒1000个请求 (QPS=1000)。"
    embs = await emb_client.embed_documents([content])
    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content=content,
        embedding=embs[0],
    )
    db_session.add(chunk)
    await db_session.commit()

    payload = {
        "knowledge_base_id": kb.id,
        "query": "系统的限流阈值是多少？",
        "top_k": 2,
    }

    response = await client.post("/api/v1/chat/completions/stream", json=payload)
    assert response.status_code == 200

    content = response.text
    assert "event: retrieval" in content
    assert "dense_duration_ms" in content
    assert "bm25_duration_ms" in content
    assert "rrf_duration_ms" in content
    assert "hybrid" in content


@pytest.mark.asyncio
async def test_query_rewriting_compatibility_with_hybrid_retrieval(db_session: AsyncSession):
    """Test 13: Query Rewriting seamlessly passes rewritten query to both Dense and BM25."""
    emb_client = get_embedding_client()
    kb = KnowledgeBase(
        name="KB-QR-RRF",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="payment.md", file_type="md", file_path="/dummy/payment.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    content = "退款流程：用户申请退款后，T+1个工作日内原路返回微信支付账户。"
    embs = await emb_client.embed_documents([content])
    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content=content,
        embedding=embs[0],
    )
    db_session.add(chunk)
    await db_session.commit()

    # Pre-populate a conversation with history
    conv = Conversation(knowledge_base_id=kb.id, title="退款咨询")
    db_session.add(conv)
    await db_session.flush()

    msg_u1 = Message(conversation_id=conv.id, role="user", content="请问退款多久能到账？")
    msg_a1 = Message(conversation_id=conv.id, role="assistant", content="退款通常需要T+1个工作日。")
    db_session.add_all([msg_u1, msg_a1])
    await db_session.commit()

    engine = WorkflowRAGEngine(db_session)
    # Ellipsis question referencing previous context
    req = ChatRequest(
        knowledge_base_id=kb.id,
        conversation_id=conv.id,
        query="那会退到哪里呢？",
        top_k=2,
    )

    resp = await engine.execute(req)
    assert resp.execution_info.retrieval_mode == "hybrid"
    assert resp.execution_info.original_query == "那会退到哪里呢？"
    assert resp.execution_info.rewritten_query is not None
    # User message in DB must keep original query
    last_msg = (
        await db_session.execute(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.role == "user")
            .order_by(Message.created_at.desc())
        )
    ).scalars().first()
    assert last_msg.content == "那会退到哪里呢？"


@pytest.mark.asyncio
async def test_multi_turn_conversation_continuity(db_session: AsyncSession):
    """Test 14: Multi-turn conversation continuity with RRF hybrid retrieval."""
    emb_client = get_embedding_client()
    kb = KnowledgeBase(
        name="KB-MultiTurn",
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
        embedding_provider=emb_client.provider_name,
    )
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="hr.txt", file_type="txt", file_path="/dummy/hr.txt", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    c1_txt = "公司实行朝九晚六弹性工时制，每月允许迟到3次以内不扣除全勤奖。"
    c2_txt = "年假制度：入职满1年可享受5天带薪年休假，满10年享受10天。"
    embs = await emb_client.embed_documents([c1_txt, c2_txt])

    chunk1 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content=c1_txt,
        embedding=embs[0],
    )
    chunk2 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content=c2_txt,
        embedding=embs[1],
    )
    db_session.add_all([chunk1, chunk2])
    await db_session.commit()

    engine = WorkflowRAGEngine(db_session)

    # Turn 1
    req1 = ChatRequest(knowledge_base_id=kb.id, query="公司的上班时间是怎样的？")
    res1 = await engine.execute(req1)
    conv_id = res1.conversation_id
    assert conv_id is not None
    assert res1.execution_info.retrieval_mode == "hybrid"

    # Turn 2 in the same conversation
    req2 = ChatRequest(
        knowledge_base_id=kb.id,
        conversation_id=conv_id,
        query="满一年有几天年假？",
    )
    res2 = await engine.execute(req2)
    assert res2.conversation_id == conv_id
    assert res2.execution_info.retrieval_mode == "hybrid"

    # Check conversation message count
    all_msgs = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conv_id)
        )
    ).scalars().all()
    assert len(all_msgs) == 4  # 2 user + 2 assistant
