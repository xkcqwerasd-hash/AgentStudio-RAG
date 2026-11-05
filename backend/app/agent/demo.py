"""
AgentStudio-RAG — M4.1 Stage 2 Runnable Offline Demo
Demonstrates autonomous Agent -> Tool -> Observation -> Agent -> Citation Verification cycle.
"""

import asyncio
import json
import sys
from pathlib import Path

# Add backend root to sys.path
backend_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_root))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.runtime.runtime import LangGraphAgentRuntime
from app.agent.tools.evidence import EvidenceRegistry
from app.agent.tools.knowledge_search import SearchKnowledgeBaseTool
from app.agent.tools.registry import ToolRegistry
from app.core.config import settings
from app.db.session import Base
from app.llm.openai_client import TestDoubleLLMClient
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase


async def run_demo():
    print("=" * 80)
    print("  AgentStudio-RAG — M4.1 Stage 2: LangGraph Agent Runtime Offline Demo")
    print("=" * 80)

    # 1. Initialize In-memory Database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        # 2. Seed Knowledge Base and Document Chunk
        kb = KnowledgeBase(
            name="企业系统架构规范库",
            description="包含网关、微服务连接池与超时治理技术白皮书",
            embedding_model=settings.EMBEDDING_MODEL_NAME,
        )
        session.add(kb)
        await session.flush()

        doc = Document(
            knowledge_base_id=kb.id,
            file_name="gateway_architecture_v2.md",
            file_type="md",
            file_path="/data/docs/gateway_architecture_v2.md",
            status="COMPLETED",
        )
        session.add(doc)
        await session.flush()

        chunk_content = (
            "【网关连接池与超时规范】\n"
            "网关核心服务对外连接池最大连接数（max_connections）配置为 500，"
            "最小空闲连接数（min_idle）配置为 50，获取连接超时阈值为 1000ms。\n"
            "全局接口调用超时时间（timeout）默认设定为 5000ms，在流量高峰期具备自适应熔断机制。"
        )
        chunk = DocumentChunk(
            document_id=doc.id,
            knowledge_base_id=kb.id,
            content=chunk_content,
            embedding=[0.05] * settings.EMBEDDING_DIMENSIONS,
            chunk_index=0,
            page_number=1,
        )
        session.add(chunk)
        await session.commit()
        print(f"\n[1. 初始化环境] 创建知识库 '{kb.name}' (ID: {kb.id[:8]}...) 包含网关规范切块。")

        # 3. Setup Tool Registry & Tools
        registry = ToolRegistry()
        rag_tool = SearchKnowledgeBaseTool(db=session)
        registry.register(rag_tool)
        print(f"[2. 工具注册中心] 成功注册工具: {registry.list_tool_names()}")

        # 4. Setup TestDouble LLM Client with autonomous multi-turn responses
        llm = TestDoubleLLMClient()
        # Turn 1: Autonomous decision to invoke search_knowledge_base
        t1_response = AIMessage(
            content="",
            tool_calls=[{
                "name": "search_knowledge_base",
                "args": {
                    "knowledge_base_id": kb.id,
                    "query": "网关连接池配置与接口超时时间",
                    "top_k": 4,
                },
                "id": "call_demo_gateway_01",
                "type": "tool_call",
            }],
            response_metadata={"finish_reason": "tool_calls"},
        )
        # Turn 2: Final grounded answer referencing [Ref-1]
        t2_response = AIMessage(
            content=(
                "根据知识库规范资料 [Ref-1]，为您分析网关配置如下：\n\n"
                "1. **连接池配置**：核心服务最大连接数设为 500，最小空闲连接数设为 50，获取连接超时阈值为 1000ms，配置合理。\n"
                "2. **超时时间评估**：接口全局调用超时时间默认设置为 5000ms，且在峰值流量下具备自适应熔断保护机制，阈值设计符合高可用规范。"
            ),
            response_metadata={"finish_reason": "stop"},
        )
        llm.set_responses([t1_response, t2_response])

        # 5. Initialize LangGraph Agent Runtime
        runtime = LangGraphAgentRuntime(
            tool_registry=registry,
            llm_client=llm,
            db_session=session,
            max_steps=5,
            max_tool_calls=10,
        )

        user_query = "请查询知识库中的网关连接池配置，并检查接口超时时间是否合理。"
        print(f"\n[3. 用户任务提交] \"{user_query}\"")

        # 6. Execute Runtime
        print("\n[4. LangGraph Runtime 执行流]")
        final_state = await runtime.ainvoke(
            query=user_query,
            knowledge_base_id=kb.id,
            enabled_tools=["search_knowledge_base"],
        )

        # 7. Print Execution Trace Timeline
        for idx, event in enumerate(final_state.get("trace_events", []), start=1):
            evt_type = event.get("event")
            if evt_type == "run_started":
                print(f"  Step {idx} [Run Started] ID: {event['run_id'][:8]}... | 限制步数: 5")
            elif evt_type == "agent_tool_decision":
                print(f"  Step {idx} [Agent 决策] 自主决定调用工具: {event['tools']} (调用次数: {event['tool_calls_count']})")
            elif evt_type == "tool_executed":
                print(f"  Step {idx} [Tool 执行] 工具 '{event['tool_name']}' 执行成功 (耗时: {event['duration_ms']}ms)")
            elif evt_type == "agent_response_generated":
                print(f"  Step {idx} [Agent 生成] 最终回答已就绪 (耗时: {event['duration_ms']}ms)")
            elif evt_type == "citations_verified":
                print(f"  Step {idx} [Citation 校验] 成功核验真实引用数量: {event['citations_count']}, 引文索引: {event['cited_ref_indices']}")

        # 8. Print Results
        print("\n" + "=" * 80)
        print("  【最终生成回答】")
        print("=" * 80)
        print(final_state.get("final_answer"))

        print("\n" + "=" * 80)
        print("  【经过真实核验的来源引用 (Citations)】")
        print("=" * 80)
        for c in final_state.get("citations", []):
            print(f"  - [Ref-{c['citation_index']}] 文件: {c['document_name']} (页码: {c['page_number']})")
            print(f"    内容摘要: {c['content_snippet'][:90]}...")

        print("\n" + "=" * 80)
        print(f"  执行状态: {final_state.get('status')} | 步数: {final_state.get('execution_steps')} | 工具调用数: {final_state.get('tool_call_count')}")
        print("=" * 80)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_demo())
