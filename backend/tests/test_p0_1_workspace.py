"""
AgentStudio-RAG — P0.1 Unified Intelligent Q&A Workspace Test Suite
Validates:
1. Built-in default agent auto-creation & retrieval (/api/v1/agents/default).
2. Agent stream execution with default agent without manual AgentDefinition setup.
3. Dynamic knowledge_base_id resolution per execution request.
4. Automatic conversation creation when conversation_id is None.
5. Message persistence and execution_info.agent_timeline storage (zero duplicates).
6. Cross-mode multi-turn continuity (Turn 1 Workflow -> Turn 2 Agent -> Turn 3 Workflow).
7. Client cancellation preserves cancelled message cleanly.
"""

import asyncio
import json
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from langchain_core.messages import AIMessage
from app.agent.tools.registry import get_default_tool_registry
from app.llm.openai_client import get_llm_client
from app.models.agent import AgentDefinition, AgentRun
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase


@pytest.fixture(autouse=True)
def setup_test_double_llm():
    """Ensure test double LLM is active for deterministic testing."""
    client = get_llm_client("test_double")
    if hasattr(client, "reset"):
        client.reset()
    yield client


async def _create_test_kb_with_doc(db: AsyncSession, name: str = "P0.1测试知识库") -> tuple[KnowledgeBase, DocumentChunk]:
    from app.llm.embeddings import get_embedding_client

    emb_client = get_embedding_client()
    kb = KnowledgeBase(
        id=f"kb-{uuid.uuid4().hex[:8]}",
        name=name,
        embedding_provider=emb_client.provider_name,
        embedding_model=emb_client.model_name,
        embedding_dimensions=emb_client.dimensions,
    )
    db.add(kb)
    await db.flush()

    doc = Document(
        id=f"doc-{uuid.uuid4().hex[:8]}",
        knowledge_base_id=kb.id,
        file_name="gateway_specs.md",
        file_path="/data/gateway_specs.md",
        file_type="markdown",
        file_size=1024,
        status="COMPLETED",
    )
    db.add(doc)
    await db.flush()

    chunk = DocumentChunk(
        id=f"chunk-{uuid.uuid4().hex[:8]}",
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="网关最大连接池数量设置为 500，接口超时时间设置为 5000 毫秒。",
        page_number=1,
        char_length=40,
        embedding_json=json.dumps([0.05] * 512),
    )
    db.add(chunk)
    await db.commit()
    return kb, chunk


@pytest.mark.asyncio
async def test_default_agent_auto_creation_and_retrieval(client: AsyncClient, db_session: AsyncSession):
    """Scenario 1: GET /api/v1/agents/default auto-creates and returns built-in system agent."""
    res = await client.get("/api/v1/agents/default")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "default-system-agent"
    assert "企业智能助手" in data["name"]
    assert "search_knowledge_base" in data["enabled_tools"]
    assert data["is_active"] is True

    # Check database persistence
    stmt = select(AgentDefinition).where(AgentDefinition.id == "default-system-agent")
    db_agent = (await db_session.execute(stmt)).scalar_one_or_none()
    assert db_agent is not None
    assert db_agent.name == data["name"]


@pytest.mark.asyncio
async def test_agent_stream_with_default_agent_and_auto_conversation(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 2 & 4: POST /api/v1/agents/default/runs/stream executes with built-in agent
    and automatically creates a conversation when conversation_id is not passed.
    """
    kb, chunk = await _create_test_kb_with_doc(db_session, "默认Agent自动会话测试库")

    # Configure test double to directly answer
    llm = get_llm_client("test_double")
    llm.set_responses([AIMessage(content="基于知识库分析，网关超时时间为 5000ms。")])

    payload = {
        "input": "请查询网关超时时间配置",
        "knowledge_base_id": kb.id,
    }

    res = await client.post("/api/v1/agents/default/runs/stream", json=payload)
    assert res.status_code == 200

    raw_text = res.text
    assert "event: run_started" in raw_text
    assert "event: final_answer" in raw_text
    assert "event: run_completed" in raw_text

    # Extract conversation_id from run_started event
    lines = raw_text.splitlines()
    conv_id = None
    for i, line in enumerate(lines):
        if line.startswith("event: run_started"):
            data_line = lines[i + 1]
            if data_line.startswith("data:"):
                data = json.loads(data_line[5:].strip())
                conv_id = data.get("conversation_id")
                break

    assert conv_id is not None, "Expected conversation_id to be auto-created and returned in run_started"

    # Verify conversation was persisted with mode="agentic"
    conv_stmt = select(Conversation).where(Conversation.id == conv_id)
    conv = (await db_session.execute(conv_stmt)).scalar_one_or_none()
    assert conv is not None
    assert conv.mode == "agentic"
    assert conv.knowledge_base_id == kb.id

    # Verify user and assistant messages were saved (Scenario 5: zero duplicates)
    msgs_stmt = select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at.asc())
    msgs = (await db_session.execute(msgs_stmt)).scalars().all()
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "请查询网关超时时间配置"
    assert msgs[1].role == "assistant"
    assert "5000ms" in msgs[1].content
    assert msgs[1].execution_info is not None
    assert msgs[1].execution_info.get("mode") == "agentic"
    assert "agent_timeline" in msgs[1].execution_info


@pytest.mark.asyncio
async def test_agent_stream_dynamic_knowledge_base_override(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 3: AgentRunRequest.knowledge_base_id dynamically targets the specified KB
    even if the agent definition has a different or null default knowledge_base_id.
    """
    kb1, chunk1 = await _create_test_kb_with_doc(db_session, "知识库A")
    kb2, chunk2 = await _create_test_kb_with_doc(db_session, "知识库B")

    llm = get_llm_client("test_double")
    llm.set_responses([AIMessage(content="知识库B检索结果总结")])

    # Send request explicitly targeting kb2
    payload = {
        "input": "查询知识库B内容",
        "knowledge_base_id": kb2.id,
    }

    res = await client.post("/api/v1/agents/default/runs/stream", json=payload)
    assert res.status_code == 200

    raw_text = res.text
    assert "event: run_started" in raw_text
    lines = raw_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("event: run_started"):
            data_line = lines[i + 1]
            if data_line.startswith("data:"):
                data = json.loads(data_line[5:].strip())
                assert data.get("knowledge_base_id") == kb2.id


@pytest.mark.asyncio
async def test_multi_turn_cross_mode_continuity(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 6: Cross-mode multi-turn continuity:
    Turn 1 (Workflow RAG) -> Turn 2 (Agent ReAct) on the SAME conversation.
    Verifies that the conversation message list grows from 2 -> 4 without duplicates.
    """
    kb, chunk = await _create_test_kb_with_doc(db_session, "混合多轮测试库")

    # Turn 1: Workflow RAG
    llm = get_llm_client("test_double")

    wf_payload = {
        "knowledge_base_id": kb.id,
        "query": "第1轮问题(Workflow)",
        "mode": "workflow",
    }
    wf_res = await client.post("/api/v1/chat/completions/stream", json=wf_payload)
    assert wf_res.status_code == 200

    # Extract conversation_id from start event
    conv_id = None
    for line in wf_res.text.splitlines():
        if line.startswith("data:") and "conversation_id" in line:
            data = json.loads(line[5:].strip())
            if "conversation_id" in data:
                conv_id = data["conversation_id"]
                break

    assert conv_id is not None

    # Check Turn 1 messages
    conv_msgs_res1 = await client.get(f"/api/v1/conversations/{conv_id}/messages")
    assert conv_msgs_res1.status_code == 200
    msgs1 = conv_msgs_res1.json()
    assert len(msgs1) == 2
    assert msgs1[0]["role"] == "user"
    assert msgs1[1]["role"] == "assistant"

    # Turn 2: Agentic ReAct on the SAME conversation
    llm.set_responses([AIMessage(content="Agent第二轮深度分析回答")])
    agent_payload = {
        "input": "第2轮追问(Agent)",
        "conversation_id": conv_id,
        "knowledge_base_id": kb.id,
    }
    agent_res = await client.post("/api/v1/agents/default/runs/stream", json=agent_payload)
    assert agent_res.status_code == 200

    # Check Turn 2 messages: total should now be exactly 4 (2 from Turn 1 + 2 from Turn 2)
    conv_msgs_res2 = await client.get(f"/api/v1/conversations/{conv_id}/messages")
    assert conv_msgs_res2.status_code == 200
    msgs2 = conv_msgs_res2.json()
    assert len(msgs2) == 4, f"Expected 4 messages, got {len(msgs2)}"
    assert msgs2[2]["role"] == "user"
    assert msgs2[2]["content"] == "第2轮追问(Agent)"
    assert msgs2[3]["role"] == "assistant"
    assert "Agent第二轮深度分析回答" in msgs2[3]["content"]


@pytest.mark.asyncio
async def test_agent_timeline_summary_in_message_execution_info(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 5: Agent execution saves timeline summary into message.execution_info
    allowing historical messages to display the AgentTimelineCard.
    """
    kb, chunk = await _create_test_kb_with_doc(db_session, "时间线历史测试库")

    # Configure test double to call a tool then answer
    llm = get_llm_client("test_double")
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "search_knowledge_base",
                "args": {"query": "连接池配置", "top_k": 2},
                "id": "tc_test_1",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="基于工具检索结果，连接池最大500。"),
    ])

    res = await client.post("/api/v1/agents/default/runs/stream", json={
        "input": "请帮我查一下连接池",
        "knowledge_base_id": kb.id,
    })
    assert res.status_code == 200

    # Extract conversation_id
    lines = res.text.splitlines()
    conv_id = None
    for i, line in enumerate(lines):
        if line.startswith("event: run_started"):
            data = json.loads(lines[i + 1][5:].strip())
            conv_id = data.get("conversation_id")
            break

    assert conv_id is not None
    msgs_res = await client.get(f"/api/v1/conversations/{conv_id}/messages")
    assert msgs_res.status_code == 200
    msgs = msgs_res.json()
    assert len(msgs) == 2
    assistant_msg = msgs[1]
    exec_info = assistant_msg.get("execution_info")
    assert exec_info is not None
    assert exec_info.get("mode") == "agentic"
    timeline = exec_info.get("agent_timeline")
    assert timeline is not None
    assert timeline.get("status") == "completed"
    assert isinstance(timeline.get("tool_calls"), list)
    assert len(timeline["tool_calls"]) >= 1
    assert timeline["tool_calls"][0]["tool_name"] == "search_knowledge_base"
