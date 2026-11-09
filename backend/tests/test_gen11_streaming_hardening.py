import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.core.config import settings
from app.llm.embeddings import get_embedding_client
from app.llm.openai_client import OpenAILLMClient, TestDoubleLLMClient
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.workflow import WorkflowRAGEngine
from app.schemas.chat import ChatRequest, ExecutionInfo


class MockStreamResponse:
    """Mock an httpx streaming response with custom SSE lines."""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            yield line
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_openai_client_stream_finish_reason_stop():
    """
    Test 1: Normal streaming completion with finish_reason='stop'.
    Verify tokens yielded, response_metadata has finish_reason='stop' and generation_status='completed'.
    """
    client = OpenAILLMClient(api_key="sk-test-key-mock", base_url="https://api.deepseek.com")

    sse_lines = [
        'data: {"id":"chat-1","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}',
        'data: {"id":"chat-1","choices":[{"index":0,"delta":{"content":"，世界！"},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]

    mock_resp = MockStreamResponse(sse_lines)
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_resp)

    meta = {}
    with patch("app.llm.openai_client.get_ssrf_safe_async_client") as mock_ssrf:
        mock_ssrf.return_value.__aenter__.return_value = mock_http_client

        tokens = []
        async for token in client.generate_stream("你好", response_metadata=meta):
            tokens.append(token)

    assert "".join(tokens) == "你好，世界！"
    assert meta["finish_reason"] == "stop"
    assert meta["is_truncated"] is False
    assert meta["generation_status"] == "completed"
    assert meta["completed"] is True


@pytest.mark.asyncio
async def test_openai_client_stream_finish_reason_length():
    """
    Test 2: Stream truncated by token limit with finish_reason='length'.
    Verify partial tokens are yielded, response_metadata records is_truncated=True and generation_status='truncated'.
    """
    client = OpenAILLMClient(api_key="sk-test-key-mock", base_url="https://api.deepseek.com")

    sse_lines = [
        'data: {"id":"chat-2","choices":[{"index":0,"delta":{"content":"前半段完整回答。"},"finish_reason":null}]}',
        'data: {"id":"chat-2","choices":[{"index":0,"delta":{"content":"后续内容因Token"},"finish_reason":"length"}]}',
        'data: [DONE]',
    ]

    mock_resp = MockStreamResponse(sse_lines)
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_resp)

    meta = {}
    with patch("app.llm.openai_client.get_ssrf_safe_async_client") as mock_ssrf:
        mock_ssrf.return_value.__aenter__.return_value = mock_http_client

        tokens = []
        async for token in client.generate_stream("长篇大论提问", response_metadata=meta):
            tokens.append(token)

    assert "".join(tokens) == "前半段完整回答。后续内容因Token"
    assert meta["finish_reason"] == "length"
    assert meta["is_truncated"] is True
    assert meta["generation_status"] == "truncated"
    assert meta["completed"] is True


@pytest.mark.asyncio
async def test_openai_client_stream_network_interruption():
    """
    Test 3: Network interruption mid-stream.
    Verify yielded partial tokens are not lost, exception is recorded in response_metadata with generation_status='interrupted'.
    """
    client = OpenAILLMClient(api_key="sk-test-key-mock", base_url="https://api.deepseek.com")

    sse_lines = [
        'data: {"id":"chat-3","choices":[{"index":0,"delta":{"content":"连接正常接收片段1"},"finish_reason":null}]}',
        'data: {"id":"chat-3","choices":[{"index":0,"delta":{"content":"，片段2"},"finish_reason":null}]}',
        httpx.RemoteProtocolError("Peer disconnected prematurely"),
    ]

    mock_resp = MockStreamResponse(sse_lines)
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_resp)

    meta = {}
    tokens = []
    with patch("app.llm.openai_client.get_ssrf_safe_async_client") as mock_ssrf:
        mock_ssrf.return_value.__aenter__.return_value = mock_http_client

        with pytest.raises(httpx.RemoteProtocolError):
            async for token in client.generate_stream("测试中断提问", response_metadata=meta):
                tokens.append(token)

    assert "".join(tokens) == "连接正常接收片段1，片段2"
    assert meta["generation_status"] == "interrupted"
    assert meta["completed"] is False
    assert "Peer disconnected" in meta["error"]


@pytest.mark.asyncio
async def test_openai_client_stream_trailing_usage_and_reasoning_tokens():
    """
    Test 4: Trailing chunk with usage and reasoning_tokens.
    Verify reasoning_tokens extracted from completion_tokens_details without emitting reasoning_content.
    """
    client = OpenAILLMClient(api_key="sk-test-key-mock", base_url="https://api.deepseek.com")

    sse_lines = [
        # Chunk 1: content + reasoning_content (which should NOT be yielded)
        'data: {"id":"chat-4","choices":[{"index":0,"delta":{"content":"回答正文","reasoning_content":"思考过程文本"},"finish_reason":null}]}',
        # Chunk 2: stop signal
        'data: {"id":"chat-4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        # Chunk 3: trailing chunk with usage & reasoning_tokens (empty choices)
        'data: {"id":"chat-4","choices":[],"usage":{"prompt_tokens":120,"completion_tokens":85,"completion_tokens_details":{"reasoning_tokens":48}}}',
        'data: [DONE]',
    ]

    mock_resp = MockStreamResponse(sse_lines)
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_resp)

    meta = {}
    with patch("app.llm.openai_client.get_ssrf_safe_async_client") as mock_ssrf:
        mock_ssrf.return_value.__aenter__.return_value = mock_http_client

        tokens = []
        async for token in client.generate_stream("测试深度思考", response_metadata=meta):
            tokens.append(token)

    # Verify reasoning_content was NOT yielded in tokens
    assert "".join(tokens) == "回答正文"
    assert "思考过程文本" not in "".join(tokens)

    # Verify usage & reasoning_tokens captured in metadata
    assert meta["finish_reason"] == "stop"
    assert meta["generation_status"] == "completed"
    assert meta["usage"]["completion_tokens"] == 85
    assert meta["reasoning_tokens"] == 48


@pytest.mark.asyncio
async def test_openai_client_stream_payload_max_tokens_verification():
    """
    Test 5: Verify the exact JSON payload sent to the LLM API contains max_tokens=4096.
    Requirement I.5: Must verify actual HTTP payload max_tokens, not just Python arguments.
    """
    client = OpenAILLMClient(api_key="sk-test-key-mock", base_url="https://api.deepseek.com")

    sse_lines = [
        'data: {"id":"chat-5","choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    mock_resp = MockStreamResponse(sse_lines)
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_resp)

    meta = {}
    with patch("app.llm.openai_client.get_ssrf_safe_async_client") as mock_ssrf:
        mock_ssrf.return_value.__aenter__.return_value = mock_http_client

        async for _ in client.generate_stream("提问", max_tokens=4096, response_metadata=meta):
            pass

        # Inspect the exact call arguments to client.stream
        mock_http_client.stream.assert_called_once()
        call_kwargs = mock_http_client.stream.call_args.kwargs
        json_payload = call_kwargs["json"]

        assert json_payload["max_tokens"] == 4096
        assert json_payload["stream"] is True
        assert json_payload["stream_options"] == {"include_usage": True}
        assert client.last_payload["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_concurrent_stream_metadata_isolation():
    """
    Test 6: Concurrent requests isolation.
    Two concurrent streams must have isolated response_metadata dicts with zero cross-contamination.
    """
    client = TestDoubleLLMClient()

    meta_req1 = {}
    meta_req2 = {}

    client.set_stream_simulation(
        chunks=["请求1回答"],
        finish_reason="stop",
        usage={"prompt_tokens": 50, "completion_tokens": 10},
    )

    async def run_req1():
        tokens = []
        async for t in client.generate_stream("请求1", response_metadata=meta_req1):
            tokens.append(t)
        return "".join(tokens)

    # Simultaneously execute with different metadata
    t1 = asyncio.create_task(run_req1())

    client2 = TestDoubleLLMClient()
    client2.set_stream_simulation(
        chunks=["请求2因长度截断"],
        finish_reason="length",
        usage={"prompt_tokens": 80, "completion_tokens": 2048},
    )

    async def run_req2():
        tokens = []
        async for t in client2.generate_stream("请求2", response_metadata=meta_req2):
            tokens.append(t)
        return "".join(tokens)

    t2 = asyncio.create_task(run_req2())

    res1, res2 = await asyncio.gather(t1, t2)

    assert res1 == "请求1回答"
    assert res2 == "请求2因长度截断"

    assert meta_req1["finish_reason"] == "stop"
    assert meta_req1["is_truncated"] is False
    assert meta_req1["generation_status"] == "completed"

    assert meta_req2["finish_reason"] == "length"
    assert meta_req2["is_truncated"] is True
    assert meta_req2["generation_status"] == "truncated"


async def _create_test_kb_with_chunk(db_session: AsyncSession, name: str) -> KnowledgeBase:
    kb = KnowledgeBase(name=name)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name=f"{name}.md",
        file_type="markdown",
        file_size=100,
        file_path=f"/tmp/{name}.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "测试文档技术架构与Token预算说明内容。"
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
    return kb


@pytest.mark.asyncio
async def test_rag_workflow_dynamic_budget_passed_to_payload(db_session: AsyncSession):
    """
    Test 7: RAG Workflow passes the planned reserved_output_tokens (4096) to generate_stream payload.
    Verifies that the Token Budget Disconnect is completely resolved.
    """
    kb = await _create_test_kb_with_chunk(db_session, "GEN11预算验证知识库")

    test_llm = TestDoubleLLMClient()
    engine = WorkflowRAGEngine(db_session, llm_client=test_llm)

    req = ChatRequest(
        knowledge_base_id=kb.id,
        query="请详细解释Token预算规划",
        mode="workflow",
    )

    events = []
    async for event in engine.execute_stream(req):
        events.append(event)

    # Verify that the LLM client received max_tokens=4096 (reserved_output_tokens)
    assert test_llm.last_max_tokens == settings.MAX_OUTPUT_TOKENS
    assert test_llm.last_max_tokens == 4096
    assert test_llm.last_payload["max_tokens"] == 4096

    # Verify trace event has matching reserved_output_tokens and completion status
    trace_events = [e for e in events if e.get("event") == "trace"]
    assert len(trace_events) == 1
    trace_data = trace_events[0]["data"]
    assert trace_data["reserved_output_tokens"] == 4096
    assert trace_data["generation_status"] == "completed"
    assert trace_data["is_truncated"] is False


@pytest.mark.asyncio
async def test_rag_workflow_stream_truncation_trace_event(db_session: AsyncSession):
    """
    Test 8: RAG Workflow handles finish_reason='length' truncation properly.
    Verifies that is_truncated=True, generation_status='truncated', and partial text is saved to DB.
    """
    kb = await _create_test_kb_with_chunk(db_session, "截断测试知识库")

    test_llm = TestDoubleLLMClient()
    test_llm.set_stream_simulation(
        chunks=["第一章：系统架构概述。\n", "第二章：因Token超出上限导致截断..."],
        finish_reason="length",
        usage={"prompt_tokens": 100, "completion_tokens": 4096},
    )

    engine = WorkflowRAGEngine(db_session, llm_client=test_llm)

    req = ChatRequest(
        knowledge_base_id=kb.id,
        query="请根据测试文档详细说明Token预算超长设计方案",
        mode="workflow",
    )

    events = []
    async for event in engine.execute_stream(req):
        events.append(event)

    trace_events = [e for e in events if e.get("event") == "trace"]
    assert len(trace_events) == 1
    trace_data = trace_events[0]["data"]

    assert trace_data["finish_reason"] == "length"
    assert trace_data["is_truncated"] is True
    assert trace_data["generation_status"] == "truncated"

    # Verify DB persistence of partial answer
    done_events = [e for e in events if e.get("event") == "done"]
    assert len(done_events) == 1
    msg_id = done_events[0]["data"]["message_id"]

    res = await db_session.execute(select(Message).where(Message.id == msg_id))
    saved_msg = res.scalar_one()

    assert saved_msg.content == "第一章：系统架构概述。\n第二章：因Token超出上限导致截断..."
    assert saved_msg.execution_info["is_truncated"] is True
    assert saved_msg.execution_info["finish_reason"] == "length"
    assert saved_msg.execution_info["generation_status"] == "truncated"


@pytest.mark.asyncio
async def test_rag_workflow_stream_interruption_preserves_accumulated_answer(db_session: AsyncSession):
    """
    Test 9: Stream interruption mid-way preserves accumulated text in DB and marks generation_status='interrupted'.
    Requirement II.2: Preserve accumulated text; must not discard on truncation or interruption.
    """
    kb = await _create_test_kb_with_chunk(db_session, "中断测试知识库")

    test_llm = TestDoubleLLMClient()
    test_llm.set_stream_simulation(
        chunks=["已输出第一部分内容。\n", "已输出第二部分内容。\n", "第三部分（未输出）"],
        fail_after_chunk=2,
        error=httpx.RemoteProtocolError("Remote peer abruptly closed socket"),
    )

    engine = WorkflowRAGEngine(db_session, llm_client=test_llm)

    req = ChatRequest(
        knowledge_base_id=kb.id,
        query="长文本测试网络中断",
        mode="workflow",
    )

    events = []
    async for event in engine.execute_stream(req):
        events.append(event)

    token_events = [e for e in events if e.get("event") == "token"]
    assert len(token_events) == 2

    trace_events = [e for e in events if e.get("event") == "trace"]
    assert len(trace_events) == 1
    trace_data = trace_events[0]["data"]

    assert trace_data["generation_status"] == "interrupted"

    # Verify message in DB was saved with the partial text
    done_events = [e for e in events if e.get("event") == "done"]
    assert len(done_events) == 1
    msg_id = done_events[0]["data"]["message_id"]

    res = await db_session.execute(select(Message).where(Message.id == msg_id))
    saved_msg = res.scalar_one()

    assert "已输出第一部分内容。" in saved_msg.content
    assert "已输出第二部分内容。" in saved_msg.content
    assert saved_msg.execution_info["generation_status"] == "interrupted"


@pytest.mark.asyncio
async def test_openai_client_stream_missing_done_marker():
    """
    Test 10: Stream terminates abruptly without [DONE] marker.
    Requirement II: Missing final end marker must not be mistaken for completed.
    """
    client = OpenAILLMClient(api_key="sk-test-key-mock", base_url="https://api.deepseek.com")

    sse_lines = [
        'data: {"id":"chat-10","choices":[{"index":0,"delta":{"content":"未完成的内容"},"finish_reason":null}]}',
    ]

    mock_resp = MockStreamResponse(sse_lines)
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_resp)

    meta = {}
    with patch("app.llm.openai_client.get_ssrf_safe_async_client") as mock_ssrf:
        mock_ssrf.return_value.__aenter__.return_value = mock_http_client

        tokens = []
        async for token in client.generate_stream("提问", response_metadata=meta):
            tokens.append(token)

    assert "".join(tokens) == "未完成的内容"
    assert meta["completed"] is False
    assert meta["generation_status"] == "unknown"


@pytest.mark.asyncio
async def test_rag_workflow_fallback_budget_when_plan_missing(db_session: AsyncSession):
    """
    Test 11: When ContextBudgetPlan has no reserved_output_tokens, fallback to LLM_MAX_TOKENS (2048).
    Requirement I.3: If planned budget is missing, fallback to LLM_MAX_TOKENS.
    """
    kb = await _create_test_kb_with_chunk(db_session, "回退预算知识库")

    test_llm = TestDoubleLLMClient()
    engine = WorkflowRAGEngine(db_session, llm_client=test_llm)

    # Patch allocate_and_assemble to return plan with reserved_output_tokens=None
    original_allocate = engine.budget_manager.allocate_and_assemble

    def mock_allocate(*args, **kwargs):
        plan = original_allocate(*args, **kwargs)
        plan.budget_info.reserved_output_tokens = None
        return plan

    with patch.object(engine.budget_manager, "allocate_and_assemble", side_effect=mock_allocate):
        req = ChatRequest(
            knowledge_base_id=kb.id,
            query="测试回退预算逻辑",
            mode="workflow",
        )
        async for _ in engine.execute_stream(req):
            pass

    # Verify fallback to settings.LLM_MAX_TOKENS (2048)
    assert test_llm.last_max_tokens == settings.LLM_MAX_TOKENS
    assert test_llm.last_max_tokens == 2048
    assert test_llm.last_payload["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_rag_workflow_capped_by_max_output_tokens(db_session: AsyncSession):
    """
    Test 12: When ContextBudgetPlan plans reserved_output_tokens > MAX_OUTPUT_TOKENS, cap strictly.
    Requirement I.2: Must apply MAX_OUTPUT_TOKENS upper bound protection.
    """
    kb = await _create_test_kb_with_chunk(db_session, "上限保护知识库")

    test_llm = TestDoubleLLMClient()
    engine = WorkflowRAGEngine(db_session, llm_client=test_llm)

    # Patch allocate_and_assemble to return plan with excessive reserved_output_tokens (8192)
    original_allocate = engine.budget_manager.allocate_and_assemble

    def mock_allocate(*args, **kwargs):
        plan = original_allocate(*args, **kwargs)
        plan.budget_info.reserved_output_tokens = 8192
        return plan

    with patch.object(engine.budget_manager, "allocate_and_assemble", side_effect=mock_allocate):
        req = ChatRequest(
            knowledge_base_id=kb.id,
            query="测试上限截断保护",
            mode="workflow",
        )
        async for _ in engine.execute_stream(req):
            pass

    # Clamped to settings.MAX_OUTPUT_TOKENS (4096)
    assert test_llm.last_max_tokens == settings.MAX_OUTPUT_TOKENS
    assert test_llm.last_max_tokens == 4096
    assert test_llm.last_payload["max_tokens"] == 4096
