import asyncio
import json
import pytest
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.runtime.graph import create_agent_graph
from app.agent.runtime.nodes import (
    agent_node,
    citation_verifier_node,
    force_stop_node,
    tool_node,
)
from app.agent.runtime.routing import route_agent_decision
from app.agent.runtime.runtime import LangGraphAgentRuntime
from app.agent.runtime.state import AgentState
from app.agent.tools.base import BaseTool
from app.agent.tools.evidence import EvidenceItem, EvidenceRegistry
from app.agent.tools.knowledge_search import SearchKnowledgeBaseTool
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.schemas import ToolResult
from app.core.config import settings
from app.llm.openai_client import TestDoubleLLMClient
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.vector_store import ScoredChunk


# ==============================================================================
# Helper Dummy Tools for Edge-Case Testing
# ==============================================================================


class DummyAddInput(BaseModel):
    a: int = Field(..., description="First number")
    b: int = Field(..., description="Second number")


class DummyAddTool(BaseTool):
    @property
    def name(self) -> str:
        return "dummy_add"

    @property
    def description(self) -> str:
        return "Add two integers together."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return DummyAddInput

    async def _run(self, a: int, b: int) -> int:
        return a + b


class SlowToolInput(BaseModel):
    delay: float = 0.5


class SlowTool(BaseTool):
    @property
    def name(self) -> str:
        return "slow_tool"

    @property
    def description(self) -> str:
        return "A tool that sleeps to test timeout."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return SlowToolInput

    async def _run(self, delay: float = 0.5) -> str:
        await asyncio.sleep(delay)
        return "slow_ok"


class CrashingToolInput(BaseModel):
    param: str = "test"


class CrashingTool(BaseTool):
    @property
    def name(self) -> str:
        return "crashing_tool"

    @property
    def description(self) -> str:
        return "A tool that throws unhandled exception."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return CrashingToolInput

    async def _run(self, param: str = "test") -> str:
        raise RuntimeError("Fatal hardware crash inside tool execution!")


def create_fake_chunk(
    chunk_id: str,
    doc_id: str = "doc-1",
    doc_name: str = "gateway_spec.md",
    page_number: int = 1,
    chunk_index: int = 0,
    content: str = "网关最大连接数 500",
    score: float = 0.95,
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        document_name=doc_name,
        page_number=page_number,
        chunk_index=chunk_index,
        content=content,
        similarity_score=score,
        dense_rank=1,
        bm25_rank=1,
        dense_score=0.9,
        bm25_score=15.0,
        fusion_score=0.032,
        reranker_score=score,
        reranker_raw_score=3.5,
    )


# ==============================================================================
# 1. 不需要工具，直接生成最终回答
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_direct_final_answer_without_tools():
    """Test 1: LLM directly produces text answer without invoking any tools."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(content="Python 装饰器是用于修改函数或类行为的高阶函数。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(
        query="无需工具，请回答 Python 装饰器是什么？",
        enabled_tools=["dummy_add"],
    )

    assert final_state["status"] == "completed"
    assert "高阶函数" in final_state["final_answer"]
    assert final_state["tool_call_count"] == 0
    assert final_state["execution_steps"] == 1
    assert final_state["citations"] == []


# ==============================================================================
# 2. 一次知识库工具调用
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_single_knowledge_base_tool_call(db_session: AsyncSession):
    """Test 2: LLM executes 1 tool call, receives observation, and finishes."""
    kb = KnowledgeBase(name="KB-Single", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="spec.md", file_type="md", file_path="/tmp/s.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="网关服务全局超时配置默认值为 5000ms",
        embedding=[0.1] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add(chunk)
    await db_session.commit()

    registry = ToolRegistry()
    rag_tool = SearchKnowledgeBaseTool(db=db_session)
    registry.register(rag_tool)

    llm = TestDoubleLLMClient()
    t1 = AIMessage(
        content="",
        tool_calls=[{
            "name": "search_knowledge_base",
            "args": {"knowledge_base_id": kb.id, "query": "网关超时配置", "top_k": 2},
            "id": "call_kb_01",
            "type": "tool_call",
        }],
    )
    t2 = AIMessage(
        content="根据参考文档 [Ref-1]，网关服务全局超时配置默认值为 5000ms。",
    )
    llm.set_responses([t1, t2])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, db_session=db_session)
    final_state = await runtime.ainvoke(
        query="请查询网关超时配置",
        knowledge_base_id=kb.id,
        enabled_tools=["search_knowledge_base"],
    )

    assert final_state["status"] == "completed"
    assert final_state["tool_call_count"] == 1
    assert final_state["execution_steps"] == 2
    assert len(final_state["citations"]) == 1
    assert final_state["citations"][0]["citation_index"] == 1
    assert final_state["citations"][0]["document_name"] == "spec.md"


# ==============================================================================
# 3. 连续两次工具调用
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_consecutive_two_tool_calls():
    """Test 3: LLM performs 2 consecutive tool calls in separate turns before final answer."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    # Turn 1: call add(2, 3)
    t1 = AIMessage(
        content="",
        tool_calls=[{"name": "dummy_add", "args": {"a": 2, "b": 3}, "id": "call_add_1", "type": "tool_call"}],
    )
    # Turn 2: call add(5, 10)
    t2 = AIMessage(
        content="",
        tool_calls=[{"name": "dummy_add", "args": {"a": 5, "b": 10}, "id": "call_add_2", "type": "tool_call"}],
    )
    # Turn 3: final answer
    t3 = AIMessage(content="两次加法计算结果分别为 5 和 15。")
    llm.set_responses([t1, t2, t3])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_steps=5)
    final_state = await runtime.ainvoke(
        query="连续计算加法",
        enabled_tools=["dummy_add"],
    )

    assert final_state["status"] == "completed"
    assert final_state["tool_call_count"] == 2
    assert final_state["execution_steps"] == 3
    assert "5 和 15" in final_state["final_answer"]


# ==============================================================================
# 4. 一次模型响应包含多个工具调用
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_multiple_tool_calls_in_single_turn():
    """Test 4: LLM emits 2 tool calls in a single AIMessage, both executed by Tool Node."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    t1 = AIMessage(
        content="",
        tool_calls=[
            {"name": "dummy_add", "args": {"a": 10, "b": 20}, "id": "c1", "type": "tool_call"},
            {"name": "dummy_add", "args": {"a": 100, "b": 200}, "id": "c2", "type": "tool_call"},
        ],
    )
    t2 = AIMessage(content="批量计算完成：10+20=30, 100+200=300。")
    llm.set_responses([t1, t2])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="同时计算两组数", enabled_tools=["dummy_add"])

    assert final_state["status"] == "completed"
    assert final_state["tool_call_count"] == 2
    assert final_state["execution_steps"] == 2
    # Verify tool messages in history
    tool_msgs = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    assert tool_msgs[0].tool_call_id == "c1"
    assert tool_msgs[0].content == "30"
    assert tool_msgs[1].tool_call_id == "c2"
    assert tool_msgs[1].content == "300"


# ==============================================================================
# 5. 工具结果正确返回给 LLM
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_observation_returned_to_llm():
    """Test 5: Verify the observation is accurately placed in messages before next LLM turn."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    captured_messages: List[List[Any]] = []

    class InspectingLLM(TestDoubleLLMClient):
        async def generate_with_tools(self, messages, tools=None, tool_choice="auto", temperature=None, max_tokens=None):
            captured_messages.append(list(messages))
            return await super().generate_with_tools(messages, tools, tool_choice, temperature, max_tokens)

    llm = InspectingLLM()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{"name": "dummy_add", "args": {"a": 7, "b": 8}, "id": "call_78", "type": "tool_call"}],
        ),
        AIMessage(content="7+8的结果是15。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    await runtime.ainvoke(query="计算7+8", enabled_tools=["dummy_add"])

    # On second call, inspect messages seen by LLM
    assert len(captured_messages) == 2
    turn2_messages = captured_messages[1]
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "call_78" and m.content == "15" for m in turn2_messages)


# ==============================================================================
# 6. 多轮 ToolMessage 与 tool_call_id 匹配
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_message_tool_call_id_matching():
    """Test 6: Strict tool_call_id matching between AIMessage.tool_calls and ToolMessages."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dummy_add", "args": {"a": 1, "b": 1}, "id": "unique_id_aaa", "type": "tool_call"},
                {"name": "dummy_add", "args": {"a": 2, "b": 2}, "id": "unique_id_bbb", "type": "tool_call"},
            ],
        ),
        AIMessage(content="完成计算。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试ID对应", enabled_tools=["dummy_add"])

    id_map = {m.tool_call_id: m.content for m in final_state["messages"] if isinstance(m, ToolMessage)}
    assert id_map["unique_id_aaa"] == "2"
    assert id_map["unique_id_bbb"] == "4"


# ==============================================================================
# 7. 未注册工具调用
# ==============================================================================


@pytest.mark.asyncio
async def test_unregistered_tool_call_handling():
    """Test 7: Unregistered or unauthorized tool call safely returns ToolMessage error without crash."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{"name": "unregistered_hack_tool", "args": {}, "id": "bad_call_01", "type": "tool_call"}],
        ),
        AIMessage(content="检测到未授权工具，无法执行。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="非法工具", enabled_tools=["dummy_add"])

    assert final_state["status"] == "completed"
    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == "bad_call_01"
    assert "not permitted" in tool_msg.content or "not registered" in tool_msg.content


# ==============================================================================
# 8. 非法工具参数
# ==============================================================================


@pytest.mark.asyncio
async def test_invalid_tool_parameters_handling():
    """Test 8: Pydantic validation failure returns error ToolMessage without crashing runtime."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Passing string 'not-a-number' for integer field 'a'
        AIMessage(
            content="",
            tool_calls=[{"name": "dummy_add", "args": {"a": "not-an-int", "b": 10}, "id": "call_bad_args", "type": "tool_call"}],
        ),
        AIMessage(content="参数类型不匹配，无法计算。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="错误参数", enabled_tools=["dummy_add"])

    assert final_state["status"] == "completed"
    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert "Invalid arguments" in tool_msg.content


# ==============================================================================
# 9. 工具执行异常
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_execution_exception_handling():
    """Test 9: Tool throwing unhandled exception returns structured ToolMessage without crashing."""
    registry = ToolRegistry()
    registry.register(CrashingTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{"name": "crashing_tool", "args": {"param": "x"}, "id": "crash_call", "type": "tool_call"}],
        ),
        AIMessage(content="工具执行出现硬件故障，已向用户如实解释。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="触发异常", enabled_tools=["crashing_tool"])

    assert final_state["status"] == "completed"
    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert "Fatal hardware crash" in tool_msg.content


# ==============================================================================
# 10. 工具超时
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_timeout_handling():
    """Test 10: Tool exceeding tool_timeout gets interrupted and returns error ToolMessage."""
    registry = ToolRegistry()
    registry.register(SlowTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{"name": "slow_tool", "args": {"delay": 2.0}, "id": "slow_call", "type": "tool_call"}],
        ),
        AIMessage(content="工具执行超时，向用户说明。"),
    ])

    # tool_timeout set to 0.1s
    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, tool_timeout=0.1)
    final_state = await runtime.ainvoke(query="测试工具超时", enabled_tools=["slow_tool"])

    assert final_state["status"] == "completed"
    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert "timed out after 0.1s" in tool_msg.content


# ==============================================================================
# 11. LLM 请求失败
# ==============================================================================


@pytest.mark.asyncio
async def test_llm_request_failure_handling():
    """Test 11: LLM raising connection exception gracefully sets status='failed' with error recorded."""
    registry = ToolRegistry()
    llm = TestDoubleLLMClient()
    # Script an exception on the LLM call
    llm.set_responses([RuntimeError("OpenAI API 500 Connection Refused")])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试LLM报错")

    assert final_state["status"] == "failed"
    assert "OpenAI API 500" in final_state["error"]


# ==============================================================================
# 12. 最大执行步数限制
# ==============================================================================


@pytest.mark.asyncio
async def test_max_execution_steps_limit():
    """Test 12: Loop prevention triggers force_stop_node when steps >= max_steps."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    # Script 3 tool calls (to reach max_steps=3) followed by the force stop final summary
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "dummy_add", "args": {"a": i, "b": 1}, "id": f"call_{i}", "type": "tool_call"}],
        )
        for i in range(3)
    ]
    calls.append(AIMessage(content="已达到最大步数，依据现有结果输出总结。"))
    llm.set_responses(calls)

    # max_steps set to 3
    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_steps=3)
    final_state = await runtime.ainvoke(query="死循环加法", enabled_tools=["dummy_add"])

    assert final_state["status"] == "max_steps_reached"
    assert final_state["execution_steps"] <= 4
    assert "依据现有结果输出总结" in final_state["final_answer"]


# ==============================================================================
# 13. 最大工具调用次数限制
# ==============================================================================


@pytest.mark.asyncio
async def test_max_tool_calls_limit():
    """Test 13: Cap on total tool calls triggers force_stop_node."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dummy_add", "args": {"a": 1, "b": 1}, "id": "c1", "type": "tool_call"},
                {"name": "dummy_add", "args": {"a": 2, "b": 2}, "id": "c2", "type": "tool_call"},
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dummy_add", "args": {"a": 3, "b": 3}, "id": "c3", "type": "tool_call"},
            ],
        ),
        AIMessage(content="工具调用达到上限，生成最终回答。"),
    ])

    # max_tool_calls set to 2
    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_tool_calls=2)
    final_state = await runtime.ainvoke(query="超出工具调用次数", enabled_tools=["dummy_add"])

    assert final_state["status"] == "max_steps_reached"
    assert final_state["tool_call_count"] >= 2
    assert "生成最终回答" in final_state["final_answer"]


# ==============================================================================
# 14. 用户取消执行
# ==============================================================================


@pytest.mark.asyncio
async def test_user_cancellation_propagation():
    """Test 14: asyncio.CancelledError cleanly propagates out without being swallowed."""
    registry = ToolRegistry()
    registry.register(SlowTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{"name": "slow_tool", "args": {"delay": 5.0}, "id": "c_slow", "type": "tool_call"}],
        ),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)

    task = asyncio.create_task(runtime.ainvoke(query="取消测试", enabled_tools=["slow_tool"]))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ==============================================================================
# 15. EvidenceRegistry 跨工具调用去重
# ==============================================================================


@pytest.mark.asyncio
async def test_evidence_registry_cross_call_dedup():
    """Test 15: Chunks recalled across multiple tool calls are deduplicated and retain [Ref-X]."""
    evidence_reg = EvidenceRegistry()
    c_a = create_fake_chunk("chunk-A", content="规范A")
    c_b = create_fake_chunk("chunk-B", content="规范B")
    c_c = create_fake_chunk("chunk-C", content="规范C")

    # Turn 1 recalls A and B
    items1 = evidence_reg.register_chunks([c_a, c_b])
    assert items1[0].tag == "[Ref-1]"
    assert items1[1].tag == "[Ref-2]"

    # Turn 2 recalls B and C
    items2 = evidence_reg.register_chunks([c_b, c_c])
    assert items2[0].tag == "[Ref-2]"  # Deduplicated!
    assert items2[1].tag == "[Ref-3]"  # New sequential index

    assert evidence_reg.total_evidence_count == 3
    all_chunks = evidence_reg.get_all_chunks()
    assert [c.chunk_id for c in all_chunks] == ["chunk-A", "chunk-B", "chunk-C"]


# ==============================================================================
# 16. 不同 Run 之间证据隔离
# ==============================================================================


@pytest.mark.asyncio
async def test_evidence_isolation_between_runs():
    """Test 16: Separate runs have completely independent EvidenceRegistry instances."""
    registry = ToolRegistry()
    registry.register(DummyAddTool())

    llm = TestDoubleLLMClient()
    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)

    reg_run1 = EvidenceRegistry()
    reg_run1.register_chunks([create_fake_chunk("chunk-run1")])

    reg_run2 = EvidenceRegistry()
    reg_run2.register_chunks([create_fake_chunk("chunk-run2")])

    assert reg_run1.get_all_chunks()[0].chunk_id == "chunk-run1"
    assert reg_run2.get_all_chunks()[0].chunk_id == "chunk-run2"
    assert reg_run1.total_evidence_count == 1
    assert reg_run2.total_evidence_count == 1


# ==============================================================================
# 17. CitationVerifier 过滤伪造引用
# ==============================================================================


@pytest.mark.asyncio
async def test_citation_verifier_filters_fabricated_refs():
    """Test 17: Hallucinated citations like [Ref-99] are strictly filtered out."""
    evidence_reg = EvidenceRegistry()
    c_real = create_fake_chunk("real-chunk-1", doc_name="architecture.pdf", content="真实内容")
    evidence_reg.register_chunks([c_real])

    state: AgentState = {
        "messages": [
            AIMessage(content="根据真实资料 [Ref-1]，以及伪造资料 [Ref-99] 和 [Ref-0] 进行分析。"),
        ],
        "run_id": "test_run",
        "knowledge_base_id": "kb_1",
        "enabled_tools": [],
        "evidence": [],
        "execution_steps": 2,
        "tool_call_count": 0,
        "status": "running",
        "error": None,
        "final_answer": None,
        "citations": [],
        "trace_events": [],
    }

    config = {"configurable": {"evidence_registry": evidence_reg}}
    result = await citation_verifier_node(state, config)

    assert result["status"] == "completed"
    assert len(result["citations"]) == 1
    assert result["citations"][0]["citation_index"] == 1
    assert result["citations"][0]["chunk_id"] == "real-chunk-1"


# ==============================================================================
# 18. 完整 Agent → Tool → Agent → Final Answer 闭环
# ==============================================================================


@pytest.mark.asyncio
async def test_full_agent_loop_end_to_end(db_session: AsyncSession):
    """
    Test 18: Full Agentic RAG loop:
    1. Agent receives question.
    2. Agent invokes search_knowledge_base.
    3. Tool Node executes real retrieval on DB and returns ToolMessage with [Ref-1].
    4. Agent receives observation and generates final answer citing [Ref-1].
    5. CitationVerifier extracts and validates [Ref-1].
    """
    kb = KnowledgeBase(name="KB-E2E", embedding_model=settings.EMBEDDING_MODEL_NAME)
    db_session.add(kb)
    await db_session.flush()

    doc = Document(knowledge_base_id=kb.id, file_name="gateway.md", file_type="md", file_path="/tmp/g.md", status="COMPLETED")
    db_session.add(doc)
    await db_session.flush()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="网关核心服务对外连接池最大连接数配置为 500",
        embedding=[0.05] * settings.EMBEDDING_DIMENSIONS,
    )
    db_session.add(chunk)
    await db_session.commit()

    registry = ToolRegistry()
    rag_tool = SearchKnowledgeBaseTool(db=db_session)
    registry.register(rag_tool)

    llm = TestDoubleLLMClient()
    t1 = AIMessage(
        content="",
        tool_calls=[{
            "name": "search_knowledge_base",
            "args": {"knowledge_base_id": kb.id, "query": "网关最大连接数", "top_k": 3},
            "id": "e2e_call_01",
            "type": "tool_call",
        }],
    )
    t2 = AIMessage(
        content="根据知识库技术规范 [Ref-1]，网关对外连接池最大连接数配置为 500。",
    )
    llm.set_responses([t1, t2])

    runtime = LangGraphAgentRuntime(
        tool_registry=registry,
        llm_client=llm,
        db_session=db_session,
        max_steps=5,
    )

    final_state = await runtime.ainvoke(
        query="请查询网关最大连接数配置",
        knowledge_base_id=kb.id,
        enabled_tools=["search_knowledge_base"],
    )

    assert final_state["status"] == "completed"
    assert final_state["execution_steps"] == 2
    assert final_state["tool_call_count"] == 1
    assert "500" in final_state["final_answer"]
    assert len(final_state["citations"]) == 1
    assert final_state["citations"][0]["citation_index"] == 1
    assert final_state["citations"][0]["document_name"] == "gateway.md"
    assert len(final_state["evidence"]) >= 1
