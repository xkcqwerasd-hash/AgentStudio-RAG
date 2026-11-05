"""
AgentStudio-RAG — P0.3 Stage 3-B Live DeepSeek Tool Calling Verification Script
Performs real Function Calling verification with DeepSeek API (deepseek-chat)
evaluating the newly registered `search_conversation_attachments` tool alongside `search_knowledge_base`.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Add backend root to sys.path
backend_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_root))

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from app.core.config import settings
from app.llm.openai_client import OpenAILLMClient
from app.agent.tools.registry import get_default_tool_registry


async def verify_deepseek_agent_attachment_live():
    print("=" * 80)
    print("  AgentStudio-RAG P0.3 Stage 3-B: DeepSeek 真实 Function Calling 联调核验")
    print("=" * 80)

    api_key = settings.OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY", "")
    base_url = settings.OPENAI_BASE_URL or "https://api.deepseek.com"
    model = settings.LLM_MODEL or "deepseek-chat"

    print(f"LLM Base URL: {base_url}")
    print(f"LLM Model:    {model}")
    print(f"API Key:      {'[已就绪]' if api_key else '[缺失 - 将跳过外部请求]'}")

    if not api_key:
        print("\n[SKIP] 未检测到有效的 OPENAI_API_KEY，跳过真实在线测试。")
        return

    # 1. Inspect Default Tool Registry
    registry = get_default_tool_registry()
    tools_schema = registry.to_openai_tools()
    print(f"\n[1] ToolRegistry 导出 OpenAI Tools 规范 (共 {len(tools_schema)} 个工具):")
    tool_names = [t["function"]["name"] for t in tools_schema]
    print(f"    可用工具列表: {tool_names}")
    assert "search_conversation_attachments" in tool_names, "search_conversation_attachments 必须在注册工具中"
    assert "search_knowledge_base" in tool_names, "search_knowledge_base 必须在注册工具中"

    client = OpenAILLMClient(api_key=api_key, base_url=base_url, model=model)

    # 2. Test Scenario A: Autonomous Attachment Tool Decision
    print("\n" + "-" * 70)
    print("[测试场景 A] 用户针对上传附件提问 -> 模型应自主调用 search_conversation_attachments")
    print("-" * 70)
    system_prompt = (
        "你是一个企业级智能助手。当用户提出与当前会话上传的附件、文件、文档相关的问题时，"
        "请调用 search_conversation_attachments 工具检索附件内容；"
        "当用户询问企业系统或知识库标准规范时，调用 search_knowledge_base 工具；"
        "若问题为通用打招呼或常识，无需调用工具直接回答。"
    )
    user_query_a = "我刚刚在聊天中上传了一份名为《2026架构规划白皮书.pdf》的文件，请帮我检索第3章中关于分布式缓存架构的规划目标。"
    messages_a = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_query_a),
    ]

    t0 = time.perf_counter()
    resp_a = await client.generate_with_tools(messages=messages_a, tools=tools_schema, tool_choice="auto")
    dur_a = round((time.perf_counter() - t0) * 1000, 2)
    print(f"DeepSeek 响应耗时: {dur_a}ms")
    print(f"Finish Reason: {resp_a.response_metadata.get('finish_reason')}")
    tcs_a = getattr(resp_a, "tool_calls", []) or []
    print(f"调用工具数量: {len(tcs_a)}")
    for tc in tcs_a:
        print(f"  -> 工具名称: {tc['name']}, 参数: {json.dumps(tc['args'], ensure_ascii=False)}")

    assert len(tcs_a) >= 1, "场景 A 必须触发工具调用"
    assert tcs_a[0]["name"] == "search_conversation_attachments", f"期望调用 search_conversation_attachments，实际为 {tcs_a[0]['name']}"
    print("  >>> [PASS] 场景 A 成功自主决策调用 search_conversation_attachments 工具！")

    # 3. Test Scenario B: Mock Observation Feedback & Grounded Citation Answer
    print("\n" + "-" * 70)
    print("[测试场景 B] 回传 Observation 观察结果 -> 模型生成引用标注 [Ref-1] 的最终回答")
    print("-" * 70)
    tc_call_id = tcs_a[0]["id"]
    observation_data = json.dumps({
        "query": user_query_a,
        "evidence": [{
            "ref_id": 1,
            "document_name": "2026架构规划白皮书.pdf",
            "page_number": 3,
            "source_type": "pdf",
            "provenance_label": "页码: 第 3 页",
            "content": "第三章：分布式缓存架构全面升级至双活多集群架构，采用主从读写分离与自适应热点数据驱逐，保障P99延迟低于5ms。",
        }],
    }, ensure_ascii=False)

    messages_b = [
        SystemMessage(content=system_prompt + " 并在引述技术事实时末尾标注对应的引用编号如 [Ref-1]。"),
        HumanMessage(content=user_query_a),
        resp_a,
        ToolMessage(content=observation_data, tool_call_id=tc_call_id, name="search_conversation_attachments"),
    ]

    t0 = time.perf_counter()
    resp_b = await client.generate_with_tools(messages=messages_b, tools=tools_schema)
    dur_b = round((time.perf_counter() - t0) * 1000, 2)
    print(f"DeepSeek 响应耗时: {dur_b}ms")
    print("模型生成最终回答:")
    print(resp_b.content)
    assert "[Ref-1]" in resp_b.content or "Ref-1" in resp_b.content or "白皮书" in resp_b.content, "模型回答必须基于检索证据"
    print("  >>> [PASS] 场景 B 成功基于附件检索证据生成合规回答！")

    # 4. Test Scenario C: Autonomous Knowledge Base Decision
    print("\n" + "-" * 70)
    print("[测试场景 C] 用户针对企业知识库提问 -> 模型应自主调用 search_knowledge_base")
    print("-" * 70)
    user_query_c = "请在知识库 ID 为 'kb_corp_gateway' 中检索网关连接池配置要求与超时标准。"
    messages_c = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_query_c),
    ]

    t0 = time.perf_counter()
    resp_c = await client.generate_with_tools(messages=messages_c, tools=tools_schema, tool_choice="auto")
    dur_c = round((time.perf_counter() - t0) * 1000, 2)
    print(f"DeepSeek 响应耗时: {dur_c}ms")
    tcs_c = getattr(resp_c, "tool_calls", []) or []
    print(f"调用工具数量: {len(tcs_c)}")
    for tc in tcs_c:
        print(f"  -> 工具名称: {tc['name']}, 参数: {json.dumps(tc['args'], ensure_ascii=False)}")

    assert len(tcs_c) >= 1, "场景 C 必须触发工具调用"
    assert tcs_c[0]["name"] == "search_knowledge_base", f"期望调用 search_knowledge_base，实际为 {tcs_c[0]['name']}"
    print("  >>> [PASS] 场景 C 成功自主决策调用 search_knowledge_base 工具！")

    print("\n" + "=" * 80)
    print("  [SUCCESS] DeepSeek Function Calling 实测全部通过！多工具注册与自主路由完整验证！")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(verify_deepseek_agent_attachment_live())
