import asyncio
import json
import pytest
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.runtime.runtime import LangGraphAgentRuntime
from app.agent.tools.base import BaseTool
from app.agent.tools.evidence import EvidenceRegistry
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.schemas import ToolResult
from app.llm.openai_client import TestDoubleLLMClient


# ==============================================================================
# Helper Test Tools with Strong Schemas
# ==============================================================================


class CalcInput(BaseModel):
    a: int = Field(..., description="First integer operand")
    b: int = Field(..., description="Second integer operand")


class CalculatorTool(BaseTool):
    def __init__(self):
        super().__init__()
        self.call_count = 0

    @property
    def name(self) -> str:
        return "calculator_add"

    @property
    def description(self) -> str:
        return "Add two integers together."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return CalcInput

    async def _run(self, a: int, b: int) -> Dict[str, Any]:
        self.call_count += 1
        return {"result": a + b, "call_count": self.call_count}


class SearchMockInput(BaseModel):
    query: str = Field(..., description="Query keyword")
    top_k: int = Field(3, description="Number of items to retrieve")


class SearchMockTool(BaseTool):
    def __init__(self):
        super().__init__()
        self.call_history: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "mock_search"

    @property
    def description(self) -> str:
        return "Search mocked documents by query string."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return SearchMockInput

    async def _run(self, query: str, top_k: int = 3) -> Dict[str, Any]:
        self.call_history.append({"query": query, "top_k": top_k})
        return {
            "query": query,
            "hits": [f"Document chunk matching '{query}' #{i+1}" for i in range(top_k)],
            "total": top_k,
        }


class UserRoleInput(BaseModel):
    username: str = Field(..., description="User account name")


class UserRoleTool(BaseTool):
    @property
    def name(self) -> str:
        return "get_user_role"

    @property
    def description(self) -> str:
        return "Get user role and assigned department ID."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return UserRoleInput

    async def _run(self, username: str) -> Dict[str, Any]:
        if username == "alice":
            return {"username": "alice", "role": "lead_architect", "dept_id": "dept_engineering"}
        return {"username": username, "role": "guest", "dept_id": "dept_general"}


class DeptBudgetInput(BaseModel):
    dept_id: str = Field(..., description="Department ID")


class DeptBudgetTool(BaseTool):
    @property
    def name(self) -> str:
        return "get_dept_budget"

    @property
    def description(self) -> str:
        return "Get budget for a specific department."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return DeptBudgetInput

    async def _run(self, dept_id: str) -> Dict[str, Any]:
        budgets = {"dept_engineering": 500000, "dept_general": 50000}
        return {"dept_id": dept_id, "annual_budget": budgets.get(dept_id, 0)}


class CrashingMockToolInput(BaseModel):
    param: str = Field(..., description="Test parameter")


class CrashingMockTool(BaseTool):
    @property
    def name(self) -> str:
        return "crashing_mock_tool"

    @property
    def description(self) -> str:
        return "Tool that deliberately raises an unhandled exception."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return CrashingMockToolInput

    async def _run(self, param: str) -> Dict[str, Any]:
        raise ZeroDivisionError(f"Simulated internal crash with param: {param}")


class DelayMockToolInput(BaseModel):
    duration_s: float = Field(0.5, description="Sleep duration in seconds")


class DelayMockTool(BaseTool):
    @property
    def name(self) -> str:
        return "delay_mock_tool"

    @property
    def description(self) -> str:
        return "Tool that sleeps to test timeout handling."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return DelayMockToolInput

    async def _run(self, duration_s: float = 0.5) -> Dict[str, Any]:
        await asyncio.sleep(duration_s)
        return {"status": "ok", "slept": duration_s}


# ==============================================================================
# TC-01: 正常单步工具调用
# ==============================================================================


@pytest.mark.asyncio
async def test_tc01_normal_single_step_tool_call():
    """TC-01: LLM executes 1 valid tool call, receives observation, and finishes."""
    registry = ToolRegistry()
    calc_tool = CalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "calculator_add",
                "args": {"a": 12, "b": 30},
                "id": "call_calc_01",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="计算结果为 42。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="计算 12 加 30", enabled_tools=["calculator_add"])

    assert final_state["status"] == "completed"
    assert "42" in final_state["final_answer"]
    assert final_state["tool_call_count"] == 1
    assert final_state["execution_steps"] == 2
    assert calc_tool.call_count == 1

    tool_msg = next((m for m in final_state["messages"] if isinstance(m, ToolMessage)), None)
    assert tool_msg is not None
    assert tool_msg.tool_call_id == "call_calc_01"
    parsed_content = json.loads(tool_msg.content)
    assert parsed_content["result"] == 42


# ==============================================================================
# TC-02: 多轮依赖工具调用
# ==============================================================================


@pytest.mark.asyncio
async def test_tc02_multi_turn_dependent_tool_calls():
    """TC-02: Step 1 gets user dept, Step 2 uses dept to query budget, Step 3 finishes."""
    registry = ToolRegistry()
    registry.register(UserRoleTool())
    registry.register(DeptBudgetTool())

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Step 1: Query user role
        AIMessage(
            content="",
            tool_calls=[{
                "name": "get_user_role",
                "args": {"username": "alice"},
                "id": "call_role_01",
                "type": "tool_call",
            }],
        ),
        # Step 2: Query department budget using returned dept_id
        AIMessage(
            content="",
            tool_calls=[{
                "name": "get_dept_budget",
                "args": {"dept_id": "dept_engineering"},
                "id": "call_budget_01",
                "type": "tool_call",
            }],
        ),
        # Step 3: Final answer
        AIMessage(content="Alice 属于工程部 (dept_engineering)，年度预算为 500,000 元。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(
        query="Alice 部门的预算是多少？",
        enabled_tools=["get_user_role", "get_dept_budget"],
    )

    assert final_state["status"] == "completed"
    assert "500,000" in final_state["final_answer"]
    assert final_state["tool_call_count"] == 2
    assert final_state["execution_steps"] == 3

    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    assert tool_messages[0].tool_call_id == "call_role_01"
    assert tool_messages[1].tool_call_id == "call_budget_01"


# ==============================================================================
# TC-03: 缺少必填参数与有界自修复上限
# ==============================================================================


@pytest.mark.asyncio
async def test_tc03_missing_required_parameter_self_correction():
    """TC-03a: Missing required parameter returns schema error, LLM self-corrects once."""
    registry = ToolRegistry()
    search_tool = SearchMockTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Step 1: Call missing required 'query'
        AIMessage(
            content="",
            tool_calls=[{
                "name": "mock_search",
                "args": {"top_k": 3},  # missing 'query'
                "id": "call_bad_missing_01",
                "type": "tool_call",
            }],
        ),
        # Step 2: Model sees error feedback, corrects by providing 'query'
        AIMessage(
            content="",
            tool_calls=[{
                "name": "mock_search",
                "args": {"query": "python_asyncio", "top_k": 2},
                "id": "call_corrected_02",
                "type": "tool_call",
            }],
        ),
        # Step 3: Final answer
        AIMessage(content="找到关于 python_asyncio 的相关文档。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="搜索异步资料", enabled_tools=["mock_search"])

    assert final_state["status"] == "completed"
    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2

    # First tool message has error feedback indicating query is missing
    assert tool_messages[0].tool_call_id == "call_bad_missing_01"
    assert "error" in tool_messages[0].content

    # Second tool message has success result
    assert tool_messages[1].tool_call_id == "call_corrected_02"
    assert "python_asyncio" in tool_messages[1].content


@pytest.mark.asyncio
async def test_tc03_exceeding_bounded_param_retry_limit():
    """TC-03b (Defect Exposure - P0-1): Model repeatedly generates invalid parameters (>2 times).
    Target P0-1 expectation: Bounded self-correction limit (max 2 attempts) should halt
    futile parameter retries and trigger graceful degradation.
    Current baseline: System has NO parameter retry limit and blindly consumes steps until max_steps.
    """
    registry = ToolRegistry()
    search_tool = SearchMockTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    # Script 4 consecutive invalid calls missing 'query'
    llm.set_responses([
        AIMessage(content="", tool_calls=[{"name": "mock_search", "args": {"top_k": 1}, "id": "bad_1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "mock_search", "args": {"top_k": 2}, "id": "bad_2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "mock_search", "args": {"top_k": 3}, "id": "bad_3", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "mock_search", "args": {"top_k": 4}, "id": "bad_4", "type": "tool_call"}]),
        AIMessage(content="总结。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_steps=10)
    final_state = await runtime.ainvoke(query="持续传错参数", enabled_tools=["mock_search"])

    # In target P0-1: after 2 failures, parameter retry guard halts further tool execution
    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    print(f"\n[TC-03b Bounded Retries] total tool messages with error = {len(tool_messages)}")
    assert len(tool_messages) == 2, "P0-1 verified: parameter retries bounded at exactly 2."



# ==============================================================================
# TC-04: 参数类型不符
# ==============================================================================


@pytest.mark.asyncio
async def test_tc04_parameter_type_mismatch():
    """TC-04: Integer field passed string 'not_a_number' returns validation error."""
    registry = ToolRegistry()
    calc_tool = CalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Step 1: Wrong type for 'a'
        AIMessage(
            content="",
            tool_calls=[{
                "name": "calculator_add",
                "args": {"a": "invalid_string_not_int", "b": 10},
                "id": "call_wrong_type",
                "type": "tool_call",
            }],
        ),
        # Step 2: Model corrects types
        AIMessage(
            content="",
            tool_calls=[{
                "name": "calculator_add",
                "args": {"a": 5, "b": 10},
                "id": "call_corrected_type",
                "type": "tool_call",
            }],
        ),
        # Step 3: Final answer
        AIMessage(content="5 加 10 等于 15。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="计算 5 加 10", enabled_tools=["calculator_add"])

    assert final_state["status"] == "completed"
    assert "15" in final_state["final_answer"]
    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    assert "error" in tool_messages[0].content or "Invalid arguments" in tool_messages[0].content


# ==============================================================================
# TC-05: 畸形 JSON 参数串与 invalid_tool_calls 处理
# ==============================================================================


@pytest.mark.asyncio
async def test_tc05_baseline_silently_drops_invalid_tool_calls():
    """TC-05a (Defect Exposure - P0-1/P0-3): LLM returns malformed JSON, placed in invalid_tool_calls.
    Current baseline defect: routing.py ignores invalid_tool_calls, seeing tool_calls==[],
    routing straight to citation_verifier_node. ToolMessage count is 0 (silently swallowed!).
    Target P0-3 expectation: routing.py & tool_node must process invalid_tool_calls and return
    a structured error ToolMessage so the model can self-correct.
    """
    registry = ToolRegistry()
    search_tool = SearchMockTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            invalid_tool_calls=[{
                "name": "mock_search",
                "args": '{"query": "unterminated_string',
                "id": "call_malformed_json",
                "error": "Unterminated string",
            }],
        ),
        AIMessage(content="检测到参数 JSON 解析失败。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试坏JSON", enabled_tools=["mock_search"])

    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    print(f"\n[TC-05 Verified] ToolMessages count = {len(tool_messages)}")
    # P0-3 verified: exactly 1 ToolMessage generated, strictly paired with call_malformed_json!
    assert len(tool_messages) == 1, "P0-3 verified: invalid_tool_calls generates paired ToolMessage."
    assert tool_messages[0].tool_call_id == "call_malformed_json"
    assert "error" in tool_messages[0].content or "JSON" in tool_messages[0].content


@pytest.mark.asyncio
async def test_tc05_markdown_fenced_json_direct_loads_failure():
    """TC-05b (Defect Exposure - P0-1): Tests that standard json.loads fails on markdown codeblocks.
    Target P0-1 expectation: Add robust markdown codeblock stripping so ```json {..} ``` can be parsed.
    """
    fenced_raw = '```json\n{"query": "markdown_fenced_test", "top_k": 2}\n```'
    with pytest.raises(Exception):
        json.loads(fenced_raw)



# ==============================================================================
# TC-06: 调用未注册工具
# ==============================================================================


@pytest.mark.asyncio
async def test_tc06_call_unregistered_tool():
    """TC-06: LLM attempts calling unregistered or forbidden tool."""
    registry = ToolRegistry()
    calc_tool = CalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "non_existent_magic_tool",
                "args": {"magic_key": "123"},
                "id": "call_ghost_01",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="该工具未授权或不存在，已终止调用。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="调用魔法工具", enabled_tools=["calculator_add"])

    assert final_state["status"] == "completed"
    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == "call_ghost_01"
    assert "not permitted" in tool_msg.content or "not registered" in tool_msg.content


# ==============================================================================
# TC-07: 工具内部异常隔离
# ==============================================================================


@pytest.mark.asyncio
async def test_tc07_tool_internal_exception_isolation():
    """TC-07: Tool raises ZeroDivisionError, runtime isolates it and returns error ToolMessage."""
    registry = ToolRegistry()
    crashing_tool = CrashingMockTool()
    registry.register(crashing_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "crashing_mock_tool",
                "args": {"param": "trigger_div_zero"},
                "id": "call_crash_01",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="工具执行出现异常，已捕获并返回。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="触发除零错误", enabled_tools=["crashing_mock_tool"])

    assert final_state["status"] == "completed"
    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == "call_crash_01"
    assert "ZeroDivisionError" in tool_msg.content or "Simulated internal crash" in tool_msg.content


# ==============================================================================
# TC-08: 工具超时降级
# ==============================================================================


@pytest.mark.asyncio
async def test_tc08_tool_timeout_handling():
    """TC-08: Tool exceeds tool_timeout (0.1s), gets cancelled, returns timeout ToolMessage."""
    registry = ToolRegistry()
    delay_tool = DelayMockTool()
    registry.register(delay_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "delay_mock_tool",
                "args": {"duration_s": 2.0},
                "id": "call_delay_01",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="工具执行超时，向用户报告。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, tool_timeout=0.1)
    final_state = await runtime.ainvoke(query="测试超时", enabled_tools=["delay_mock_tool"])

    assert final_state["status"] == "completed"
    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == "call_delay_01"
    assert "timed out after 0.1s" in tool_msg.content


# ==============================================================================
# TC-09: 重复相同工具调用（防死循环/防震荡）
# ==============================================================================


@pytest.mark.asyncio
async def test_tc09_repeated_identical_tool_call_loop():
    """TC-09: Tests runtime behavior when model generates duplicate calls with identical arguments.
    In baseline code: executes repeatedly without defense.
    In target P0-2 code: should intercept duplicate calls early.
    """
    registry = ToolRegistry()
    calc_tool = CalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    # Script model repeating the EXACT same call 3 times in a row
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "calculator_add",
                "args": {"a": 1, "b": 1},
                "id": "dup_call_01",
                "type": "tool_call",
            }],
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "calculator_add",
                "args": {"a": 1, "b": 1},
                "id": "dup_call_02",
                "type": "tool_call",
            }],
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "calculator_add",
                "args": {"a": 1, "b": 1},
                "id": "dup_call_03",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="重复执行完毕。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_steps=5)
    final_state = await runtime.ainvoke(query="重复计算 1 加 1", enabled_tools=["calculator_add"])

    print(f"\n[TC-09 Duplicate Calls] tool_call_count={final_state['tool_call_count']}, calc_tool.call_count={calc_tool.call_count}")
    # In P0-2: calc_tool was executed only once because 2nd and 3rd calls were blocked as duplicates!
    assert calc_tool.call_count == 1, "P0-2 verified: duplicate tool executed only once."
    assert final_state["status"] == "completed"


# ==============================================================================
# TC-10: 达到最大步数硬截断（force_stop_node）
# ==============================================================================


@pytest.mark.asyncio
async def test_tc10_max_execution_steps_cutoff():
    """TC-10: When execution_steps >= max_steps, routes to force_stop_node."""
    registry = ToolRegistry()
    calc_tool = CalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    # Script calls to exceed max_steps=2
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{"name": "calculator_add", "args": {"a": 1, "b": 1}, "id": "c1", "type": "tool_call"}],
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "calculator_add", "args": {"a": 2, "b": 2}, "id": "c2", "type": "tool_call"}],
        ),
        # force_stop_node summary response
        AIMessage(content="已达到最大步数限制，输出最终总结。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_steps=2)
    final_state = await runtime.ainvoke(query="测试步数超限", enabled_tools=["calculator_add"])

    assert final_state["status"] == "max_steps_reached"
    assert "输出最终总结" in final_state["final_answer"]
    assert any(t.get("event") == "agent_forced_final_summary" for t in final_state["trace_events"])


# ==============================================================================
# TC-11: 单步多工具并发调用与消息配对
# ==============================================================================


@pytest.mark.asyncio
async def test_tc11_single_turn_multiple_tool_calls_and_pairing():
    """TC-11: Single AIMessage emits 3 tool calls; all 3 ToolMessages must pair correctly."""
    registry = ToolRegistry()
    calc_tool = CalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[
                {"name": "calculator_add", "args": {"a": 10, "b": 1}, "id": "multi_call_aaa", "type": "tool_call"},
                {"name": "calculator_add", "args": {"a": 20, "b": 2}, "id": "multi_call_bbb", "type": "tool_call"},
                {"name": "calculator_add", "args": {"a": 30, "b": 3}, "id": "multi_call_ccc", "type": "tool_call"},
            ],
        ),
        AIMessage(content="三个加法结果分别为 11, 22, 33。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="并发计算三组数字", enabled_tools=["calculator_add"])

    assert final_state["status"] == "completed"
    assert final_state["tool_call_count"] == 3

    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 3

    # Strict ID and result pairing verification
    id_to_content = {m.tool_call_id: json.loads(m.content) for m in tool_messages}
    assert "multi_call_aaa" in id_to_content
    assert "multi_call_bbb" in id_to_content
    assert "multi_call_ccc" in id_to_content
    assert id_to_content["multi_call_aaa"]["result"] == 11
    assert id_to_content["multi_call_bbb"]["result"] == 22
    assert id_to_content["multi_call_ccc"]["result"] == 33
