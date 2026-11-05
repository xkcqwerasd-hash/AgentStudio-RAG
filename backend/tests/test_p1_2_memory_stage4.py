import asyncio
import json
import uuid
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.memory.extractor import MemoryExtractor, run_memory_extraction_task
from app.memory.manager import MemoryManager
from app.models.conversation import Conversation, Message
from app.models.memory import (
    Memory,
    MemoryExtractionLog,
    MemorySourceType,
    MemoryType,
    UserMemorySetting,
)


def parse_sse_events(raw_text: str) -> List[Dict[str, Any]]:
    """Parse raw SSE text into a list of {event: str, data: Any} dicts."""
    events = []
    blocks = raw_text.strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        event_type = "message"
        data_str = ""
        for line in lines:
            line = line.strip()
            if line.startswith("event:"):
                event_type = line.replace("event:", "", 1).strip()
            elif line.startswith("data:"):
                d = line.replace("data:", "", 1).strip()
                data_str = data_str + "\n" + d if data_str else d
        if data_str:
            try:
                data = json.loads(data_str)
            except Exception:
                data = data_str
            events.append({"event": event_type, "data": data})
    return events


# ==============================================================================
# Scenario 1: 正常对话自动提取 (场景 A)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_1_normal_conversation_auto_extraction(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID
    msg_id = str(uuid.uuid4())

    conv = Conversation(title="测试自动记忆会话", mode="direct")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    conv_id = conv.id

    query = "我正在开发 AgentStudio-RAG，后端使用 FastAPI，Agent 使用 LangGraph。"
    answer = "收到！AgentStudio-RAG 的架构非常现代化，FastAPI 提供高效的全异步接口，LangGraph 则负责灵活的多步智能体编排。"

    memories = await extractor.extract_from_conversation(
        user_id=user_id,
        query=query,
        answer=answer,
        conversation_id=conv_id,
        message_id=msg_id,
        db=db_session,
    )

    assert len(memories) >= 1
    mem = memories[0]
    assert "AgentStudio-RAG" in mem.content
    assert mem.source_type == MemorySourceType.AUTO_EXTRACTED
    assert mem.owner_id == user_id
    assert mem.is_active is True

    # Verify idempotency log recorded
    stmt = select(MemoryExtractionLog).where(MemoryExtractionLog.message_id == msg_id)
    log_row = (await db_session.execute(stmt)).scalar_one_or_none()
    assert log_row is not None
    assert log_row.status == "EXTRACTED"
    assert log_row.extracted_count >= 1


# ==============================================================================
# Scenario 2: 普通问候不提取
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_2_greetings_not_extracted(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID

    greetings = ["你好！", "早上好", "在吗？", "谢谢你！", "再见"]
    for g in greetings:
        msg_id = str(uuid.uuid4())
        memories = await extractor.extract_from_conversation(
            user_id=user_id,
            query=g,
            answer="您好！请问有什么我可以协助您的？",
            conversation_id=None,
            message_id=msg_id,
            db=db_session,
        )
        assert len(memories) == 0

        # Log must be SKIPPED
        stmt = select(MemoryExtractionLog).where(MemoryExtractionLog.message_id == msg_id)
        log_row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert log_row is not None
        assert log_row.status == "SKIPPED"
        assert log_row.reason == "no_candidate_memory_triggers"


# ==============================================================================
# Scenario 3: 普通知识问题不提取 (场景 F)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_3_general_knowledge_not_extracted(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID
    msg_id = str(uuid.uuid4())

    query = "什么是 C++ 多态？"
    answer = "C++ 多态性是指同一个调用形式可以执行不同的具体实现..."

    memories = await extractor.extract_from_conversation(
        user_id=user_id,
        query=query,
        answer=answer,
        conversation_id=None,
        message_id=msg_id,
        db=db_session,
    )
    assert len(memories) == 0

    stmt = select(MemoryExtractionLog).where(MemoryExtractionLog.message_id == msg_id)
    log_row = (await db_session.execute(stmt)).scalar_one_or_none()
    assert log_row is not None
    assert log_row.status == "SKIPPED"


# ==============================================================================
# Scenario 4: 长期偏好正确提取 (profile)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_4_long_term_preference_extracted(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID
    msg_id = str(uuid.uuid4())

    query = "我偏好使用 Python 全异步编程风格，并要求所有接口具备统一错误处理中间件。"
    answer = "已记录您的偏好！后续编写接口代码时将默认采用 async/await 与统一中间件。"

    memories = await extractor.extract_from_conversation(
        user_id=user_id,
        query=query,
        answer=answer,
        conversation_id=None,
        message_id=msg_id,
        db=db_session,
    )
    assert len(memories) >= 1
    assert any(m.memory_type == MemoryType.USER_PROFILE for m in memories)


# ==============================================================================
# Scenario 5: 项目背景正确提取 (background)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_5_project_background_extracted(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID
    msg_id = str(uuid.uuid4())

    query = "我们项目核心业务是金融信贷风控评估系统，采用分布式微服务架构。"
    answer = "了解，金融信贷风控对于高可用、数据一致性和审计追踪有极高的要求。"

    memories = await extractor.extract_from_conversation(
        user_id=user_id,
        query=query,
        answer=answer,
        conversation_id=None,
        message_id=msg_id,
        db=db_session,
    )
    assert len(memories) >= 1
    assert any(m.memory_type == MemoryType.PROJECT_CONTEXT for m in memories)


# ==============================================================================
# Scenario 6: 敏感凭据拦截 (API Key, Passwords, Tokens)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_6_sensitive_credentials_intercepted(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID

    # 1. API key in query
    msg_id_1 = str(uuid.uuid4())
    memories_1 = await extractor.extract_from_conversation(
        user_id=user_id,
        query="我的 API Key 是 sk-proj-1234567890abcdef1234567890abcdef，请记住",
        answer="收到",
        conversation_id=None,
        message_id=msg_id_1,
        db=db_session,
    )
    assert len(memories_1) == 0

    # 2. Bearer token in query
    msg_id_2 = str(uuid.uuid4())
    memories_2 = await extractor.extract_from_conversation(
        user_id=user_id,
        query="系统鉴权 Token 是 Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThis",
        answer="收到",
        conversation_id=None,
        message_id=msg_id_2,
        db=db_session,
    )
    assert len(memories_2) == 0

    # 3. Password in query
    msg_id_3 = str(uuid.uuid4())
    memories_3 = await extractor.extract_from_conversation(
        user_id=user_id,
        query="请记住我们生产数据库密码 password: SuperSecretP@ssword123",
        answer="收到",
        conversation_id=None,
        message_id=msg_id_3,
        db=db_session,
    )
    assert len(memories_3) == 0


# ==============================================================================
# Scenario 7: 重复任务幂等保护
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_7_background_task_idempotency(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID
    fixed_msg_id = str(uuid.uuid4())

    query = "我正在开发基于 LangGraph 的企业级多智能体协同框架。"
    answer = "太棒了，LangGraph 在状态图与回溯机制上有很好的支持。"

    # First run -> extracts memory
    memories_1 = await extractor.extract_from_conversation(
        user_id=user_id,
        query=query,
        answer=answer,
        conversation_id=None,
        message_id=fixed_msg_id,
        db=db_session,
    )
    assert len(memories_1) >= 1

    # Second run with exact same message_id -> idempotent skip
    memories_2 = await extractor.extract_from_conversation(
        user_id=user_id,
        query=query,
        answer=answer,
        conversation_id=None,
        message_id=fixed_msg_id,
        db=db_session,
    )
    assert len(memories_2) == 0


# ==============================================================================
# Scenario 8: 完全重复记忆去重
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_8_exact_duplicate_deduplication(
    client: AsyncClient, db_session: AsyncSession
):
    manager = MemoryManager(db_session)
    user_id = settings.DEFAULT_LOCAL_USER_ID

    content = "用户偏好使用 FastAPI 进行全异步微服务架构设计"
    m1 = await manager.create_memory(user_id=user_id, content=content)
    init_access = m1.access_count

    # Try creating identical content verbatim
    m2 = await manager.create_memory(user_id=user_id, content=content)
    assert m1.id == m2.id
    assert m2.access_count == init_access + 1

    # Total in DB is still 1
    stmt = select(Memory).where(Memory.owner_id == user_id)
    items = (await db_session.execute(stmt)).scalars().all()
    assert len(items) == 1


# ==============================================================================
# Scenario 9: 用户明确修正记忆 (场景 C)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_9_explicit_memory_correction(
    client: AsyncClient, db_session: AsyncSession
):
    extractor = MemoryExtractor()
    user_id = settings.DEFAULT_LOCAL_USER_ID

    # 1. User sets initial tech stack
    msg_1 = str(uuid.uuid4())
    mem1 = await extractor.extract_from_conversation(
        user_id=user_id,
        query="我正在开发项目，后端使用 FastAPI 架构",
        answer="已记录",
        conversation_id=None,
        message_id=msg_1,
        db=db_session,
    )
    assert len(mem1) >= 1
    orig_id = mem1[0].id

    # 2. User explicitly updates/corrects the previous stack
    msg_2 = str(uuid.uuid4())
    corrected_query = "我现在已经不使用原来的技术方案了，把原来的架构换了，项目后端全面改成 Go Gin 架构"
    mem2 = await extractor.extract_from_conversation(
        user_id=user_id,
        query=corrected_query,
        answer="明白，已将后端技术栈更新为 Go Gin",
        conversation_id=None,
        message_id=msg_2,
        db=db_session,
    )
    assert len(mem2) >= 1
    # Check that original memory was superseded/updated
    updated_mem = await db_session.get(Memory, orig_id)
    assert "Go Gin" in updated_mem.content or "改用" in updated_mem.content


# ==============================================================================
# Scenario 10: 相似但不同的记忆安全并存
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_10_similar_memories_coexist(
    client: AsyncClient, db_session: AsyncSession
):
    manager = MemoryManager(db_session)
    user_id = settings.DEFAULT_LOCAL_USER_ID

    # Two distinct facets of the same project
    m1 = await manager.create_memory(
        user_id=user_id,
        content="用户开发 AgentStudio-RAG，后端采用 FastAPI 和 SQLAlchemy 异步引擎",
        memory_type=MemoryType.PROJECT_CONTEXT,
    )
    m2 = await manager.create_memory(
        user_id=user_id,
        content="用户开发 AgentStudio-RAG，前端采用 React 18 与 Ant Design 5 组件库",
        memory_type=MemoryType.PROJECT_CONTEXT,
    )

    assert m1.id != m2.id

    # Both co-exist safely in DB
    stmt = select(Memory).where(Memory.owner_id == user_id)
    items = (await db_session.execute(stmt)).scalars().all()
    assert len(items) == 2


# ==============================================================================
# Scenario 11: BackgroundTask 异常不影响 SSE 正常流式响应
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_11_background_task_exception_does_not_break_sse(
    client: AsyncClient, db_session: AsyncSession
):
    with patch(
        "app.memory.extractor.MemoryExtractor.extract_from_conversation",
        side_effect=RuntimeError("Simulated LLM crash inside background task"),
    ):
        req_payload = {
            "query": "介绍一下微服务核心原则",
            "mode": "direct",
        }
        res = await client.post("/api/v1/chat/completions/stream", json=req_payload)
        assert res.status_code == 200

        events = parse_sse_events(res.text)
        event_names = [e.get("event") for e in events]
        assert "start" in event_names
        assert "token" in event_names
        assert "done" in event_names


# ==============================================================================
# Scenario 12: 用户关闭自动记忆 (场景 D)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_12_user_disables_auto_memory(
    client: AsyncClient, db_session: AsyncSession
):
    user_id = settings.DEFAULT_LOCAL_USER_ID
    manager = MemoryManager(db_session)

    # 1. Existing memory created beforehand
    await manager.create_memory(
        user_id=user_id,
        content="用户历史偏好使用 Docker 进行容器化交付",
    )

    # 2. Disable auto-memory via API
    put_res = await client.put(
        "/api/v1/memories/settings",
        json={"auto_memory_enabled": False},
    )
    assert put_res.status_code == 200
    assert put_res.json()["auto_memory_enabled"] is False

    # 3. New conversation turn with candidate memory text
    extractor = MemoryExtractor()
    msg_id = str(uuid.uuid4())
    memories = await extractor.extract_from_conversation(
        user_id=user_id,
        query="我正在研发一个全新的物联网平台架构",
        answer="已收到",
        conversation_id=None,
        message_id=msg_id,
        db=db_session,
    )
    assert len(memories) == 0

    # Log shows DISABLED
    stmt = select(MemoryExtractionLog).where(MemoryExtractionLog.message_id == msg_id)
    log_row = (await db_session.execute(stmt)).scalar_one_or_none()
    assert log_row is not None
    assert log_row.status == "DISABLED"

    # 4. Old memory remains intact and searchable
    existing = await manager.list_memories(user_id=user_id)
    assert existing[1] == 1

    # Cleanup: re-enable auto-memory
    await manager.update_user_setting(user_id=user_id, auto_memory_enabled=True)


# ==============================================================================
# Scenario 13: 不同用户记忆隔离
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_13_user_isolation(
    client: AsyncClient, db_session: AsyncSession
):
    manager = MemoryManager(db_session)
    user_alice = "usr_alice_stage4"
    user_bob = "usr_bob_stage4"

    await manager.create_memory(
        user_id=user_alice,
        content="Alice 的专属微服务框架使用 gRPC 和 Protocol Buffers",
    )

    # Bob's query sees 0 items
    bob_items, bob_total = await manager.list_memories(user_id=user_bob)
    assert bob_total == 0

    # Bob's stats show 0
    bob_stats = await manager.get_memory_stats(user_id=user_bob)
    assert bob_stats["total_memories"] == 0


# ==============================================================================
# Scenario 14: 删除后不再召回 (场景 E)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_14_deleted_memory_not_recalled(
    client: AsyncClient, db_session: AsyncSession
):
    manager = MemoryManager(db_session)
    user_id = settings.DEFAULT_LOCAL_USER_ID

    mem = await manager.create_memory(
        user_id=user_id,
        content="用户计划在下季度将主服务器迁移至 Kubernetes 集群",
    )

    # Delete memory
    del_res = await client.delete(f"/api/v1/memories/{mem.id}")
    assert del_res.status_code == 200

    # Vector recall returns 0
    results = await manager.retrieve_relevant_memories(
        user_id=user_id,
        query="服务器迁移 Kubernetes",
    )
    assert len(results) == 0


# ==============================================================================
# Scenario 15: 停用后不再召回
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_15_inactive_memory_not_recalled(
    client: AsyncClient, db_session: AsyncSession
):
    manager = MemoryManager(db_session)
    user_id = settings.DEFAULT_LOCAL_USER_ID

    mem = await manager.create_memory(
        user_id=user_id,
        content="用户偏好使用 RabbitMQ 作为消息中间件",
    )

    # Toggle to inactive
    patch_res = await client.patch(f"/api/v1/memories/{mem.id}/status?is_active=false")
    assert patch_res.status_code == 200
    assert patch_res.json()["is_active"] is False

    # Vector recall returns 0
    results = await manager.retrieve_relevant_memories(
        user_id=user_id,
        query="消息中间件 RabbitMQ",
    )
    assert len(results) == 0


# ==============================================================================
# Scenario 16: 历史 Conversation 正常恢复
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_16_conversation_restoration_normal(
    client: AsyncClient, db_session: AsyncSession
):
    conv = Conversation(title="Stage 4 会话测试", mode="direct")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    req_payload = {
        "conversation_id": conv.id,
        "query": "介绍一下微服务架构设计基本原则",
        "mode": "direct",
    }
    res = await client.post("/api/v1/chat/completions", json=req_payload)
    assert res.status_code == 200

    conv_res = await client.get(f"/api/v1/conversations/{conv.id}")
    assert conv_res.status_code == 200
    assert len(conv_res.json()["messages"]) == 2


# ==============================================================================
# Scenario 17: 三大执行引擎继续正常使用记忆 (场景 B)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_17_three_engines_continue_using_memories(
    client: AsyncClient, db_session: AsyncSession
):
    manager = MemoryManager(db_session)
    user_id = settings.DEFAULT_LOCAL_USER_ID

    await manager.create_memory(
        user_id=user_id,
        content="用户偏好使用 FastAPI 微服务架构设计并要求包含 Trace-ID",
    )

    # Direct LLM in new conversation
    req_payload = {
        "query": "请问 FastAPI 微服务架构设计有哪些 Trace-ID 规范要求？",
        "mode": "direct",
    }
    res = await client.post("/api/v1/chat/completions", json=req_payload)
    assert res.status_code == 200
    exec_info = res.json()["execution_info"]
    assert exec_info["memory_used"] is True
    assert exec_info["injected_memory_count"] >= 1


# ==============================================================================
# Scenario 18: 记忆抽屉 CRUD 全链路 API 正常
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_18_memory_drawer_crud_full_api(
    client: AsyncClient, db_session: AsyncSession
):
    # 1. Create memory
    create_payload = {
        "content": "用户偏好使用 TypeScript 和 React 18 进行前端开发",
        "memory_type": "USER_PROFILE",
        "tags": ["frontend", "react"],
    }
    res_c = await client.post("/api/v1/memories", json=create_payload)
    assert res_c.status_code == 201
    mem_data = res_c.json()
    mem_id = mem_data["id"]

    # 2. Get stats
    res_s = await client.get("/api/v1/memories/stats")
    assert res_s.status_code == 200
    stats = res_s.json()
    assert stats["total_memories"] >= 1
    assert stats["profile_count"] >= 1

    # 3. List with type filter
    res_l = await client.get("/api/v1/memories?memory_type=USER_PROFILE")
    assert res_l.status_code == 200
    assert len(res_l.json()["items"]) >= 1

    # 4. Update memory
    res_u = await client.put(
        f"/api/v1/memories/{mem_id}",
        json={"content": "用户偏好使用 TypeScript、React 18 及 Ant Design 5 进行前端开发"},
    )
    assert res_u.status_code == 200
    assert "Ant Design 5" in res_u.json()["content"]

    # 5. Toggle status
    res_t = await client.patch(f"/api/v1/memories/{mem_id}/status?is_active=false")
    assert res_t.status_code == 200
    assert res_t.json()["is_active"] is False

    # 6. Single delete
    res_d = await client.delete(f"/api/v1/memories/{mem_id}")
    assert res_d.status_code == 200
    assert res_d.json()["deleted"] is True

    # 7. Clear all
    res_clear = await client.delete("/api/v1/memories")
    assert res_clear.status_code == 200
    assert "deleted_count" in res_clear.json()
