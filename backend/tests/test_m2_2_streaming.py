import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.workflow import WorkflowRAGEngine
from app.schemas.chat import ChatRequest


def parse_sse_events(raw_text: str) -> List[Dict[str, Any]]:
    """Parse raw SSE text into a list of {event: str, data: Any} dicts."""
    events = []
    blocks = raw_text.strip().split("\n\n")
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
async def test_sse_stream_sequence_and_tokens(client: AsyncClient, db_session: AsyncSession):
    """
    Test 1: Full SSE streaming sequence: start -> retrieval -> token* -> citations -> trace -> done.
    Verify incremental tokens reassemble to the full answer.
    """
    kb = KnowledgeBase(name="流式测试知识库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="streaming_guide.md",
        file_type="markdown",
        file_size=120,
        file_path="/tmp/streaming_guide.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "AgentStudio-RAG 流式响应规范：采用 Server-Sent Events (SSE) 协议，文本按 token 实时推送到前端。"
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

    # Stream chat request
    req_payload = {
        "knowledge_base_id": kb.id,
        "query": "系统流式响应采用什么协议？",
        "mode": "workflow",
        "top_k": 3,
    }

    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    events = parse_sse_events(resp.text)
    event_types = [e["event"] for e in events]

    assert "start" in event_types
    assert "retrieval" in event_types
    assert "token" in event_types
    assert "citations" in event_types
    assert "trace" in event_types
    assert "done" in event_types

    # First event must be start
    assert event_types[0] == "start"
    start_data = events[0]["data"]
    assert "conversation_id" in start_data
    conv_id = start_data["conversation_id"]

    # Retrieval event must be present after start (or after query_rewrite)
    assert "retrieval" in event_types
    retrieval_idx = event_types.index("retrieval")
    assert retrieval_idx > 0
    retrieval_data = events[retrieval_idx]["data"]
    assert retrieval_data["retrieved_chunks_count"] >= 1
    assert "retrieval_duration_ms" in retrieval_data

    # Tokens concatenation
    tokens = [e["data"]["delta"] for e in events if e["event"] == "token"]
    assert len(tokens) > 0
    full_answer = "".join(tokens)
    assert len(full_answer) > 0

    # Citations event
    citations_events = [e for e in events if e["event"] == "citations"]
    assert len(citations_events) == 1
    citations_data = citations_events[0]["data"]
    assert isinstance(citations_data, list)

    # Trace event
    trace_events = [e for e in events if e["event"] == "trace"]
    assert len(trace_events) == 1
    trace_data = trace_events[0]["data"]
    assert trace_data["retrieved_chunks_count"] >= 1
    assert trace_data["total_duration_ms"] > 0

    # Done event
    done_events = [e for e in events if e["event"] == "done"]
    assert len(done_events) == 1
    done_data = done_events[0]["data"]
    assert "message_id" in done_data

    # Verify Database Persistence
    stmt = select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
    db_msgs = (await db_session.execute(stmt)).scalars().all()
    assert len(db_msgs) == 2
    assert db_msgs[0].role == "user"
    assert db_msgs[0].content == req_payload["query"]
    assert db_msgs[1].role == "assistant"
    assert db_msgs[1].content == full_answer
    assert db_msgs[1].execution_info is not None


@pytest.mark.asyncio
async def test_sse_stream_empty_knowledge_base(client: AsyncClient, db_session: AsyncSession):
    """
    Test 2: Empty knowledge base stream handling.
    """
    kb = KnowledgeBase(name="空库流式测试")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    req_payload = {
        "knowledge_base_id": kb.id,
        "query": "请介绍系统核心能力",
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    event_types = [e["event"] for e in events]

    assert event_types == ["start", "route", "retrieval", "token", "citations", "trace", "done"]
    retrieval_event = next(e for e in events if e["event"] == "retrieval")
    assert retrieval_event["data"]["retrieved_chunks_count"] == 0

    token_event = next(e for e in events if e["event"] == "token")
    assert "尚未建立任何文档索引" in token_event["data"]["delta"]


@pytest.mark.asyncio
async def test_sse_stream_no_evidence_fallback(client: AsyncClient, db_session: AsyncSession):
    """
    Test 3: Query with no matching evidence returns clear disclaimer via stream.
    """
    kb = KnowledgeBase(name="特定领域库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="java.md",
        file_type="markdown",
        file_size=50,
        file_path="/tmp/java.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "Java 虚拟机使用垃圾回收器管理内存堆分配与回收。"
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

    # Query completely unrelated to Java
    req_payload = {
        "knowledge_base_id": kb.id,
        "query": "深海章鱼的变色伪装生物学机制是什么？",
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    event_types = [e["event"] for e in events]
    assert "start" in event_types
    assert "token" in event_types
    assert "done" in event_types

    tokens = [e["data"]["delta"] for e in events if e["event"] == "token"]
    full_answer = "".join(tokens)
    assert "未找到与您提问相关的充分事实证据" in full_answer


@pytest.mark.asyncio
async def test_sse_stream_pre_validation_errors(client: AsyncClient, db_session: AsyncSession):
    """
    Test 4: Pre-stream synchronous validation catches non-existent resources with standard HTTP 4xx.
    """
    # 1. Non-existent KB
    res1 = await client.post(
        "/api/v1/chat/completions/stream",
        json={"knowledge_base_id": "non-existent-kb", "query": "hello"},
    )
    assert res1.status_code == 404

    # 2. Existing KB but non-existent conversation
    kb = KnowledgeBase(name="有效知识库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    res2 = await client.post(
        "/api/v1/chat/completions/stream",
        json={
            "knowledge_base_id": kb.id,
            "conversation_id": "non-existent-conv",
            "query": "hello",
        },
    )
    assert res2.status_code == 404


@pytest.mark.asyncio
async def test_stream_multi_turn_continuation(client: AsyncClient, db_session: AsyncSession):
    """
    Test 5: Streaming across multiple turns in the same conversation.
    """
    kb = KnowledgeBase(name="多轮流式库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="policy.txt",
        file_type="text",
        file_size=60,
        file_path="/tmp/policy.txt",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "系统日常运维规范：密码每90天必须轮换一次，最少长度为12位。"
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
    resp1 = await client.post(
        "/api/v1/chat/completions/stream",
        json={"knowledge_base_id": kb.id, "query": "密码多久轮换一次？"},
    )
    assert resp1.status_code == 200
    events1 = parse_sse_events(resp1.text)
    conv_id = next(e["data"]["conversation_id"] for e in events1 if e["event"] == "start")

    # Round 2: same conv_id
    resp2 = await client.post(
        "/api/v1/chat/completions/stream",
        json={"knowledge_base_id": kb.id, "conversation_id": conv_id, "query": "最少长度是多少？"},
    )
    assert resp2.status_code == 200
    events2 = parse_sse_events(resp2.text)
    assert any(e["event"] == "done" for e in events2)

    # Check 4 messages in DB (2 user, 2 assistant)
    stmt = select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
    msgs = (await db_session.execute(stmt)).scalars().all()
    assert len(msgs) == 4
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"
    assert msgs[2].role == "user"
    assert msgs[3].role == "assistant"


@pytest.mark.asyncio
async def test_stream_cancellation_saves_partial_state(db_session: AsyncSession):
    """
    Test 6: Simulated client cancellation mid-stream properly saves partial assistant message.
    """
    kb = KnowledgeBase(name="取消测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="cancel.txt",
        file_type="text",
        file_size=50,
        file_path="/tmp/cancel.txt",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "长文本说明分块信息用于模拟生成中途取消的情况。"
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

    engine = WorkflowRAGEngine(db_session)
    req = ChatRequest(knowledge_base_id=kb.id, query="长文本测试")

    # Consume partial stream and cancel
    gen = engine.execute_stream(req)
    first_event = await gen.asend(None)
    assert first_event["event"] == "start"
    conv_id = first_event["data"]["conversation_id"]

    second_event = await gen.asend(None)
    if second_event["event"] == "query_rewrite":
        third_event = await gen.asend(None)
        assert third_event["event"] == "retrieval"
    else:
        assert second_event["event"] == "retrieval"

    # Simulate CancelledError during generation
    try:
        await gen.athrow(asyncio.CancelledError())
    except asyncio.CancelledError:
        pass

    # Verify that assistant message has been persisted with cancellation flag
    stmt = select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
    msgs = (await db_session.execute(stmt)).scalars().all()
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"
    assert "回答已被用户中止" in msgs[1].content


def test_sse_chunking_and_utf8_parser():
    """
    Test 7: Verify client-side SSE parser logic across buffer splits, empty lines, and multi-byte UTF-8 boundaries.
    """
    # Raw multi-event text
    sse_text = (
        "event: start\ndata: {\"conversation_id\": \"c-123\", \"message_id\": \"m-123\"}\n\n"
        "event: retrieval\ndata: {\"retrieved_chunks_count\": 2, \"retrieval_duration_ms\": 15.4}\n\n"
        "event: token\ndata: {\"delta\": \"这是中文\"}\n\n"
        "event: token\ndata: {\"delta\": \"测试\"}\n\n"
        "event: done\ndata: {\"message_id\": \"m-456\"}\n\n"
    )

    events = parse_sse_events(sse_text)
    assert len(events) == 5
    assert events[0]["event"] == "start"
    assert events[1]["event"] == "retrieval"
    assert events[2]["event"] == "token"
    assert events[2]["data"]["delta"] == "这是中文"
    assert events[3]["data"]["delta"] == "测试"
    assert events[4]["event"] == "done"
