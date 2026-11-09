import asyncio
import json
import pytest
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.runtime.runtime import LangGraphAgentRuntime
from app.agent.tools.base import BaseTool
from app.agent.tools.registry import ToolRegistry
from app.llm.openai_client import TestDoubleLLMClient


# ==============================================================================
# Dedicated Helper Tools for P0-2 Duplicate Tool Call Testing
# ==============================================================================


class CalcInput(BaseModel):
    a: int = Field(..., description="First integer")
    b: int = Field(..., description="Second integer")


class TrackedCalculatorTool(BaseTool):
    def __init__(self):
        super().__init__()
        self.call_count = 0
        self.call_args_history: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "tracked_calc"

    @property
    def description(self) -> str:
        return "Add two numbers with execution counting."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return CalcInput

    async def _run(self, a: int, b: int) -> Dict[str, Any]:
        self.call_count += 1
        self.call_args_history.append({"a": a, "b": b})
        return {"result": a + b, "execution_number": self.call_count}


class SearchInput(BaseModel):
    query: str = Field(..., description="Search keyword")
    top_k: int = Field(3, description="Number of results")


class TrackedSearchTool(BaseTool):
    def __init__(self):
        super().__init__()
        self.call_count = 0
        self.searched_queries: List[str] = []

    @property
    def name(self) -> str:
        return "tracked_search"

    @property
    def description(self) -> str:
        return "Search with execution history tracking."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return SearchInput

    async def _run(self, query: str, top_k: int = 3) -> Dict[str, Any]:
        self.call_count += 1
        self.searched_queries.append(query)
        return {
            "query": query,
            "hits": [f"Result for {query} #{i+1}" for i in range(top_k)],
            "count": top_k,
        }


# ==============================================================================
# P0-2 对照测试用例集（TC-09a 至 TC-09f）
# ==============================================================================


@pytest.mark.asyncio
async def test_p0_2_tc09a_consecutive_duplicate_call_is_blocked():
    """TC-09a: 连续发起相同工具 + 相同参数调用时，第二次调用应被阻断实际执行。
    验证：
    1. 第一次调用执行底层 _run()，calc_count == 1；
    2. 第二次相同参数调用被拦截，calc_count 保持为 1（未重复执行）；
    3. 第二个 ToolMessage 包含 duplicate 拦截提示；
    4. tool_call_id 保持 100% 配对。
    """
    registry = ToolRegistry()
    calc_tool = TrackedCalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Turn 1: 首次调用
        AIMessage(
            content="",
            tool_calls=[{"name": "tracked_calc", "args": {"a": 10, "b": 20}, "id": "c1_first", "type": "tool_call"}],
        ),
        # Turn 2: 模型发出完全相同的重复调用
        AIMessage(
            content="",
            tool_calls=[{"name": "tracked_calc", "args": {"a": 10, "b": 20}, "id": "c2_dup", "type": "tool_call"}],
        ),
        # Turn 3: 最终生成
        AIMessage(content="计算完成，结果为 30。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="计算 10 加 20", enabled_tools=["tracked_calc"])

    assert final_state["status"] == "completed"
    # 核心断言：底层物理执行次数必须为 1，而不是 2！
    print(f"\n[TC-09a Result] calc_tool.call_count = {calc_tool.call_count}")
    assert calc_tool.call_count == 1

    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    assert tool_messages[0].tool_call_id == "c1_first"
    assert tool_messages[1].tool_call_id == "c2_dup"

    # 第二个 ToolMessage 必须明确提示重复调用拦截
    dup_content = tool_messages[1].content
    assert "duplicate" in dup_content.lower() or "重复" in dup_content


@pytest.mark.asyncio
async def test_p0_2_tc09b_same_tool_different_args_not_blocked():
    """TC-09b: 避免误拦截正常业务：同一工具使用不同参数时，必须正常允许执行。
    验证：
    1. 三轮调用同一个 tracked_calc 工具，但参数分别为 (1,2), (3,4), (5,6)；
    2. 三次全部正常执行，call_count == 3；
    3. 证明系统没有简单地按 tool_name 盲目封禁。
    """
    registry = ToolRegistry()
    calc_tool = TrackedCalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(content="", tool_calls=[{"name": "tracked_calc", "args": {"a": 1, "b": 2}, "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "tracked_calc", "args": {"a": 3, "b": 4}, "id": "call_2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "tracked_calc", "args": {"a": 5, "b": 6}, "id": "call_3", "type": "tool_call"}]),
        AIMessage(content="所有加法计算完成。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="多次计算不同数字", enabled_tools=["tracked_calc"])

    assert final_state["status"] == "completed"
    assert calc_tool.call_count == 3
    assert calc_tool.call_args_history == [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5, "b": 6}]


@pytest.mark.asyncio
async def test_p0_2_tc09c_canonical_fingerprint_normalization():
    """TC-09c: 参数键无序与字符串首尾空白规范化指纹识别。
    验证：
    - Turn 1: {"query": "python", "top_k": 3}
    - Turn 2: {"top_k": 3, "query": "python "} (键顺序颠倒，字符串带尾随空格)
    指纹归一化算法应能准确识别二者语义等价，并在 Turn 2 拦截重复调用。
    """
    registry = ToolRegistry()
    search_tool = TrackedSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(content="", tool_calls=[{"name": "tracked_search", "args": {"query": "python", "top_k": 3}, "id": "s1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "tracked_search", "args": {"top_k": 3, "query": "python "}, "id": "s2_norm", "type": "tool_call"}]),
        AIMessage(content="检索完成。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试指纹规范化", enabled_tools=["tracked_search"])

    assert final_state["status"] == "completed"
    # 第二次应被指纹归一化识别并拦截，实际底层搜索只执行 1 次！
    assert search_tool.call_count == 1
    assert search_tool.searched_queries == ["python"]


@pytest.mark.asyncio
async def test_p0_2_tc09d_bounded_consecutive_duplicate_limit_triggers_stop():
    """TC-09d: 连续重复调用达到有界上限（max_consecutive_duplicate_calls=2）触发停止机制。
    验证：
    1. Turn 1 正常调用；
    2. Turn 2、Turn 3 连续发送完全相同的重复调用；
    3. consecutive_duplicate_calls 达到 2 时，禁用后续工具调用，防止死耗步数；
    4. calc_tool 实际物理执行次数严格为 1。
    """
    registry = ToolRegistry()
    calc_tool = TrackedCalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    # 模拟模型进入“复读机”死循环
    llm.set_responses([
        AIMessage(content="", tool_calls=[{"name": "tracked_calc", "args": {"a": 7, "b": 8}, "id": "c1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "tracked_calc", "args": {"a": 7, "b": 8}, "id": "c2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "tracked_calc", "args": {"a": 7, "b": 8}, "id": "c3", "type": "tool_call"}]),
        AIMessage(content="重复调用已终止，输出总结。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_steps=10)
    final_state = await runtime.ainvoke(query="测试连续重复熔断", enabled_tools=["tracked_calc"])

    assert calc_tool.call_count == 1
    assert final_state.get("consecutive_duplicate_calls") == 2
    # 确认工具在连续 2 次重复拦截后不再派发执行


@pytest.mark.asyncio
async def test_p0_2_tc09e_model_adjusts_params_resets_duplicate_count():
    """TC-09e: 模型收到重复拦截警告后主动调整参数，连续重复计数正确清零（重置为 0）。
    验证：
    1. Turn 1 正常调用；
    2. Turn 2 发生重复调用（被拦截，consecutive_duplicate_calls 变为 1）；
    3. Turn 3 模型看到警告，修改参数发起新调用（执行成功，consecutive_duplicate_calls 重置为 0）。
    """
    registry = ToolRegistry()
    search_tool = TrackedSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Turn 1: 搜索 A (执行成功)
        AIMessage(content="", tool_calls=[{"name": "tracked_search", "args": {"query": "A"}, "id": "s1", "type": "tool_call"}]),
        # Turn 2: 重复搜索 A (拦截，count=1)
        AIMessage(content="", tool_calls=[{"name": "tracked_search", "args": {"query": "A"}, "id": "s2_dup", "type": "tool_call"}]),
        # Turn 3: 调整为搜索 B (执行成功，count重置为0)
        AIMessage(content="", tool_calls=[{"name": "tracked_search", "args": {"query": "B"}, "id": "s3_new", "type": "tool_call"}]),
        # Turn 4: 最终回答
        AIMessage(content="综合 A 和 B 的结果生成回答。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试调整参数重置计数", enabled_tools=["tracked_search"])

    assert final_state["status"] == "completed"
    assert search_tool.call_count == 2
    assert search_tool.searched_queries == ["A", "B"]
    # 最终连续重复计数必须被清零
    assert final_state.get("consecutive_duplicate_calls") == 0


@pytest.mark.asyncio
async def test_p0_2_tc09f_batch_tool_calls_duplicate_interception_and_pairing():
    """TC-09f: 单条 AIMessage 发出多个 tool_calls，包含重复项：
    - call_1: calc(a=1, b=1) -> 首次执行
    - call_2: calc(a=1, b=1) -> 批次内重复，被拦截
    - call_3: calc(a=2, b=2) -> 新参数，正常执行
    验证：
    1. 必须产生严格对应的 3 个 ToolMessage；
    2. calc_tool 实际物理执行次数严格为 2（非重复项），重复项未执行；
    3. 所有 tool_call_id 100% 配对。
    """
    registry = ToolRegistry()
    calc_tool = TrackedCalculatorTool()
    registry.register(calc_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[
                {"name": "tracked_calc", "args": {"a": 1, "b": 1}, "id": "batch_c1", "type": "tool_call"},
                {"name": "tracked_calc", "args": {"a": 1, "b": 1}, "id": "batch_c2_dup", "type": "tool_call"},
                {"name": "tracked_calc", "args": {"a": 2, "b": 2}, "id": "batch_c3_new", "type": "tool_call"},
            ],
        ),
        AIMessage(content="批量计算完成。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="批量计算并排重", enabled_tools=["tracked_calc"])

    assert final_state["status"] == "completed"
    # 物理执行只应执行 2 次（batch_c1 和 batch_c3_new）
    assert calc_tool.call_count == 2

    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 3
    assert [m.tool_call_id for m in tool_messages] == ["batch_c1", "batch_c2_dup", "batch_c3_new"]

    # 验证第二个为被拦截的重复消息
    dup_msg = tool_messages[1]
    assert "duplicate" in dup_msg.content.lower() or "重复" in dup_msg.content
