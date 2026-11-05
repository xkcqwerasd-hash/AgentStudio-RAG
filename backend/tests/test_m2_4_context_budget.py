import asyncio
import json
from typing import Any, Dict, List
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ContextBudgetExceededError, QueryTooLongError
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.citation import CitationExtractor
from app.rag.context_budget import (
    ContextBudgetManager,
    TokenEstimator,
    estimate_tokens,
)
from app.retrieval.vector_store import ScoredChunk


def parse_sse_events(raw_text: str) -> List[Dict[str, Any]]:
    """Parse raw SSE text (handling both LF and CRLF) into a list of dicts."""
    events = []
    normalized = raw_text.replace("\r\n", "\n")
    blocks = normalized.strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        event_type = "message"
        data_str = ""
        for line in lines:
            line = line.strip()
            if line.startswith("event:"):
                event_type = line.replace("event:", "", 1).strip()
            elif line.startswith("data:"):
                d = line.replace("data:", "", 1).strip()
                data_str = data_str + "\n" + d if data_str else d
        if data_str:
            try:
                data = json.loads(data_str)
            except Exception:
                data = data_str
            events.append({"event": event_type, "data": data})
    return events


def create_fake_chunk(
    chunk_id: str,
    content: str,
    score: float,
    doc_name: str = "doc.txt",
    page: int = 1,
    chunk_index: int = 0,
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=f"doc_{chunk_id}",
        document_name=doc_name,
        page_number=page,
        chunk_index=chunk_index,
        content=content,
        similarity_score=score,
    )


# ==============================================================================
# Unit Tests for TokenEstimator & ContextBudgetManager
# ==============================================================================


def test_token_estimator_fallbacks_and_accuracy():
    """Test 1: Token estimator handles empty, English, CJK, and mixed symbols accurately."""
    estimator = TokenEstimator()
    assert estimator.estimate("") == 0
    assert estimator.estimate(None) == 0

    cjk_text = "这是一段用于测试中文字符Token估算准确性的示例文本。"
    cjk_tokens = estimator.estimate(cjk_text)
    assert cjk_tokens >= 20  # ~26 Chinese chars + punctuation

    en_text = "The quick brown fox jumps over the lazy dog."
    en_tokens = estimator.estimate(en_text)
    assert 8 <= en_tokens <= 15

    mixed_text = "DeepSeek-V3 采用了 671B 参数模型，其中每个 Token 激活 37B。"
    mixed_tokens = estimator.estimate(mixed_text)
    assert 20 <= mixed_tokens <= 50

    assert estimator.method in ("tokenizers_hf", "tiktoken", "conservative_heuristic")


def test_short_history_normal_assembly():
    """Test 2: Short history within budget is preserved in full without pruning."""
    mgr = ContextBudgetManager(history_token_budget=2000)
    history = [
        {"role": "user", "content": "你好，请问系统架构是什么？"},
        {"role": "assistant", "content": "系统采用模块化分层微服务架构设计。"},
    ]

    kept, tokens, kept_cnt, pruned_cnt = mgr.prune_history(history, budget=1000)
    assert len(kept) == 2
    assert kept_cnt == 2
    assert pruned_cnt == 0
    assert tokens > 0


def test_long_history_turn_pair_pruning():
    """Test 3: Long conversation exceeding history budget drops oldest turns first."""
    mgr = ContextBudgetManager()
    # 5 turns (10 messages)
    history = []
    for i in range(1, 6):
        history.append({"role": "user", "content": f"用户历史提问第{i}轮：详细说明微服务架构第{i}个模块的功能特性。"})
        history.append({"role": "assistant", "content": f"助手历史回答第{i}轮：第{i}个模块主要负责高可用服务发现与流量治理管控。"})

    # Set very small budget that only fits ~1-2 turns
    kept, tokens, kept_cnt, pruned_cnt = mgr.prune_history(history, budget=80)
    assert kept_cnt < len(history)
    assert pruned_cnt > 0
    # The kept messages must be the most recent ones (turn 5)
    assert "第5轮" in kept[-1]["content"]


def test_turn_pair_preservation_integrity():
    """Test 4: Turn pairs are kept as pairs; never leaves an orphan assistant message."""
    mgr = ContextBudgetManager()
    history = [
        {"role": "user", "content": "第1问：数据库主从复制原理？"},
        {"role": "assistant", "content": "第1答：主库写binlog，从库I/O线程同步并在relay log重放。"},
        {"role": "user", "content": "第2问：如何避免主从延迟？"},
        {"role": "assistant", "content": "第2答：并行复制、分库分表及关键业务强制读主库。"},
    ]

    # Test grouping
    turns = mgr._group_messages_into_turns(history)
    assert len(turns) == 2
    assert len(turns[0]) == 2
    assert turns[0][0]["role"] == "user"
    assert turns[0][1]["role"] == "assistant"

    # Pruned history should maintain turn pairs
    kept, _, kept_cnt, _ = mgr.prune_history(history, budget=65)
    # Kept should either be 2 or 4 messages, never 1 or 3
    assert len(kept) in (2, 4)
    if len(kept) == 2:
        assert kept[0]["role"] == "user"
        assert kept[1]["role"] == "assistant"
        assert "第2问" in kept[0]["content"]


def test_current_user_query_protection():
    """Test 5: User query has highest protection priority and is never truncated."""
    mgr = ContextBudgetManager(max_query_tokens=2000)
    query = "请详细分析系统的熔断降级机制及其在流量突增时的表现。"
    tokens = mgr.validate_user_query(query)
    assert tokens > 0

    plan = mgr.allocate_and_assemble(
        query=query,
        system_prompt="系统提示词",
        raw_history_messages=[],
        retrieved_chunks=[],
    )
    assert plan.budget_info.query_tokens == tokens


def test_query_too_long_error_raised():
    """Test 6: When user query exceeds MAX_QUERY_TOKENS, raises QueryTooLongError."""
    mgr = ContextBudgetManager(max_query_tokens=10)
    long_query = "这是一个特别特别长的问题，绝对超过了十个token的极小预算限制阈值。"
    with pytest.raises(QueryTooLongError) as exc_info:
        mgr.validate_user_query(long_query)
    assert exc_info.value.status_code == 400
    assert "用户提问长度超限" in exc_info.value.detail


def test_system_prompt_protection():
    """Test 7: System prompt tokens are accounted for and protected."""
    mgr = ContextBudgetManager()
    sys_prompt = "你是由 AgentStudio-RAG 驱动的企业级知识助手，请严格依据参考资料回答。"
    plan = mgr.allocate_and_assemble(
        query="你好",
        system_prompt=sys_prompt,
        raw_history_messages=[],
        retrieved_chunks=[],
    )
    assert plan.budget_info.system_prompt_tokens > 0
    assert plan.budget_info.estimated_input_tokens > plan.budget_info.system_prompt_tokens


def test_retrieval_chunks_score_priority_pruning():
    """Test 8: Chunks exceeding budget are pruned from lowest score, preserving highest score."""
    mgr = ContextBudgetManager()
    chunks = [
        create_fake_chunk("c1", "最高相关度证据：系统核心连接池初始大小为10个连接。", 0.95),
        create_fake_chunk("c2", "次高相关度证据：系统连接池最大连接数上限配置为100。", 0.88),
        create_fake_chunk("c3", "中等相关度证据：系统支持连接超时自动回收策略机制。", 0.75),
        create_fake_chunk("c4", "较低相关度证据：系统历史日志记录了常规调试排查信息。", 0.60),
    ]

    # Set budget that fits 2 chunks (~110-130 tokens) but not 3 or 4
    kept, tokens, kept_cnt, pruned_cnt = mgr.prune_retrieval_chunks(chunks, budget=150)
    assert kept_cnt == 2
    assert pruned_cnt == 2
    assert kept[0].chunk_id == "c1"
    assert kept[1].chunk_id == "c2"
    assert kept[0].similarity_score == 0.95
    assert kept[1].similarity_score == 0.88


def test_citation_consistency_with_kept_chunks():
    """Test 9: [Ref-X] citations and CitationExtractor strictly reference kept chunks after pruning."""
    mgr = ContextBudgetManager()
    chunks = [
        create_fake_chunk("c_keep_1", "微服务注册中心基于 Raft 一致性协议选主。", 0.96, "raft.md", 1),
        create_fake_chunk("c_keep_2", "服务心跳探测周期默认配置为 5 秒一次。", 0.89, "heartbeat.md", 2),
        create_fake_chunk("c_drop_3", "历史版本旧协议采用 Gossip 协议最终一致性。", 0.62, "legacy.md", 5),
    ]

    # Chunks are ~45 tokens each formatted. Budget 105 fits exactly 2 chunks (~88 tokens) but drops 3rd (~133 tokens)
    kept, _, kept_cnt, pruned_cnt = mgr.prune_retrieval_chunks(chunks, budget=105)
    assert kept_cnt == 2
    assert pruned_cnt == 1

    # Simulate model answer citing Ref-1 and Ref-2
    answer = "根据系统资料 [Ref-1]，注册中心采用 Raft 协议；同时心跳探测为 5 秒 [Ref-2]。"
    citations = CitationExtractor.extract_citations(answer, kept)

    assert len(citations) == 2
    assert citations[0].chunk_id == "c_keep_1"
    assert citations[0].document_name == "raft.md"
    assert citations[1].chunk_id == "c_keep_2"
    assert citations[1].document_name == "heartbeat.md"

    # Verify dropped chunk "c_drop_3" is never in citations
    citation_ids = [c.chunk_id for c in citations]
    assert "c_drop_3" not in citation_ids


def test_dynamic_slack_transfer():
    """Test 10: When history is small, unused history budget transfers to retrieval."""
    mgr = ContextBudgetManager(
        model_context_window=8192,
        max_output_tokens=1024,
        context_safety_margin=512,
        history_token_budget=1024,
        retrieval_token_budget=4096,
    )

    chunks = [
        create_fake_chunk(f"c_{i}", f"证据分块内容第{i}段：详述系统内部各个模块的数据字典规范与接口定义。", 0.9 - i * 0.05)
        for i in range(8)
    ]

    # Case A: with 0 history messages, slack from history can be accommodated in context
    plan_no_hist = mgr.allocate_and_assemble(
        query="系统数据字典规范是什么？",
        system_prompt="系统提示词",
        raw_history_messages=[],
        retrieved_chunks=chunks,
    )
    assert plan_no_hist.budget_info.history_tokens == 0
    assert plan_no_hist.budget_info.retrieval_chunks_kept >= 4


def test_different_model_context_window_configs():
    """Test 11: Configurable model context window (e.g. 4096 vs 32768) dynamically changes input budget."""
    mgr_small = ContextBudgetManager(
        model_context_window=4096,
        max_output_tokens=1024,
        context_safety_margin=512,
    )
    assert mgr_small.available_input_budget == 4096 - 1024 - 512

    mgr_large = ContextBudgetManager(
        model_context_window=32768,
        max_output_tokens=4096,
        context_safety_margin=2048,
    )
    assert mgr_large.available_input_budget == 32768 - 4096 - 2048
    assert mgr_large.available_input_budget > mgr_small.available_input_budget


def test_query_rewriter_dedicated_history_budget():
    """Test 12: prune_history_for_rewriter limits turns and tokens appropriately for rewriter."""
    mgr = ContextBudgetManager()
    history = []
    for i in range(1, 10):
        history.append({"role": "user", "content": f"这是历史第{i}问"})
        history.append({"role": "assistant", "content": f"这是历史第{i}答"})

    rewriter_hist = mgr.prune_history_for_rewriter(history, max_turns=3)
    # 3 turns = 6 messages
    assert len(rewriter_hist) == 6
    assert rewriter_hist[-1]["content"] == "这是历史第9答"


# ==============================================================================
# End-to-End API Integration & Streaming Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_workflow_rag_execute_populates_budget_info(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 13: Full chat API execution populates all context budget fields in
    execution_info and persists them in DB.
    """
    kb = KnowledgeBase(name="Token预算测试知识库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="budget_spec.md",
        file_type="markdown",
        file_size=120,
        file_path="/tmp/budget_spec.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "网关限流配置：令牌桶每秒产生1000个令牌，桶最大容量为5000。"
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

    resp = await client.post(
        "/api/v1/chat/completions",
        json={"knowledge_base_id": kb.id, "query": "网关限流令牌桶容量是多少？"},
    )
    assert resp.status_code == 200
    data = resp.json()

    exec_info = data["execution_info"]
    assert "model_context_window" in exec_info
    assert exec_info["model_context_window"] == settings.MODEL_CONTEXT_WINDOW
    assert "estimated_input_tokens" in exec_info
    assert exec_info["estimated_input_tokens"] > 0
    assert "reserved_output_tokens" in exec_info
    assert exec_info["reserved_output_tokens"] == settings.MAX_OUTPUT_TOKENS
    assert "retrieval_chunks_kept" in exec_info
    assert exec_info["retrieval_chunks_kept"] >= 1
    assert "budget_estimation_method" in exec_info

    # Verify DB persistence of execution_info
    conv_id = data["conversation_id"]
    stmt = (
        select(Message)
        .where(Message.conversation_id == conv_id, Message.role == "assistant")
    )
    assistant_msg = (await db_session.execute(stmt)).scalar_one()
    assert assistant_msg.execution_info is not None
    assert assistant_msg.execution_info["estimated_input_tokens"] == exec_info["estimated_input_tokens"]


@pytest.mark.asyncio
async def test_workflow_rag_execute_stream_emits_budget_event(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 14: Streaming includes context_budget event with complete ContextBudgetInfo.
    """
    kb = KnowledgeBase(name="SSE预算事件测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="stream_budget.md",
        file_type="markdown",
        file_size=120,
        file_path="/tmp/stream_budget.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "分布式调度器通过两阶段提交协议保证任务分发原子性。"
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

    resp = await client.post(
        "/api/v1/chat/completions/stream",
        json={"knowledge_base_id": kb.id, "query": "分布式调度器如何保证任务原子性？"},
    )
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)

    event_names = [e["event"] for e in events]
    assert "start" in event_names
    assert "retrieval" in event_names
    assert "context_budget" in event_names
    assert "token" in event_names
    assert "citations" in event_names
    assert "trace" in event_names
    assert "done" in event_names

    # Verify context_budget payload
    budget_event = next(e for e in events if e["event"] == "context_budget")
    bdata = budget_event["data"]
    assert bdata["model_context_window"] == settings.MODEL_CONTEXT_WINDOW
    assert bdata["estimated_input_tokens"] > 0
    assert bdata["retrieval_chunks_kept"] >= 1
    assert "history_tokens" in bdata
    assert "retrieval_tokens" in bdata


@pytest.mark.asyncio
async def test_stream_query_too_long_returns_400(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 15: Streaming chat pre-validation rejects excessively long query with 400 Bad Request.
    """
    kb = KnowledgeBase(name="超长提问拒绝测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    # Construct query longer than MAX_QUERY_TOKENS (default 2048)
    excessive_query = "超长提问测试内容 " * 1200
    resp = await client.post(
        "/api/v1/chat/completions/stream",
        json={"knowledge_base_id": kb.id, "query": excessive_query},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "用户提问长度超限" in data["detail"]


@pytest.mark.asyncio
async def test_multi_turn_budget_continuity(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 16: Multi-turn chat maintains context continuity and dynamically tracks budget.
    """
    kb = KnowledgeBase(name="多轮预算连续性测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="flow.md",
        file_type="markdown",
        file_size=120,
        file_path="/tmp/flow.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "流量控制器支持 QPS 阈值限流与并发线程数限流两种模式。"
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

    # Turn 1
    r1 = await client.post(
        "/api/v1/chat/completions",
        json={"knowledge_base_id": kb.id, "query": "流量控制器支持哪些限流模式？"},
    )
    assert r1.status_code == 200
    conv_id = r1.json()["conversation_id"]
    t1_info = r1.json()["execution_info"]
    assert t1_info["history_messages_kept"] == 0

    # Turn 2 (with existing history)
    r2 = await client.post(
        "/api/v1/chat/completions",
        json={
            "knowledge_base_id": kb.id,
            "conversation_id": conv_id,
            "query": "那第一种模式在突发流量时表现如何？",
        },
    )
    assert r2.status_code == 200
    t2_info = r2.json()["execution_info"]
    assert t2_info["history_messages_kept"] == 2
    assert t2_info["history_tokens"] > 0
    assert t2_info["retrieval_chunks_kept"] >= 1
