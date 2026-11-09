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
# Helper Test Tool
# ==============================================================================


class MockSearchInput(BaseModel):
    query: str = Field(..., description="Query keyword")
    top_k: int = Field(3, description="Number of items")


class MockSearchTool(BaseTool):
    def __init__(self):
        super().__init__()
        self.call_count = 0
        self.last_query: Optional[str] = None

    @property
    def name(self) -> str:
        return "mock_search"

    @property
    def description(self) -> str:
        return "Search tool for testing JSON parsing."

    @property
    def args_schema(self) -> Type[BaseModel]:
        return MockSearchInput

    async def _run(self, query: str, top_k: int = 3) -> Dict[str, Any]:
        self.call_count += 1
        self.last_query = query
        return {"query": query, "hits": [f"Chunk_{i+1}" for i in range(top_k)]}


# ==============================================================================
# P0-3 对照测试用例集（TC-05a 至 TC-05f）
# ==============================================================================


@pytest.mark.asyncio
async def test_p0_3_tc05a_invalid_tool_calls_routed_and_message_paired():
    """TC-05a: 当大模型输出畸形 JSON 导致客户端将其解析至 invalid_tool_calls 时：
    1. routing.py 不得将请求静默放行至 citation_verifier_node；
    2. 必须路由至 tool_node 并产出具有对应 tool_call_id 的 ToolMessage；
    3. ToolMessage 必须包含清晰的语法报错信息；
    4. 记录对应 tool_malformed_json_error 的 Trace 事件；
    5. 模型在第二轮接收到错误反馈后纠正 JSON，正常获取结果。
    """
    registry = ToolRegistry()
    search_tool = MockSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # Turn 1: 模型返回畸形 JSON 字符串，被置入 invalid_tool_calls
        AIMessage(
            content="",
            invalid_tool_calls=[{
                "name": "mock_search",
                "args": '{"query": "unterminated_string',
                "id": "bad_json_call_1",
                "error": "Unterminated string starting at line 1 column 11 (char 10)",
            }],
        ),
        # Turn 2: 模型看到错误提示，自修复输出合法 JSON
        AIMessage(
            content="",
            tool_calls=[{
                "name": "mock_search",
                "args": {"query": "fixed_query", "top_k": 2},
                "id": "fixed_call_2",
                "type": "tool_call",
            }],
        ),
        # Turn 3: 最终生成
        AIMessage(content="检索成功，生成回答。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试坏JSON路由", enabled_tools=["mock_search"])

    assert final_state["status"] == "completed"
    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    # 必须产生 2 个 ToolMessage（bad_json_call_1 和 fixed_call_2 均必须配对！）
    assert len(tool_messages) == 2
    assert tool_messages[0].tool_call_id == "bad_json_call_1"
    assert "error" in tool_messages[0].content or "JSON" in tool_messages[0].content
    assert tool_messages[1].tool_call_id == "fixed_call_2"
    assert "fixed_query" in tool_messages[1].content

    # Trace 必须捕获到错误事件
    events = [t.get("event") for t in final_state["trace_events"]]
    assert "tool_malformed_json_error" in events or any("json" in str(e).lower() for e in events)


@pytest.mark.asyncio
async def test_p0_3_tc05b_hybrid_valid_and_invalid_tool_calls_in_single_turn():
    """TC-05b: 单轮 AIMessage 同时包含合法的 tool_calls 和畸形的 invalid_tool_calls。
    验证：
    1. 合法的 tool_call 正常执行；
    2. 畸形的 invalid_tool_call 返回错误 ToolMessage；
    3. 消息列表中两者的 ToolMessage 均存在且 ID 严格配对。
    """
    registry = ToolRegistry()
    search_tool = MockSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "mock_search",
                "args": {"query": "valid_query_1", "top_k": 1},
                "id": "valid_call_id",
                "type": "tool_call",
            }],
            invalid_tool_calls=[{
                "name": "mock_search",
                "args": "not a valid json",
                "id": "invalid_call_id",
                "error": "Expecting value: line 1 column 1 (char 0)",
            }],
        ),
        AIMessage(content="部分工具执行成功。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试混合调用", enabled_tools=["mock_search"])

    assert final_state["status"] == "completed"
    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    id_map = {m.tool_call_id: m for m in tool_messages}
    assert "valid_call_id" in id_map
    assert "invalid_call_id" in id_map
    assert "valid_query_1" in id_map["valid_call_id"].content
    assert "error" in id_map["invalid_call_id"].content.lower()


@pytest.mark.asyncio
async def test_p0_3_tc05c_markdown_fenced_json_codeblock_parsing():
    """TC-05c: 当大模型返回被 Markdown 代码块围栏包裹的 JSON 参数时（如 ```json {"query":"x"} ```）：
    系统应能鲁棒剥离围栏标记并正常解析执行，而不是抛出 JSONDecodeError。
    """
    from app.agent.tools.schemas import clean_and_parse_json

    # 1. 验证清洗器核心函数的剥离能力
    raw_fenced = '```json\n{"query": "markdown_codeblock_test", "top_k": 2}\n```'
    parsed = clean_and_parse_json(raw_fenced)
    assert parsed == {"query": "markdown_codeblock_test", "top_k": 2}

    # 2. 验证 Agent Runtime 端到端执行
    registry = ToolRegistry()
    search_tool = MockSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        # raw_args 作为字符串传入，且包含 ```json ... ```
        AIMessage.model_construct(
            content="",
            tool_calls=[{
                "name": "mock_search",
                "args": raw_fenced,
                "id": "fenced_call_1",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="检索成功。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试Markdown围栏JSON", enabled_tools=["mock_search"])

    assert final_state["status"] == "completed"
    assert search_tool.call_count == 1
    assert search_tool.last_query == "markdown_codeblock_test"

    tool_msg = next(m for m in final_state["messages"] if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == "fenced_call_1"
    parsed_res = json.loads(tool_msg.content)
    assert parsed_res["query"] == "markdown_codeblock_test"


@pytest.mark.asyncio
async def test_p0_3_tc05d_json_parse_error_trace_event_recorded():
    """TC-05d: 当 raw_args 字符串发生真正无法恢复的 JSON 解析错误时，
    tool_node 必须向 trace_events 追加标准化的 trace 记录，不能静默丢失。
    """
    registry = ToolRegistry()
    search_tool = MockSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage.model_construct(
            content="",
            tool_calls=[{
                "name": "mock_search",
                "args": '{"query": "unclosed_string',
                "id": "unclosed_call",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="报错后直接回答。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试JSON解析错误Trace", enabled_tools=["mock_search"])

    traces = final_state.get("trace_events", [])
    # 必须包含 JSON 解析错误的 Trace 事件
    json_err_trace = next((t for t in traces if t.get("event") in ["tool_json_parse_error", "tool_arguments_json_error", "tool_malformed_json_error"]), None)
    assert json_err_trace is not None
    assert json_err_trace["tool_call_id"] == "unclosed_call"
    assert json_err_trace["tool_name"] == "mock_search"
    assert "error" in json_err_trace


@pytest.mark.asyncio
async def test_p0_3_tc05e_unregistered_tool_trace_event_recorded():
    """TC-05e: 当模型调用未注册工具时，除了返回 ToolMessage，必须补齐 trace_events 记录。"""
    registry = ToolRegistry()
    # 允许列表包含 ghost_tool，但 registry 中并没有注册
    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "ghost_tool",
                "args": {"param": 1},
                "id": "ghost_call_1",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="工具不存在。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm)
    final_state = await runtime.ainvoke(query="测试未注册工具Trace", enabled_tools=["ghost_tool"])

    traces = final_state.get("trace_events", [])
    unregistered_trace = next((t for t in traces if t.get("event") in ["tool_not_registered", "tool_registry_error", "tool_discovery_error"]), None)
    assert unregistered_trace is not None
    assert unregistered_trace["tool_name"] == "ghost_tool"
    assert unregistered_trace["tool_call_id"] == "ghost_call_1"


@pytest.mark.asyncio
async def test_p0_3_tc05f_consecutive_malformed_json_limit_triggers_stop():
    """TC-05f: 当模型连续两次产生畸形 JSON 时，有界熔断机制应禁止进一步工具调用，
    防止因坏 JSON 陷入死循环。
    """
    registry = ToolRegistry()
    search_tool = MockSearchTool()
    registry.register(search_tool)

    llm = TestDoubleLLMClient()
    llm.set_responses([
        AIMessage(content="", invalid_tool_calls=[{"name": "mock_search", "args": "{bad1", "id": "b1", "error": "err1"}]),
        AIMessage(content="", invalid_tool_calls=[{"name": "mock_search", "args": "{bad2", "id": "b2", "error": "err2"}]),
        AIMessage(content="连续格式错误后输出最终答案。"),
    ])

    runtime = LangGraphAgentRuntime(tool_registry=registry, llm_client=llm, max_steps=10)
    final_state = await runtime.ainvoke(query="测试连续坏JSON熔断", enabled_tools=["mock_search"])

    # 两个 bad_calls 均必须生成 ToolMessage 配对
    tool_messages = [m for m in final_state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    assert [m.tool_call_id for m in tool_messages] == ["b1", "b2"]
    assert search_tool.call_count == 0
    assert final_state["status"] == "completed"


def test_p0_3_tc05g_message_protocol_serialization_and_orphan_prevention():
    """TC-05g: 验证消息协议兼容性与 OpenAI API 序列化无孤立 tool_call_id。
    1. 纯 invalid_tool_calls：下一轮 Assistant 消息必须声明对应 tool_calls，消除孤立 ID；
    2. 混合合法与非法调用：声明顺序与 ToolMessage 顺序严格一致；
    3. missing id 场景：就地生成并补全，确保 Assistant 与 ToolMessage 的 ID 100% 相同；
    4. clean_and_parse_json 语义纯洁度验证：不猜测语义，格式非法时纯粹报错。
    """
    from app.llm.openai_client import messages_to_openai_payload
    from app.agent.tools.schemas import clean_and_parse_json

    # 1. 纯 invalid_tool_calls 序列化测试
    ai_bad = AIMessage(
        content="",
        invalid_tool_calls=[{
            "name": "mock_search",
            "args": '{"query": "broken_json',
            "id": "bad_call_id_101",
            "error": "Unterminated string",
        }],
    )
    tool_bad = ToolMessage(
        content='{"error": "bad json"}',
        tool_call_id="bad_call_id_101",
        name="mock_search",
    )
    payload_bad = messages_to_openai_payload([
        HumanMessage(content="test query"),
        ai_bad,
        tool_bad,
    ])
    assert len(payload_bad) == 3
    asst_msg = payload_bad[1]
    assert asst_msg["role"] == "assistant"
    assert "tool_calls" in asst_msg
    assert len(asst_msg["tool_calls"]) == 1
    assert asst_msg["tool_calls"][0]["id"] == "bad_call_id_101"
    assert asst_msg["tool_calls"][0]["function"]["arguments"] == '{"query": "broken_json'
    # 验证 Tool 消息严格引用 Assistant 声明的 ID
    tool_payload = payload_bad[2]
    assert tool_payload["role"] == "tool"
    assert tool_payload["tool_call_id"] == asst_msg["tool_calls"][0]["id"]

    # 2. 混合合法与非法调用：顺序与协议合法性
    ai_hybrid = AIMessage(
        content="",
        tool_calls=[{
            "name": "mock_search",
            "args": {"query": "valid_1"},
            "id": "valid_call_201",
            "type": "tool_call",
        }],
        invalid_tool_calls=[{
            "name": "mock_search",
            "args": "{invalid_args",
            "id": "invalid_call_202",
            "error": "SyntaxError",
        }],
    )
    tool_valid = ToolMessage(content='{"data": 1}', tool_call_id="valid_call_201", name="mock_search")
    tool_invalid = ToolMessage(content='{"error": "err"}', tool_call_id="invalid_call_202", name="mock_search")
    payload_hybrid = messages_to_openai_payload([ai_hybrid, tool_valid, tool_invalid])
    declared_ids = [tc["id"] for tc in payload_hybrid[0]["tool_calls"]]
    subsequent_tool_ids = [m["tool_call_id"] for m in payload_hybrid[1:]]
    assert declared_ids == ["valid_call_201", "invalid_call_202"]
    assert subsequent_tool_ids == declared_ids

    # 3. invalid_tool_calls 缺少有效 ID 时，就地补齐并保持一致
    ai_missing_id = AIMessage(
        content="",
        invalid_tool_calls=[{
            "name": "mock_search",
            "args": "bad",
        }],
    )
    payload_missing = messages_to_openai_payload([ai_missing_id])
    generated_id = payload_missing[0]["tool_calls"][0]["id"]
    assert generated_id is not None and len(generated_id) > 0
    # 验证原 invalid_tool_calls 的 id 已被就地写入，防止后续 ID 漂移
    assert ai_missing_id.invalid_tool_calls[0]["id"] == generated_id

    # 4. clean_and_parse_json 语义安全性验证
    assert clean_and_parse_json('{"a": 1, "b": "hello"}') == {"a": 1, "b": "hello"}
    assert clean_and_parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        clean_and_parse_json('{"a": 1')
    with pytest.raises(json.JSONDecodeError):
        clean_and_parse_json("{'a': 1}")

