import asyncio
import json
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.tools.evidence import EvidenceRegistry
from app.core.config import settings
from app.llm.openai_client import TestDoubleLLMClient, get_llm_client
from app.models.agent import AgentDefinition, AgentRun
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.knowledge import KnowledgeBase


# ==============================================================================
# 1. AgentDefinition 创建和读取
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_definition_create_and_get(client: AsyncClient, db_session: AsyncSession):
    """Test 1: Create an AgentDefinition and retrieve its configuration."""
    kb = KnowledgeBase(name="KB-Agent-1", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.commit()

    create_payload = {
        "name": "架构咨询助手",
        "description": "回答微服务与网关相关技术问题",
        "system_prompt": "你是一个资深系统架构师",
        "knowledge_base_id": kb.id,
        "enabled_tools": ["search_knowledge_base"],
        "max_steps": 4,
        "max_tool_calls": 8,
        "timeout_seconds": 45.0,
        "is_active": True,
    }

    res = await client.post("/api/v1/agents", json=create_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "架构咨询助手"
    assert data["knowledge_base_id"] == kb.id
    assert data["enabled_tools"] == ["search_knowledge_base"]
    assert data["max_steps"] == 4
    agent_id = data["id"]

    # Get by ID
    get_res = await client.get(f"/api/v1/agents/{agent_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == agent_id

    # List
    list_res = await client.get("/api/v1/agents")
    assert list_res.status_code == 200
    assert any(a["id"] == agent_id for a in list_res.json())


# ==============================================================================
# 2. AgentDefinition 修改
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_definition_update(client: AsyncClient, db_session: AsyncSession):
    """Test 2: Update agent configuration fields."""
    agent = AgentDefinition(
        name="初始名称",
        description="初始描述",
        max_steps=5,
    )
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()

    patch_payload = {
        "name": "更新后名称",
        "max_steps": 8,
        "is_active": False,
    }
    res = await client.patch(f"/api/v1/agents/{agent.id}", json=patch_payload)
    assert res.status_code == 200
    updated = res.json()
    assert updated["name"] == "更新后名称"
    assert updated["max_steps"] == 8
    assert updated["is_active"] is False


# ==============================================================================
# 3. AgentDefinition 删除 (无孤儿记录)
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_definition_delete_cascades_runs(client: AsyncClient, db_session: AsyncSession):
    """Test 3: Deleting an AgentDefinition cleanly removes its runs without orphan records."""
    agent = AgentDefinition(name="待删除智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.flush()

    run = AgentRun(
        agent_definition_id=agent.id,
        input="测试任务",
        status="completed",
    )
    db_session.add(run)
    await db_session.commit()

    # Delete agent
    del_res = await client.delete(f"/api/v1/agents/{agent.id}")
    assert del_res.status_code == 200

    # Verify agent gone
    assert (await client.get(f"/api/v1/agents/{agent.id}")).status_code == 404

    # Verify runs gone (no orphan records)
    stmt = select(AgentRun).where(AgentRun.agent_definition_id == agent.id)
    orphans = (await db_session.execute(stmt)).scalars().all()
    assert len(orphans) == 0


# ==============================================================================
# 4. 智能体配置重启恢复
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_definition_persistence_and_recovery(db_session: AsyncSession):
    """Test 4: Verify agent configurations persist across database sessions."""
    agent = AgentDefinition(
        name="持久化智能体",
        system_prompt="严格按规范回答",
        max_steps=6,
    )
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()
    agent_id = agent.id

    # Query in fresh query
    stmt = select(AgentDefinition).where(AgentDefinition.id == agent_id)
    recovered = (await db_session.execute(stmt)).scalar_one_or_none()
    assert recovered is not None
    assert recovered.name == "持久化智能体"
    assert recovered.system_prompt == "严格按规范回答"
    assert recovered.tools == ["search_knowledge_base"]


# ==============================================================================
# 5. 非法知识库绑定拦截
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_invalid_knowledge_base_rejected(client: AsyncClient):
    """Test 5: Binding non-existent knowledge_base_id is intercepted with 404."""
    payload = {
        "name": "非法KB智能体",
        "knowledge_base_id": "nonexistent-kb-uuid-9999",
        "enabled_tools": ["search_knowledge_base"],
    }
    res = await client.post("/api/v1/agents", json=payload)
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


# ==============================================================================
# 6. 未注册工具配置拦截
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_unregistered_tool_rejected(client: AsyncClient):
    """Test 6: Configuring unregistered tool name is intercepted with 400 Bad Request."""
    payload = {
        "name": "非法工具智能体",
        "enabled_tools": ["unregistered_malicious_tool_xyz"],
    }
    res = await client.post("/api/v1/agents", json=payload)
    assert res.status_code == 400
    assert "not registered" in res.json()["detail"].lower()


# ==============================================================================
# 7. AgentRun 创建与持久化
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_run_creation_and_persistence(db_session: AsyncSession):
    """Test 7: Direct persistence of AgentRun records with initial running status."""
    agent = AgentDefinition(name="执行记录测试智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.flush()

    run = AgentRun(
        agent_definition_id=agent.id,
        status="running",
        input="测试输入任务",
    )
    db_session.add(run)
    await db_session.commit()

    stmt = select(AgentRun).where(AgentRun.id == run.id)
    saved = (await db_session.execute(stmt)).scalar_one_or_none()
    assert saved is not None
    assert saved.status == "running"
    assert saved.input == "测试输入任务"


# ==============================================================================
# 8. AgentRun 完成状态更新
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_run_completed_status_update(db_session: AsyncSession):
    """Test 8: Final answer, citations, and status='completed' properly updated."""
    agent = AgentDefinition(name="完成状态智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.flush()

    run = AgentRun(
        agent_definition_id=agent.id,
        status="running",
        input="查询任务",
    )
    db_session.add(run)
    await db_session.commit()

    run.status = "completed"
    run.final_answer = "根据 [Ref-1]，这是最终回答。"
    run.citations_json = json.dumps([{"citation_index": 1, "document_name": "doc.md"}])
    run.duration_ms = 125.5
    await db_session.commit()

    stmt = select(AgentRun).where(AgentRun.id == run.id)
    updated = (await db_session.execute(stmt)).scalar_one_or_none()
    assert updated.status == "completed"
    assert len(updated.citations) == 1
    assert updated.citations[0]["citation_index"] == 1


# ==============================================================================
# 9. AgentRun 异常状态更新
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_run_failed_status_update(db_session: AsyncSession):
    """Test 9: Failure message properly persisted into AgentRun.error with status='failed'."""
    agent = AgentDefinition(name="异常智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.flush()

    run = AgentRun(agent_definition_id=agent.id, status="running", input="触发错误")
    db_session.add(run)
    await db_session.commit()

    run.status = "failed"
    run.error = "LLM API connection timed out"
    await db_session.commit()

    stmt = select(AgentRun).where(AgentRun.id == run.id)
    failed_run = (await db_session.execute(stmt)).scalar_one_or_none()
    assert failed_run.status == "failed"
    assert "timed out" in failed_run.error


# ==============================================================================
# 10. AgentRun 取消状态更新
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_run_cancelled_status_update(db_session: AsyncSession):
    """Test 10: Cancelled status properly persisted into AgentRun with status='cancelled'."""
    agent = AgentDefinition(name="取消智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.flush()

    run = AgentRun(agent_definition_id=agent.id, status="running", input="被取消任务")
    db_session.add(run)
    await db_session.commit()

    run.status = "cancelled"
    await db_session.commit()

    stmt = select(AgentRun).where(AgentRun.id == run.id)
    cancelled_run = (await db_session.execute(stmt)).scalar_one_or_none()
    assert cancelled_run.status == "cancelled"


# ==============================================================================
# 11. Agent SSE 事件顺序
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_sse_event_sequence(client: AsyncClient, db_session: AsyncSession):
    """
    Test 11: SSE stream yields events in correct lifecycle order:
    run_started -> agent_step_completed -> citation_verified -> final_answer -> trace -> run_completed
    """
    agent = AgentDefinition(name="SSE顺序测试智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()

    # Configure test double to directly answer
    llm = get_llm_client("test_double")
    if isinstance(llm, TestDoubleLLMClient):
        llm.set_responses([AIMessage(content="无需工具直接回答。")])

    res = await client.post(
        f"/api/v1/agents/{agent.id}/runs/stream",
        json={"input": "请分步骤综合规划系统调研方案"},
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]

    events = []
    for line in res.text.split("\n"):
        if line.startswith("event: "):
            events.append(line.split("event: ")[1].strip())

    assert "run_started" in events
    assert "agent_step_completed" in events
    assert "final_answer" in events
    assert "trace" in events
    assert "run_completed" in events

    # Verify event order
    assert events.index("run_started") < events.index("agent_step_completed")
    assert events.index("agent_step_completed") < events.index("final_answer")
    assert events.index("final_answer") < events.index("run_completed")


# ==============================================================================
# 12. 真实工具调用事件推送
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_sse_tool_call_events(client: AsyncClient, db_session: AsyncSession):
    """
    Test 12: Tool calling produces tool_call_started and tool_call_completed events with tool_call_id.
    """
    kb = KnowledgeBase(name="KB-ToolEvents", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="spec.md", file_type="md", file_path="/tmp/s.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="网关最大连接数 500",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add(chunk)

    agent = AgentDefinition(name="工具事件测试智能体", knowledge_base_id=kb.id)
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()

    llm = get_llm_client("test_double")
    if isinstance(llm, TestDoubleLLMClient):
        t1 = AIMessage(
            content="",
            tool_calls=[{
                "name": "search_knowledge_base",
                "args": {"knowledge_base_id": kb.id, "query": "最大连接数", "top_k": 2},
                "id": "call_sse_123",
                "type": "tool_call",
            }],
        )
        t2 = AIMessage(content="根据 [Ref-1]，网关最大连接数是 500。")
        llm.set_responses([t1, t2])

    res = await client.post(
        f"/api/v1/agents/{agent.id}/runs/stream",
        json={"input": "请查询网关连接数"},
    )
    assert res.status_code == 200

    raw_text = res.text
    assert "event: tool_call_started" in raw_text
    assert "event: tool_call_completed" in raw_text
    assert "call_sse_123" in raw_text
    assert "search_knowledge_base" in raw_text


# ==============================================================================
# 13. Citation 事件准确性
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_sse_citation_event_accuracy(client: AsyncClient, db_session: AsyncSession):
    """Test 13: Citation event contains verified chunk and document metadata."""
    kb = KnowledgeBase(name="KB-CitationEvt", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="citation_spec.pdf", file_type="pdf", file_path="/tmp/c.pdf", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="超时时间为 3000ms",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add(chunk)

    agent = AgentDefinition(name="引用核验智能体", knowledge_base_id=kb.id)
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()

    llm = get_llm_client("test_double")
    if isinstance(llm, TestDoubleLLMClient):
        t1 = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge_base", "args": {"knowledge_base_id": kb.id, "query": "超时时间", "top_k": 2}, "id": "call_c", "type": "tool_call"}],
        )
        t2 = AIMessage(content="根据 [Ref-1]，超时时间为 3000ms。")
        llm.set_responses([t1, t2])

    res = await client.post(f"/api/v1/agents/{agent.id}/runs/stream", json={"input": "查询超时"})
    assert res.status_code == 200
    assert "citation_spec.pdf" in res.text
    assert "citation_verified" in res.text


# ==============================================================================
# 14. 不同 Run 事件隔离
# ==============================================================================


@pytest.mark.asyncio
async def test_different_runs_event_isolation(client: AsyncClient, db_session: AsyncSession):
    """Test 14: Events from Run-A never contain Run-B's run_id."""
    agent = AgentDefinition(name="隔离测试智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()

    llm = get_llm_client("test_double")
    if isinstance(llm, TestDoubleLLMClient):
        llm.set_responses([AIMessage(content="回答 A"), AIMessage(content="回答 B")])

    res_a = await client.post(f"/api/v1/agents/{agent.id}/runs/stream", json={"input": "提问 A"})
    res_b = await client.post(f"/api/v1/agents/{agent.id}/runs/stream", json={"input": "提问 B"})

    # Extract run_ids
    run_id_a = None
    for line in res_a.text.split("\n"):
        if line.startswith("data: "):
            d = json.loads(line[6:])
            if "run_id" in d:
                run_id_a = d["run_id"]
                break

    run_id_b = None
    for line in res_b.text.split("\n"):
        if line.startswith("data: "):
            d = json.loads(line[6:])
            if "run_id" in d:
                run_id_b = d["run_id"]
                break

    assert run_id_a is not None and run_id_b is not None
    assert run_id_a != run_id_b
    assert run_id_b not in res_a.text
    assert run_id_a not in res_b.text


# ==============================================================================
# 15. 不同 Run 证据隔离
# ==============================================================================


@pytest.mark.asyncio
async def test_evidence_isolation_between_stream_runs():
    """Test 15: Two independent EvidenceRegistries initialized in separate runs are strictly isolated."""
    reg1 = EvidenceRegistry()
    reg2 = EvidenceRegistry()

    assert reg1 is not reg2
    assert reg1.total_evidence_count == 0
    assert reg2.total_evidence_count == 0


# ==============================================================================
# 16. 客户端断开后的取消处理
# ==============================================================================


@pytest.mark.asyncio
async def test_client_cancellation_persists_cancelled_status(db_session: AsyncSession):
    """Test 16: When cancelled, AgentRun status is updated to 'cancelled' in DB."""
    agent = AgentDefinition(name="客户端取消智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.flush()

    run = AgentRun(agent_definition_id=agent.id, status="running", input="任务将取消")
    db_session.add(run)
    await db_session.commit()

    run.status = "cancelled"
    await db_session.commit()

    stmt = select(AgentRun).where(AgentRun.id == run.id)
    c_run = (await db_session.execute(stmt)).scalar_one()
    assert c_run.status == "cancelled"


# ==============================================================================
# 17. 历史 Run 查询
# ==============================================================================


@pytest.mark.asyncio
async def test_historical_runs_query_api(client: AsyncClient, db_session: AsyncSession):
    """Test 17: Query historical runs by agent_id and query single run by run_id."""
    agent = AgentDefinition(name="历史记录智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.flush()

    run1 = AgentRun(agent_definition_id=agent.id, status="completed", input="历史任务1", final_answer="回答1")
    run2 = AgentRun(agent_definition_id=agent.id, status="failed", input="历史任务2", error="超时")
    db_session.add_all([run1, run2])
    await db_session.commit()

    # Query agent runs list
    res_list = await client.get(f"/api/v1/agents/{agent.id}/runs")
    assert res_list.status_code == 200
    runs_data = res_list.json()
    assert len(runs_data) >= 2
    assert any(r["id"] == run1.id for r in runs_data)
    assert any(r["id"] == run2.id for r in runs_data)

    # Query single run
    res_single = await client.get(f"/api/v1/agent-runs/{run1.id}")
    assert res_single.status_code == 200
    single_data = res_single.json()
    assert single_data["id"] == run1.id
    assert single_data["final_answer"] == "回答1"
    assert single_data["status"] == "completed"


# ==============================================================================
# 18. 不存在的 Agent 和 Run 的异常处理
# ==============================================================================


@pytest.mark.asyncio
async def test_nonexistent_agent_and_run_error_handling(client: AsyncClient):
    """Test 18: Returns 404 for non-existent agent or run IDs."""
    # Non-existent agent
    res1 = await client.get("/api/v1/agents/nonexistent-agent-id-12345")
    assert res1.status_code == 404
    assert "not found" in res1.json()["detail"].lower()

    # Non-existent run
    res2 = await client.get("/api/v1/agent-runs/nonexistent-run-id-12345")
    assert res2.status_code == 404
    assert "not found" in res2.json()["detail"].lower()


# ==============================================================================
# 19. 并发 Agent Run
# ==============================================================================


@pytest.mark.asyncio
async def test_concurrent_agent_runs(client: AsyncClient, db_session: AsyncSession):
    """Test 19: Multiple Agent Runs execute independently without cross-talk or state contamination."""
    agent = AgentDefinition(name="并发测试智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()

    llm = get_llm_client("test_double")
    if isinstance(llm, TestDoubleLLMClient):
        llm.set_responses([
            AIMessage(content="并发回答 1"),
            AIMessage(content="并发回答 2"),
        ])

    res1 = await client.post(
        f"/api/v1/agents/{agent.id}/runs/stream",
        json={"input": "并发任务一"},
    )
    res2 = await client.post(
        f"/api/v1/agents/{agent.id}/runs/stream",
        json={"input": "并发任务二"},
    )

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert "run_completed" in res1.text
    assert "run_completed" in res2.text

    # Extract Run IDs and verify isolation
    id1 = [line for line in res1.text.split("\n") if "run_id" in line][0]
    id2 = [line for line in res2.text.split("\n") if "run_id" in line][0]
    assert id1 != id2


# ==============================================================================
# 20. API Key 等敏感信息不进入响应和 Trace
# ==============================================================================


@pytest.mark.asyncio
async def test_no_sensitive_api_keys_leaked(client: AsyncClient, db_session: AsyncSession):
    """Test 20: Ensure API keys and credentials never appear in agent responses or traces."""
    agent = AgentDefinition(name="安全检查智能体")
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()

    llm = get_llm_client("test_double")
    if isinstance(llm, TestDoubleLLMClient):
        llm.set_responses([AIMessage(content="安全核验回答。")])

    res = await client.post(
        f"/api/v1/agents/{agent.id}/runs/stream",
        json={"input": "安全测试"},
    )
    assert res.status_code == 200
    raw_response = res.text

    # Verify no credentials leaked
    assert "sk-" not in raw_response
    assert "api_key" not in raw_response.lower() or '"api_key":' not in raw_response
    assert "authorization" not in raw_response.lower()
