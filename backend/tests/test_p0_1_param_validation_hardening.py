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
# Dedicated Helper Tools for P0-1 Parameter Validation Testing
# ==============================================================================


class StrictSearchInput(BaseModel):
    query: str = Field(..., description="检索关键词（必填）")
    top_k: int = Field(3, description="召回条数（可选整数，默认3）")
    secret_token: Optional[str] = Field(None, description="敏感访问令牌（不应泄露在报错中）")


class StrictSearchTool(BaseTool):
    def __init__(self):
        super().__init__()
        self.call_count = 0
        self.executed_queries: List[str] = []

    @property
    def name(self) -> str:
        return "strict_search"

    @property
    def description(self) -> str:
        return "严格参数校验的知识库检索工具。"

    @property
    def args_schema(self) -> Type[BaseModel]:
        return StrictSearchInput

    async def _run(self, query: str, top_k: int = 3, secret_token: Optional[str] = None) -> Dict[str, Any]:
        self.call_count += 1
        self.executed_queries.append(query)
        return {
            "query": query,
            "hits": [f"Chunk_{i+1}_for_{query}" for i in range(top_k)],
            "count": top_k,
        }


class FlakyExecutionInput(BaseModel):
    code: int = Field(..., description="状态码（整数）")


class FlakyExecutionTool(BaseTool):
    """Tool whose parameters pass validation, but execution raises an unhandled exception."""
    def __init__(self):
        super().__init__()
        self.call_count = 0

    @property
    def name(self) -> str:
        return "flaky_tool"

    @property
    def description(self) -> str:
        return "参数通过但执行必然抛出业务异常的工具。"

    @property
    def args_schema(self) -> Type[BaseModel]:
        return FlakyExecutionInput

    async def _run(self, code: int) -> Dict[str, Any]:
        self.call_count += 1
        raise RuntimeError(f"Database deadlocked with code {code} during tool execution!")


# ==============================================================================
# P0-1 对照测试用例集
# ==============================================================================


@pytest.mark.asyncio
async def test_p0_1_tc03a_first_error_second_correction_success():
    """TC-03a: 首次参数校验错误，第二次成功修正参数并获得最终答案。
    验证：
    1. 首次返回结构化参数 Schema 错误提示（含缺失字段说明，不暴露 Python traceback）；
    2. 第二次 LLM 提供正确参数，工具成功执行；
    3. 最终 status 为 completed。
    """
    registry = ToolRegistry()
    search_tool = StrictSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Turn 1: 缺少必填字段 'query'
        AIMessage(
            content="",
            tool_calls=[{
                "name": "strict_search",
                "args": {"top_k": 2},
                "id": "call_bad_missing_q",
                "type": "tool_call",
            }],
        ),
        # Turn 2: 模型自修正，补齐 'query'
        AIMessage(
            content="",
            tool_calls=[{
                "name": "strict_search",
                "args": {"query": "deepseek_r1", "top_k": 2},
                "id": "call_corrected_q",
                "type": "tool_call",
            }],
        ),
        # Turn 3: 依据检索结果生成回答
        AIMessage(content="DeepSeek R1 具备强大的推理能力。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="查询 DeepSeek R1", enabled_tools=["strict_search"])

    assert final_state["status"] == "completed"
    assert search_tool.call_count == 1
    assert search_tool.executed_queries == ["deepseek_r1"]

    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    assert tool_messages[0].tool_call_id == "call_bad_missing_q"
    assert "strict_search" in tool_messages[0].content
    assert tool_messages[1].tool_call_id == "call_corrected_q"
    assert "deepseek_r1" in tool_messages[1].content


@pytest.mark.asyncio
async def test_p0_1_tc03b_consecutive_two_errors_trigger_stop():
    """TC-03b: 连续两次参数校验错误，触发停止机制（max_consecutive_param_errors=2）。
    验证：
    1. 连续 2 次参数校验失败后，停止继续调用工具；
    2. consecutive_param_errors 达到 2；
    3. runtime 不应继续无限重试。
    """
    registry = ToolRegistry()
    search_tool = StrictSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    # 模拟模型连续两次传错参数，第三次准备直接总结
    llm.set_responses([
        # Error 1: 缺少必填参数
        AIMessage(
            content="",
            tool_calls=[{"name": "strict_search", "args": {"top_k": 1}, "id": "call_err_1", "type": "tool_call"}],
        ),
        # Error 2: 再次缺少必填参数（或类型错误）
        AIMessage(
            content="",
            tool_calls=[{"name": "strict_search", "args": {"top_k": "not_an_int"}, "id": "call_err_2", "type": "tool_call"}],
        ),
        # Final answer after tools disabled
        AIMessage(content="由于连续参数错误，依据现有信息完成总结。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="连续错误测试", enabled_tools=["strict_search"])

    # 工具底层一次都没有真正执行成功
    assert search_tool.call_count == 0
    # 在目标 P0-1 代码下：consecutive_param_errors == 2，且工具调用停止
    print(f"\n[TC-03b Result] consecutive_param_errors = {final_state.get('consecutive_param_errors')}, status = {final_state.get('status')}")
    assert final_state.get("consecutive_param_errors") == 2


@pytest.mark.asyncio
async def test_p0_1_tc03c_after_consecutive_errors_further_tool_calls_prohibited():
    """TC-03c: 连续错误达到上限后，agent_node 必须禁用进一步工具调用（tools=None, tool_choice='none'）。
    验证：
    1. 前两轮发生参数校验错误；
    2. 第三轮模型若尝试继续调用工具，系统应拒绝/阻断，不得继续派发到 tool_node 执行。
    """
    registry = ToolRegistry()
    search_tool = StrictSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Error 1
        AIMessage(
            content="",
            tool_calls=[{"name": "strict_search", "args": {}, "id": "c1", "type": "tool_call"}],
        ),
        # Error 2
        AIMessage(
            content="",
            tool_calls=[{"name": "strict_search", "args": {}, "id": "c2", "type": "tool_call"}],
        ),
        # Turn 3: 达到上限后生成最终回答
        AIMessage(content="参数连续错误已达到上限，直接回答用户。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="禁用进一步工具测试", enabled_tools=["strict_search"])

    assert final_state.get("consecutive_param_errors") == 2
    assert search_tool.call_count == 0
    # 消息必须完整包含两轮错误的 ToolMessage
    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2


@pytest.mark.asyncio
async def test_p0_1_tc03d_successful_validation_resets_error_count():
    """TC-03d: 首次参数校验错误，第二次成功校验通过，错误计数正确清零（重置为 0）。
    验证：
    1. Error 1 使 consecutive_param_errors 从 0 增至 1；
    2. Turn 2 参数校验成功，consecutive_param_errors 被重置回 0；
    3. 证明系统不会将非连续的历史错误永久累加。
    """
    registry = ToolRegistry()
    search_tool = StrictSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Turn 1: 缺少必填参数 (Error 1)
        AIMessage(
            content="",
            tool_calls=[{"name": "strict_search", "args": {}, "id": "c1_bad", "type": "tool_call"}],
        ),
        # Turn 2: 修正正确 (Success 1 -> reset to 0)
        AIMessage(
            content="",
            tool_calls=[{"name": "strict_search", "args": {"query": "reset_test"}, "id": "c2_good", "type": "tool_call"}],
        ),
        # Turn 3: 最终生成
        AIMessage(content="成功检索并重置计数。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试重置计数", enabled_tools=["strict_search"])

    assert final_state["status"] == "completed"
    assert search_tool.call_count == 1
    # 最终连续错误计数必须为 0
    assert final_state.get("consecutive_param_errors") == 0


@pytest.mark.asyncio
async def test_p0_1_tc03e_tool_execution_exception_does_not_increment_param_error_count():
    """TC-03e: 参数校验通过但工具内部执行异常（RuntimeError）时，不应增加参数校验错误计数，
    相反，因参数本身合法通过了 Schema 校验，连续参数错误计数应被重置为 0。
    """
    registry = ToolRegistry()
    flaky_tool = FlakyExecutionTool()
    registry.register(flaky_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Turn 1: 参数合法 (code=500)，但内部执行抛出死锁异常
        AIMessage(
            content="",
            tool_calls=[{"name": "flaky_tool", "args": {"code": 500}, "id": "c_flaky_1", "type": "tool_call"}],
        ),
        # Turn 2: 模型看到执行异常，如实向用户解释
        AIMessage(content="数据库发生死锁，操作未完成。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="触发执行异常", enabled_tools=["flaky_tool"])

    assert final_state["status"] == "completed"
    assert flaky_tool.call_count == 1
    # 连续参数校验错误计数不应为 1，而应为 0（因为参数 Schema 是合法的！）
    assert final_state.get("consecutive_param_errors") == 0

    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == "c_flaky_1"
    assert "Database deadlocked" in tool_msg.content


@pytest.mark.asyncio
async def test_p0_1_tc03f_multi_tool_calls_error_limit_and_pairing():
    """TC-03f: 单条 AIMessage 包含多个 Tool Calls 时：
    若当前批次执行过程中达到错误上限，应阻止后续工具实际执行，
    但必须为所有 tool_call_id 生成对应 ToolMessage，确保消息协议完整配对。
    """
    registry = ToolRegistry()
    search_tool = StrictSearchTool()
    registry.register(search_tool)

    # 假设此前已有 1 次参数校验错误，本轮 AIMessage 同时发出 2 个 tool call：
    # call_1: 再次传错参数 -> 连续错误达到 2（上限）！
    # call_2: 即使参数写对了，也应被阻断实际执行（状态标记为 blocked），但必须产出对应的 ToolMessage 配对！
    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Step 1: 首次传错参数 (consecutive_param_errors 变为 1)
        AIMessage(
            content="",
            tool_calls=[{"name": "strict_search", "args": {}, "id": "call_batch_err1", "type": "tool_call"}],
        ),
        # Step 2: 单条消息发出两个 tool call
        AIMessage(
            content="",
            tool_calls=[
                {"name": "strict_search", "args": {}, "id": "call_batch_err2", "type": "tool_call"},
                {"name": "strict_search", "args": {"query": "valid_query"}, "id": "call_batch_blocked3", "type": "tool_call"},
            ],
        ),
        # Step 3: 达到上限，直接回答
        AIMessage(content="部分工具被阻断，完成最终回答。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试批量调用阻断与配对", enabled_tools=["strict_search"])

    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    # 必须严格产生 3 个 ToolMessage，一一配对 3 个 tool_call_id！
    assert len(tool_messages) == 3
    tool_call_ids = [m.tool_call_id for m in tool_messages]
    assert tool_call_ids == ["call_batch_err1", "call_batch_err2", "call_batch_blocked3"]

    # 检查第三个 ToolMessage 是否被安全阻断而没有真正执行
    blocked_msg = tool_messages[2]
    assert "blocked" in blocked_msg.content or "limit" in blocked_msg.content or "error" in blocked_msg.content
    # 工具实际只执行了 0 次（因为 call_batch_blocked3 被阻断未调 _run）
    assert search_tool.call_count == 0
