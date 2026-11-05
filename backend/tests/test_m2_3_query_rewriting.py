import asyncio
import json
from typing import Any, Dict, List
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.llm.base import BaseLLMClient
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.query_rewriter import QueryRewriter, QueryRewriteResult
from app.rag.workflow import WorkflowRAGEngine
from app.schemas.chat import ChatRequest


def parse_sse_events(raw_text: str) -> List[Dict[str, Any]]:
    """Parse raw SSE text (handling both LF and CRLF) into a list of dicts."""
    events = []
    # Normalize CRLF to LF
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


@pytest.mark.asyncio
async def test_no_history_skips_rewrite():
    """Test 1: When history is empty, query rewriter exits immediately without LLM call."""
    rewriter = QueryRewriter()
    res = await rewriter.rewrite(query="网关连接池最大连接数是多少？", history_messages=[])
    assert res.is_rewritten is False
    assert res.rewritten_query == "网关连接池最大连接数是多少？"
    assert res.original_query == "网关连接池最大连接数是多少？"
    assert res.duration_ms == 0.0
    assert res.fallback is False


@pytest.mark.asyncio
async def test_independent_question_preserves_original():
    """Test 2: Independent question not related to history is not force-rewritten."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "网关连接池最大连接数是多少？"},
        {"role": "assistant", "content": "网关连接池最大连接数为100。"},
    ]
    res = await rewriter.rewrite(query="Python 的装饰器是什么？", history_messages=history)
    assert res.is_rewritten is False
    assert res.rewritten_query == "Python 的装饰器是什么？"
    assert res.fallback is False


@pytest.mark.asyncio
async def test_ellipsis_omission_rewriting():
    """Test 3: Omission scenario ('那最小是多少？' -> '网关连接池的最小空闲连接数是多少？')."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "网关连接池最大连接数是多少？"},
        {"role": "assistant", "content": "网关连接池最大连接数为100 [Ref-1]。"},
    ]
    res = await rewriter.rewrite(query="那最小是多少？", history_messages=history)
    assert res.is_rewritten is True
    assert "网关连接池" in res.rewritten_query
    assert "最小" in res.rewritten_query
    assert res.original_query == "那最小是多少？"


@pytest.mark.asyncio
async def test_pronoun_coreference_rewriting():
    """Test 4: Pronoun coreference ('它什么时候进入 OPEN 状态？')."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "介绍一下系统的三态熔断器。"},
        {"role": "assistant", "content": "系统三态熔断器包含 CLOSED、OPEN、HALF_OPEN 三种状态。"},
    ]
    res = await rewriter.rewrite(query="它什么时候进入 OPEN 状态？", history_messages=history)
    assert res.is_rewritten is True
    assert "系统三态熔断器" in res.rewritten_query
    assert "OPEN" in res.rewritten_query


@pytest.mark.asyncio
async def test_ordinal_reference_rewriting():
    """Test 5: Ordinal reference ('第二种状态什么时候触发？' -> OPEN 状态)."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "熔断器包含 CLOSED、OPEN、HALF_OPEN 三种状态。"},
        {"role": "assistant", "content": "三种状态按健康度自动流转。"},
    ]
    res = await rewriter.rewrite(query="第二种状态什么时候触发？", history_messages=history)
    assert res.is_rewritten is True
    assert "OPEN" in res.rewritten_query


@pytest.mark.asyncio
async def test_continuous_multi_turn_rewriting():
    """Test 6: Continuous multi-turn follow-up across 3 turns."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "BGE-small 是什么模型？"},
        {"role": "assistant", "content": "BGE-small 是 BAAI 开源的 512 维轻量级中文语义嵌入模型。"},
        {"role": "user", "content": "它的向量维度是多少？"},
        {"role": "assistant", "content": "它的向量维度是 512 维。"},
    ]
    res = await rewriter.rewrite(query="那和 BGE-M3 有什么区别？", history_messages=history)
    assert res.is_rewritten is True
    assert "BGE-small" in res.rewritten_query
    assert "BGE-M3" in res.rewritten_query.upper()


@pytest.mark.asyncio
async def test_irrelevant_history_does_not_pollute():
    """Test 7: Irrelevant history doesn't contaminate an independent question."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "天气真好，去公园玩吧。"},
        {"role": "assistant", "content": "祝您玩得愉快！"},
    ]
    res = await rewriter.rewrite(query="Python 的装饰器是什么？", history_messages=history)
    assert res.is_rewritten is False
    assert "公园" not in res.rewritten_query
    assert "天气" not in res.rewritten_query


@pytest.mark.asyncio
async def test_timeout_fallback_to_original():
    """Test 8: Timeout protection gracefully falls back to original query."""
    rewriter = QueryRewriter(timeout=0.01)
    history = [
        {"role": "user", "content": "测试超时前置问题"},
        {"role": "assistant", "content": "前置回答"},
    ]
    res = await rewriter.rewrite(query="测试超时提问", history_messages=history)
    assert res.fallback is True
    assert res.fallback_reason == "timeout"
    assert res.is_rewritten is False
    assert res.rewritten_query == "测试超时提问"


@pytest.mark.asyncio
async def test_invalid_json_fallback_to_original():
    """Test 9: Invalid JSON response gracefully falls back to original query."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "前置问题"},
        {"role": "assistant", "content": "前置回答"},
    ]
    res = await rewriter.rewrite(query="测试非法JSON提问", history_messages=history)
    assert res.fallback is True
    assert res.fallback_reason == "invalid_json"
    assert res.is_rewritten is False
    assert res.rewritten_query == "测试非法JSON提问"


@pytest.mark.asyncio
async def test_empty_rewrite_fallback_to_original():
    """Test 10: Empty rewritten query fallback to original query."""
    rewriter = QueryRewriter()
    history = [
        {"role": "user", "content": "前置问题"},
        {"role": "assistant", "content": "前置回答"},
    ]
    res = await rewriter.rewrite(query="测试空结果提问", history_messages=history)
    assert res.fallback is True
    assert res.fallback_reason == "empty_rewritten_query"
    assert res.is_rewritten is False
    assert res.rewritten_query == "测试空结果提问"


@pytest.mark.asyncio
async def test_original_query_preserved_and_rewritten_for_embedding(client: AsyncClient, db_session: AsyncSession):
    """
    Test 11 & 12: In RAG flow, user message in DB preserves original query,
    while execution_info records both original_query and rewritten_query.
    """
    kb = KnowledgeBase(name="重写持久化知识库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="pool_spec.md",
        file_type="markdown",
        file_size=100,
        file_path="/tmp/pool_spec.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "网关连接池配置：min_idle最小空闲为10，max_connections最大连接数为100。"
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

    # Round 1: establishes history
    r1 = await client.post(
        "/api/v1/chat/completions",
        json={"knowledge_base_id": kb.id, "query": "网关连接池最大连接数是多少？"},
    )
    assert r1.status_code == 200
    conv_id = r1.json()["conversation_id"]

    # Round 2: follow-up query with omission
    r2 = await client.post(
        "/api/v1/chat/completions",
        json={"knowledge_base_id": kb.id, "conversation_id": conv_id, "query": "那最小是多少？"},
    )
    assert r2.status_code == 200
    r2_data = r2.json()

    # Verify execution_info has rewrite metrics
    exec_info = r2_data["execution_info"]
    assert exec_info["is_rewritten"] is True
    assert exec_info["original_query"] == "那最小是多少？"
    assert "网关连接池" in exec_info["rewritten_query"]
    assert exec_info["rewrite_duration_ms"] is not None

    # Verify DB User message preserves original query
    stmt = (
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at.desc())
    )
    db_msgs = (await db_session.execute(stmt)).scalars().all()
    # Most recent user message
    user_msgs = [m for m in db_msgs if m.role == "user"]
    assert user_msgs[0].content == "那最小是多少？"


@pytest.mark.asyncio
async def test_sse_query_rewrite_event(client: AsyncClient, db_session: AsyncSession):
    """
    Test 13: SSE stream emits query_rewrite event right after rewrite and before retrieval.
    """
    kb = KnowledgeBase(name="SSE重写事件库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="breaker.md",
        file_type="markdown",
        file_size=100,
        file_path="/tmp/breaker.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "系统三态熔断器：当失败率超过30%时进入 OPEN 状态阻断请求。"
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

    # Round 1
    r1 = await client.post(
        "/api/v1/chat/completions",
        json={"knowledge_base_id": kb.id, "query": "介绍一下系统的三态熔断器。"},
    )
    conv_id = r1.json()["conversation_id"]

    # Round 2: SSE stream
    resp = await client.post(
        "/api/v1/chat/completions/stream",
        json={
            "knowledge_base_id": kb.id,
            "conversation_id": conv_id,
            "query": "它什么时候进入 OPEN 状态？",
        },
    )
    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    event_names = [e["event"] for e in events]

    assert "start" in event_names
    assert "query_rewrite" in event_names
    assert "retrieval" in event_names

    # Check order: query_rewrite comes before retrieval
    rewrite_idx = event_names.index("query_rewrite")
    retrieval_idx = event_names.index("retrieval")
    assert rewrite_idx < retrieval_idx

    # Check query_rewrite event data
    rewrite_event = events[rewrite_idx]
    assert rewrite_event["data"]["is_rewritten"] is True
    assert rewrite_event["data"]["original_query"] == "它什么时候进入 OPEN 状态？"
    assert "系统三态熔断器" in rewrite_event["data"]["rewritten_query"]


@pytest.mark.asyncio
async def test_citations_work_with_rewritten_query(client: AsyncClient, db_session: AsyncSession):
    """
    Test 14: Citations extracted in rewritten query flow match the actual retrieved chunks.
    """
    kb = KnowledgeBase(name="重写引用库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="doc_a.md",
        file_type="markdown",
        file_size=80,
        file_path="/tmp/doc_a.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "网关连接池的最小空闲连接数 min_idle 规定为 10。"
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

    # Preload history turn
    conv = Conversation(knowledge_base_id=kb.id, title="引用测试")
    db_session.add(conv)
    await db_session.flush()

    m1 = Message(conversation_id=conv.id, role="user", content="网关连接池最大连接数是多少？")
    m2 = Message(conversation_id=conv.id, role="assistant", content="最大连接数是100。")
    db_session.add_all([m1, m2])
    await db_session.commit()

    # Follow up
    resp = await client.post(
        "/api/v1/chat/completions",
        json={"knowledge_base_id": kb.id, "conversation_id": conv.id, "query": "那最小是多少？"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["citations"]) >= 1
    assert data["citations"][0]["document_name"] == "doc_a.md"


@pytest.mark.asyncio
async def test_multi_turn_history_restoration_and_continuity(client: AsyncClient, db_session: AsyncSession):
    """
    Test 15: History messages are properly loaded from DB and cleaned of interruption tags.
    """
    kb = KnowledgeBase(name="会话恢复库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    conv = Conversation(knowledge_base_id=kb.id, title="多轮历史接续测试")
    db_session.add(conv)
    await db_session.flush()

    # Message with interruption tag
    m1 = Message(conversation_id=conv.id, role="user", content="介绍系统架构")
    m2 = Message(conversation_id=conv.id, role="assistant", content="系统包含微服务网关\n\n[回答已被用户中止]")
    db_session.add_all([m1, m2])
    await db_session.commit()

    rewriter = QueryRewriter()
    engine = WorkflowRAGEngine(db_session)

    # Check rewriter formats history without the aborted tag
    history_stmt = select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
    msgs = (await db_session.execute(history_stmt)).scalars().all()
    history_dicts = [{"role": m.role, "content": m.content} for m in msgs]

    formatted = rewriter.format_history_context(history_dicts)
    assert "[回答已被用户中止]" not in formatted
    assert "系统包含微服务网关" in formatted
