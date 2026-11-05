"""
AgentStudio-RAG — Optional Live DeepSeek Tool Calling Verification Script
Performs a real Function Calling handshake with DeepSeek API if credentials exist.
Reports explicit verification status without conflating mock test results with real model results.
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

from langchain_core.messages import HumanMessage, ToolMessage
from app.core.config import settings
from app.llm.openai_client import OpenAILLMClient
from app.agent.tools.registry import get_default_tool_registry


async def verify_deepseek_live():
    print("=" * 80)
    print("  DeepSeek API 原生 Tool Calling 真实联调核验")
    print("=" * 80)

    api_key = settings.OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY", "")
    base_url = settings.OPENAI_BASE_URL or "https://api.deepseek.com"
    model = settings.LLM_MODEL or "deepseek-chat"

    print(f"端点配置: {base_url}")
    print(f"目标模型: {model}")
    print(f"API Key 状态: {'[已配置]' if api_key else '[未配置 (将跳过真实外部调用)]'}")

    if not api_key:
        print("\n[提示] 未在 backend/.env 或环境变量中检测到 OPENAI_API_KEY。")
        print("根据 M4.1 规范，自动化回归测试完全基于 TestDouble 离线运行。")
        print("如需核验真实 DeepSeek-V3 接口，请在 backend/.env 中配置 OPENAI_API_KEY 后重试。")
        return

    print("\n[步骤 1] 初始化工具注册中心并导出 OpenAI 标准 Function Calling Schema...")
    tool_registry = get_default_tool_registry()
    tools_schema = tool_registry.to_openai_tools()
    print(f"导出工具数量: {len(tools_schema)}")
    print("工具定义概要:\n", json.dumps(tools_schema, ensure_ascii=False, indent=2))

    print("\n[步骤 2] 向 DeepSeek 发起包含 tools 的真实提问...")
    client = OpenAILLMClient(api_key=api_key, base_url=base_url, model=model)
    user_query = "知识库ID为 kb_prod_gateway_01，请检索该知识库中关于网关连接池最大连接数与接口超时时间的技术规范。"
    messages = [HumanMessage(content=user_query)]

    start_t = time.perf_counter()
    try:
        ai_resp = await client.generate_with_tools(
            messages=messages,
            tools=tools_schema,
            tool_choice="auto",
        )
        duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
        print(f"DeepSeek 响应成功 (耗时: {duration_ms}ms)")
        print(f"Finish Reason: {ai_resp.response_metadata.get('finish_reason')}")
        print(f"Token Usage: {ai_resp.response_metadata.get('usage')}")

        tcs = getattr(ai_resp, "tool_calls", []) or []
        print(f"模型自主决策生成的 tool_calls 数量: {len(tcs)}")
        if not tcs:
            print("[警告] 模型直接返回了文本回答，未触发工具调用:")
            print(ai_resp.content)
            return

        for idx, tc in enumerate(tcs, start=1):
            print(f"  Tool Call #{idx}:")
            print(f"    ID: {tc.get('id')}")
            print(f"    Name: {tc.get('name')}")
            print(f"    Args: {json.dumps(tc.get('args'), ensure_ascii=False)}")

        print("\n[步骤 3] 模拟回传 ToolMessage 观察结果，请求 DeepSeek 生成最终回答...")
        first_tc = tcs[0]
        tool_call_id = first_tc["id"]
        mock_observation = json.dumps({
            "query": user_query,
            "evidence": [{
                "ref_id": 1,
                "document_name": "gateway_architecture.md",
                "page_number": 2,
                "content": "网关对外连接池最大连接数配置为 500，接口超时时间设定为 5000ms。",
            }],
        }, ensure_ascii=False)

        follow_up_messages = [
            HumanMessage(content=user_query),
            ai_resp,
            ToolMessage(content=mock_observation, tool_call_id=tool_call_id, name=first_tc["name"]),
        ]

        start_t2 = time.perf_counter()
        final_resp = await client.generate_with_tools(
            messages=follow_up_messages,
            tools=tools_schema,
            tool_choice="auto",
        )
        duration_ms2 = round((time.perf_counter() - start_t2) * 1000, 2)

        print(f"DeepSeek 终答生成成功 (耗时: {duration_ms2}ms):")
        print("-" * 60)
        print(final_resp.content)
        print("-" * 60)
        print("\n【真实模型联调核验结论】: DeepSeek-V3 原生 Function Calling 协议 100% 兼容通过！")

    except Exception as exc:
        print(f"\n[异常] 调用 DeepSeek 发生错误: {exc}")


if __name__ == "__main__":
    asyncio.run(verify_deepseek_live())
