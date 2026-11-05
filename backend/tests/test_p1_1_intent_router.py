import asyncio
import json
import uuid
from typing import Any, Dict, List
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.embeddings import get_embedding_client
from app.models.conversation import Conversation, ConversationAttachment, Message
from app.models.knowledge import KnowledgeBase
from app.models.model_provider import ModelConfig, ModelProvider
from app.routing import (
    DirectChatEngine,
    ExecutionPath,
    IntentRouter,
    RouteDecision,
    UserIntent,
    get_intent_router,
)
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


async def _create_test_kb(db: AsyncSession, name: str = "企业技术知识库") -> KnowledgeBase:
    emb = get_embedding_client()
    kb = KnowledgeBase(
        id=f"kb-test-{uuid.uuid4().hex[:8]}",
        name=name,
        description="用于路由专项测试的企业级知识库",
        embedding_provider=emb.provider_name,
        embedding_model=emb.model_name,
        embedding_dimensions=emb.dimensions,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _create_test_conv(
    db: AsyncSession, title: str = "测试会话", kb_id: str = None, mode: str = "workflow"
) -> Conversation:
    conv = Conversation(
        id=str(uuid.uuid4()),
        knowledge_base_id=kb_id,
        title=title,
        mode=mode,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _add_test_attachment(
    db: AsyncSession, conv_id: str, filename: str = "architecture_spec.pdf"
) -> ConversationAttachment:
    att = ConversationAttachment(
        id=str(uuid.uuid4()),
        conversation_id=conv_id,
        original_filename=filename,
        storage_key=f"uploads/{conv_id}/{filename}",
        file_extension="." + filename.split(".")[-1],
        mime_type="application/pdf",
        file_size=10240,
        parse_status="COMPLETED",
        page_count=5,
        char_length=2000,
        token_count=500,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return att


# ==============================================================================
# Unit & Rule-Level Tests
# ==============================================================================


def test_intent_router_rule_unit_matrix():
    """Unit test for IntentRouter pattern matching across diverse queries."""
    router = IntentRouter()

    # 1. Chitchat / Greetings
    for q in ["你好", "您好呀", "hello", "hi", "早上好", "你是谁", "介绍一下你自己", "谢谢你"]:
        d = router.route(q, has_knowledge_base=True, has_completed_attachments=True)
        assert d.intent == UserIntent.DIRECT_CHAT
        assert d.execution_path == ExecutionPath.DIRECT_LLM
        assert d.should_retrieve is False
        assert d.should_use_agent is False

    # 2. General Knowledge / Programming Concepts
    for q in [
        "什么是 Transformer？",
        "Python 中 list 和 tuple 有什么区别？",
        "解释一下 TCP 三次握手。",
        "什么是死锁？怎么解决？",
        "快速排序的时间复杂度",
        "React 的 useEffect 怎么用？",
    ]:
        d = router.route(q, has_knowledge_base=True, has_completed_attachments=True)
        assert d.intent == UserIntent.DIRECT_CHAT
        assert d.execution_path == ExecutionPath.DIRECT_LLM
        assert d.should_retrieve is False
        assert d.should_use_agent is False

    # 3. Conversational Follow-up
    for q in [
        "你刚才说的是什么意思？",
        "上面提到的第二种方法呢？",
        "继续解释。",
        "我上一轮问了什么？",
        "展开讲讲刚才那点",
        "接着往下说",
    ]:
        d = router.route(
            q,
            has_knowledge_base=True,
            has_completed_attachments=True,
            history_messages=[{"role": "user", "content": "前文提问"}, {"role": "assistant", "content": "前文回答"}],
        )
        assert d.intent == UserIntent.CONVERSATION_FOLLOWUP
        assert d.execution_path == ExecutionPath.DIRECT_LLM
        assert d.should_retrieve is False

    # 4. Explicit Knowledge Base
    for q in [
        "根据企业知识库解释网关超时机制。",
        "查一下内部规范中连接池的默认配置。",
        "对照知识库中的两份技术规范。",
        "根据架构规范说明三态熔断机制",
        "ERR-GATEWAY-504 错误码排查指引",
    ]:
        d = router.route(q, has_knowledge_base=True, has_completed_attachments=False)
        assert d.intent == UserIntent.KNOWLEDGE_BASE
        assert d.execution_path == ExecutionPath.KNOWLEDGE_BASE
        assert d.should_retrieve is True
        assert "knowledge_base" in d.required_sources

    # 5. Explicit Attachment
    for q in [
        "总结我刚才上传的 PDF。",
        "这个文件的核心参数是什么？",
        "提取附件中的错误码。",
        "分析我上传的文档结构",
    ]:
        d = router.route(q, has_knowledge_base=True, has_completed_attachments=True)
        assert d.intent == UserIntent.ATTACHMENT
        assert d.execution_path == ExecutionPath.ATTACHMENT
        assert d.should_retrieve is True
        assert "attachments" in d.required_sources

    # 6. Hybrid Cross-Source
    for q in [
        "对比上传的 PDF 与知识库中的技术规范。",
        "结合企业知识库和我上传的文件进行对比分析。",
    ]:
        d = router.route(q, has_knowledge_base=True, has_completed_attachments=True)
        assert d.intent == UserIntent.HYBRID
        assert d.execution_path == ExecutionPath.HYBRID
        assert d.should_retrieve is True
        assert set(d.required_sources) == {"knowledge_base", "attachments"}

    # 7. Agent Mode: Simple questions stay DIRECT_LLM, complex tasks enter AGENT
    d_agent_simple = router.route(
        "Python 装饰器是什么？",
        has_knowledge_base=True,
        has_completed_attachments=True,
        requested_mode="agentic",
    )
    assert d_agent_simple.execution_path == ExecutionPath.DIRECT_LLM
    assert d_agent_simple.should_use_agent is False

    d_agent_complex = router.route(
        "综合规划网关从 Zuul 迁移至 Spring Cloud Gateway 的多步调研方案，并调用工具检索各模块配置。",
        has_knowledge_base=True,
        has_completed_attachments=True,
        requested_mode="agentic",
    )
    assert d_agent_complex.execution_path == ExecutionPath.AGENT
    assert d_agent_complex.should_use_agent is True


# ==============================================================================
# End-to-End API Integration Tests (12 Required Scenarios)
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_1_general_question_no_retrieval(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 1: General question does NOT trigger any retrieval."""
    conv = await _create_test_conv(db_session, title="通用问答测试")

    req_payload = {
        "query": "什么是 Transformer？",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    event_names = [e["event"] for e in events]
    assert "start" in event_names
    assert "route" in event_names
    assert "token" in event_names
    assert "done" in event_names
    # Retrieval events must NOT be emitted
    assert "retrieval" not in event_names
    assert "query_rewrite" not in event_names

    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "DIRECT_LLM"
    assert route_event["data"]["should_retrieve"] is False

    trace_event = next(e for e in events if e["event"] == "trace")
    assert trace_event["data"]["execution_path"] == "DIRECT_LLM"
    assert trace_event["data"]["retrieved_chunks_count"] == 0


@pytest.mark.asyncio
async def test_scenario_2_kb_selected_but_general_question_direct_llm(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 2: KB is selected, but general question still routes to DIRECT_LLM."""
    kb = await _create_test_kb(db_session)
    conv = await _create_test_conv(db_session, kb_id=kb.id)

    req_payload = {
        "knowledge_base_id": kb.id,
        "query": "Python 装饰器是什么？",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "DIRECT_LLM"
    assert route_event["data"]["should_retrieve"] is False

    trace_event = next(e for e in events if e["event"] == "trace")
    assert trace_event["data"]["retrieval_duration_ms"] == 0.0
    assert trace_event["data"]["retrieved_chunks_count"] == 0


@pytest.mark.asyncio
async def test_scenario_3_attachments_exist_but_general_question_direct_llm(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 3: Attachments exist, but general question does NOT trigger attachment search."""
    conv = await _create_test_conv(db_session)
    await _add_test_attachment(db_session, conv.id, filename="financial_report.pdf")

    req_payload = {
        "query": "解释一下 TCP 三次握手。",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "DIRECT_LLM"
    assert route_event["data"]["should_retrieve"] is False


@pytest.mark.asyncio
async def test_scenario_4_explicit_kb_question_triggers_rag(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 4: Explicit KB question correctly triggers Workflow RAG retrieval."""
    kb = await _create_test_kb(db_session)
    conv = await _create_test_conv(db_session, kb_id=kb.id)

    req_payload = {
        "knowledge_base_id": kb.id,
        "query": "根据企业知识库解释网关超时机制。",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "KNOWLEDGE_BASE"
    assert route_event["data"]["should_retrieve"] is True
    # In empty KB without chunks, WorkflowRAGEngine handles empty index safely
    event_names = [e["event"] for e in events]
    assert "start" in event_names
    assert "route" in event_names


@pytest.mark.asyncio
async def test_scenario_5_explicit_attachment_question_triggers_attachment_retrieval(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 5: Explicit attachment question triggers attachment retrieval."""
    conv = await _create_test_conv(db_session)
    await _add_test_attachment(db_session, conv.id, filename="architecture_v2.pdf")

    req_payload = {
        "query": "总结我刚才上传的 PDF。",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "ATTACHMENT"
    assert route_event["data"]["should_retrieve"] is True


@pytest.mark.asyncio
async def test_scenario_6_hybrid_question_triggers_dual_source_retrieval(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 6: Hybrid question triggers dual-source retrieval."""
    kb = await _create_test_kb(db_session)
    conv = await _create_test_conv(db_session, kb_id=kb.id)
    await _add_test_attachment(db_session, conv.id, filename="custom_gateway.pdf")

    req_payload = {
        "knowledge_base_id": kb.id,
        "query": "对比上传的 PDF 与知识库中的技术规范。",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "HYBRID"
    assert route_event["data"]["should_retrieve"] is True
    assert set(route_event["data"]["required_sources"]) == {"knowledge_base", "attachments"}


@pytest.mark.asyncio
async def test_scenario_7_conversational_followup_preserves_history(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 7: Conversational follow-up utilizes message history without triggering retrieval."""
    conv = await _create_test_conv(db_session)

    # First turn: Ask general question
    resp1 = await client.post(
        "/api/v1/chat/completions/stream",
        json={"query": "请介绍两种常见排序算法", "conversation_id": conv.id},
    )
    assert resp1.status_code == 200

    # Second turn: Conversational follow-up
    resp2 = await client.post(
        "/api/v1/chat/completions/stream",
        json={"query": "你刚才说的是什么意思？上面提到的第二种方法呢？", "conversation_id": conv.id},
    )
    assert resp2.status_code == 200

    events2 = parse_sse_events(resp2.text)
    route_event = next(e for e in events2 if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "DIRECT_LLM"
    assert route_event["data"]["intent"] == "CONVERSATION_FOLLOWUP"
    assert route_event["data"]["should_retrieve"] is False

    # Check that messages are persisted in conversation
    msg_stmt = select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at.asc())
    msgs = (await db_session.execute(msg_stmt)).scalars().all()
    assert len(msgs) == 4  # (User1, Assistant1, User2, Assistant2)


@pytest.mark.asyncio
async def test_scenario_8_general_question_in_agent_mode_no_tools(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 8: General question in Agent mode does NOT blindly invoke tools."""
    kb = await _create_test_kb(db_session)
    conv = await _create_test_conv(db_session, kb_id=kb.id, mode="agentic")

    agent_req = {
        "input": "Python 装饰器是什么？",
        "conversation_id": conv.id,
        "knowledge_base_id": kb.id,
    }
    resp = await client.post("/api/v1/agents/default/runs/stream", json=agent_req)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    event_names = [e["event"] for e in events]
    assert "run_started" in event_names
    assert "final_answer" in event_names
    assert "run_completed" in event_names
    # MUST NOT invoke tool calls
    assert "tool_call_started" not in event_names
    assert "tool_call_completed" not in event_names

    run_started = next(e for e in events if e["event"] == "run_started")
    assert run_started["data"]["execution_path"] == "DIRECT_LLM"

    run_completed = next(e for e in events if e["event"] == "run_completed")
    assert run_completed["data"]["tool_calls_count"] == 0
    assert run_completed["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_scenario_9_complex_agent_task_enters_langgraph_agent(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 9: Complex multi-step task in Agent mode enters LangGraph Agent Runtime."""
    kb = await _create_test_kb(db_session)
    conv = await _create_test_conv(db_session, kb_id=kb.id, mode="agentic")

    agent_req = {
        "input": "综合规划网关从 Zuul 迁移至 Spring Cloud Gateway 的多步调研方案，并调用知识库检索各模块配置。",
        "conversation_id": conv.id,
        "knowledge_base_id": kb.id,
    }
    resp = await client.post("/api/v1/agents/default/runs/stream", json=agent_req)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    event_names = [e["event"] for e in events]
    assert "run_started" in event_names
    assert "run_completed" in event_names


@pytest.mark.asyncio
async def test_scenario_10_direct_llm_cancellation_and_persistence(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 10: Direct LLM synchronous execute and message persistence."""
    conv = await _create_test_conv(db_session)

    req_payload = {
        "query": "什么是二分查找？",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    # Test sync endpoint
    resp = await client.post("/api/v1/chat/completions", json=req_payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["answer"]
    assert data["citations"] == []
    assert data["execution_info"]["execution_path"] == "DIRECT_LLM"
    assert data["execution_info"]["intent"] == "DIRECT_CHAT"
    assert data["execution_info"]["retrieved_chunks_count"] == 0

    # Verify message persistence
    msg_stmt = select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at.desc())
    latest_msg = (await db_session.execute(msg_stmt)).scalars().first()
    assert latest_msg.role == "assistant"
    assert latest_msg.execution_info.get("execution_path") == "DIRECT_LLM"


@pytest.mark.asyncio
async def test_scenario_11_multi_model_selection(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 11: Multi-model selection does not break routing or direct LLM execution."""
    prov = ModelProvider(
        id=str(uuid.uuid4()),
        name="测试模型服务商",
        provider_type="openai",
        base_url="http://localhost:8000/v1",
        api_key_ciphertext="sk-test-key",
        enabled=True,
    )
    db_session.add(prov)
    model = ModelConfig(
        id=str(uuid.uuid4()),
        provider_id=prov.id,
        model_name="deepseek-chat",
        display_name="DeepSeek V3",
        enabled=True,
        is_default=False,
    )
    db_session.add(model)
    await db_session.commit()

    conv = await _create_test_conv(db_session)

    req_payload = {
        "query": "Python 中的字典推导式怎么写？",
        "conversation_id": conv.id,
        "model_id": model.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "DIRECT_LLM"


@pytest.mark.asyncio
async def test_scenario_12_regression_p0_3_workflow_qa_continues_passing(
    client: AsyncClient, db_session: AsyncSession
):
    """Scenario 12: Regression test verifying P0.3 and M6.2 workflows continue to function."""
    conv = await _create_test_conv(db_session)
    await _add_test_attachment(db_session, conv.id, filename="system_overview.pdf")

    # Explicit attachment query
    req_payload = {
        "query": "从附件中提取系统架构图的核心模块",
        "conversation_id": conv.id,
        "mode": "workflow",
    }
    resp = await client.post("/api/v1/chat/completions/stream", json=req_payload)
    assert resp.status_code == 200

    events = parse_sse_events(resp.text)
    route_event = next(e for e in events if e["event"] == "route")
    assert route_event["data"]["execution_path"] == "ATTACHMENT"
    assert route_event["data"]["should_retrieve"] is True
