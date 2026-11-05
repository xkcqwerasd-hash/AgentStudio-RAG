import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import MemoryNotFoundError
from app.llm.embeddings import get_embedding_client
from app.memory.manager import MemoryManager
from app.models.conversation import Conversation
from app.models.memory import Memory, MemorySourceType, MemoryType


@pytest.fixture(autouse=True)
def enable_legacy_stage2_test_user_override(monkeypatch):
    """Enable X-User-Id override specifically for historical Stage 2 memory tests."""
    monkeypatch.setattr(settings, "ALLOW_TEST_USER_OVERRIDE", True)


@pytest.mark.asyncio
async def test_scenario_1_local_mode_creation_and_cross_conversation_recall(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 1: Local mode memory creation and cross-conversation recall.
    User creates memory in Conversation A, then recalls it when querying in Conversation B.
    """
    # 1. Create two conversations
    conv_a = Conversation(title="会话 A: 需求讨论", mode="workflow")
    conv_b = Conversation(title="会话 B: 架构设计", mode="workflow")
    db_session.add_all([conv_a, conv_b])
    await db_session.commit()
    await db_session.refresh(conv_a)
    await db_session.refresh(conv_b)

    # 2. In Local mode, create memory via API tied to Conversation A
    create_payload = {
        "content": "用户偏好使用 Python 3.12 并强制要求类型注解",
        "memory_type": "USER_PROFILE",
        "tags": ["python", "type-hints"],
        "source_conversation_id": conv_a.id,
        "confidence": 1.0,
    }
    res = await client.post("/api/v1/memories", json=create_payload)
    assert res.status_code == 201
    mem_data = res.json()
    assert mem_data["owner_id"] == settings.DEFAULT_LOCAL_USER_ID
    assert mem_data["source_conversation_id"] == conv_a.id
    assert mem_data["memory_type"] == "USER_PROFILE"
    mem_id = mem_data["id"]

    # 3. Simulate querying from Conversation B
    query_payload = {
        "query": "请问 Python 代码有什么规范要求？",
        "top_k": 3,
        "threshold": 0.3,
    }
    query_res = await client.post("/api/v1/memories/query", json=query_payload)
    assert query_res.status_code == 200
    results = query_res.json()
    assert len(results) > 0
    assert results[0]["memory"]["id"] == mem_id
    assert "Python 3.12" in results[0]["memory"]["content"]
    assert results[0]["similarity_score"] > 0.3


@pytest.mark.asyncio
async def test_scenario_2_user_isolation(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario 2: Memory isolation between different users.
    User A cannot retrieve User B's memories, and vice versa.
    """
    user_a = "usr_alice_123"
    user_b = "usr_bob_456"

    # User A creates a memory
    res_a = await client.post(
        "/api/v1/memories",
        json={
            "content": "Alice 专有微服务架构采用 Kafka 和 Flink",
            "memory_type": "PROJECT_CONTEXT",
            "tags": ["alice", "kafka"],
        },
        headers={"X-User-Id": user_a},
    )
    assert res_a.status_code == 201
    mem_a_id = res_a.json()["id"]

    # User B creates a memory
    res_b = await client.post(
        "/api/v1/memories",
        json={
            "content": "Bob 专有系统采用 RabbitMQ 和 Celery 异步队列",
            "memory_type": "PROJECT_CONTEXT",
            "tags": ["bob", "rabbitmq"],
        },
        headers={"X-User-Id": user_b},
    )
    assert res_b.status_code == 201
    mem_b_id = res_b.json()["id"]

    # User A searches for Kafka architecture -> only Alice's memory returned
    search_a = await client.post(
        "/api/v1/memories/query",
        json={"query": "Alice 的微服务架构与 Kafka", "top_k": 5, "threshold": 0.3},
        headers={"X-User-Id": user_a},
    )
    assert search_a.status_code == 200
    results_a = search_a.json()
    recalled_ids_a = [item["memory"]["id"] for item in results_a]
    assert mem_a_id in recalled_ids_a
    assert mem_b_id not in recalled_ids_a

    # User B lists memories -> only Bob's memory returned
    list_b = await client.get("/api/v1/memories", headers={"X-User-Id": user_b})
    assert list_b.status_code == 200
    list_b_data = list_b.json()
    assert list_b_data["total"] == 1
    assert list_b_data["items"][0]["id"] == mem_b_id
    assert list_b_data["items"][0]["owner_id"] == user_b


@pytest.mark.asyncio
async def test_scenario_3_unauthorized_access_interception(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 3: Interception of unauthorized queries, modifications, and deletions.
    User B attempting to view, edit, or delete User A's memory returns 404 (IDOR defense).
    """
    user_a = "usr_alice_sec"
    user_b = "usr_mallory_attacker"

    # User A creates a memory
    res_a = await client.post(
        "/api/v1/memories",
        json={
            "content": "机密业务规则：内部折扣码为 VIP888",
            "memory_type": "IMPORTANT_FACT",
        },
        headers={"X-User-Id": user_a},
    )
    assert res_a.status_code == 201
    mem_id = res_a.json()["id"]

    # User B attempts to view User A's memory -> 404
    view_res = await client.get(f"/api/v1/memories/{mem_id}", headers={"X-User-Id": user_b})
    assert view_res.status_code == 404

    # User B attempts to update User A's memory -> 404
    update_res = await client.put(
        f"/api/v1/memories/{mem_id}",
        json={"content": "被攻击者篡改的规则"},
        headers={"X-User-Id": user_b},
    )
    assert update_res.status_code == 404

    # User B attempts to toggle status -> 404
    toggle_res = await client.patch(
        f"/api/v1/memories/{mem_id}/status?is_active=false",
        headers={"X-User-Id": user_b},
    )
    assert toggle_res.status_code == 404

    # User B attempts to delete User A's memory -> 404
    del_res = await client.delete(f"/api/v1/memories/{mem_id}", headers={"X-User-Id": user_b})
    assert del_res.status_code == 404

    # Verify memory is still intact for User A
    verify_res = await client.get(f"/api/v1/memories/{mem_id}", headers={"X-User-Id": user_a})
    assert verify_res.status_code == 200
    assert verify_res.json()["content"] == "机密业务规则：内部折扣码为 VIP888"


@pytest.mark.asyncio
async def test_scenario_4_memory_update_and_exact_deduplication(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 4: Memory update and exact-fact deduplication.
    Verbatim identical memories are deduplicated without creating duplicate rows;
    explicit updates modify content and vector representation.
    """
    user_id = "usr_dedup_test"

    # 1. First creation
    res1 = await client.post(
        "/api/v1/memories",
        json={"content": "项目统一采用 16~20px 大圆角设计风格", "tags": ["ui"]},
        headers={"X-User-Id": user_id},
    )
    assert res1.status_code == 201
    mem1 = res1.json()
    assert mem1["access_count"] == 0

    # 2. Second creation with verbatim identical content -> Deduplicated!
    res2 = await client.post(
        "/api/v1/memories",
        json={"content": "  项目统一采用 16~20px 大圆角设计风格  ", "tags": ["design"]},
        headers={"X-User-Id": user_id},
    )
    assert res2.status_code == 201
    mem2 = res2.json()
    assert mem2["id"] == mem1["id"]
    assert mem2["access_count"] == 1
    assert "design" in mem2["tags"]

    # Verify only 1 record exists in list
    list_res = await client.get("/api/v1/memories", headers={"X-User-Id": user_id})
    assert list_res.json()["total"] == 1

    # 3. Explicit update
    update_res = await client.put(
        f"/api/v1/memories/{mem1['id']}",
        json={"content": "项目统一采用 16~20px 大圆角与浅灰边框设计风格"},
        headers={"X-User-Id": user_id},
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["content"] == "项目统一采用 16~20px 大圆角与浅灰边框设计风格"


@pytest.mark.asyncio
async def test_scenario_5_deactivated_memory_not_recalled(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 5: Deactivated memories (is_active == False) are excluded from recall results.
    """
    user_id = "usr_active_toggle_test"

    # 1. Create memory
    res = await client.post(
        "/api/v1/memories",
        json={"content": "网关三态熔断默认超时阈值配置为 5000ms"},
        headers={"X-User-Id": user_id},
    )
    assert res.status_code == 201
    mem_id = res.json()["id"]

    # 2. Verify it is recallable when active
    recall_active = await client.post(
        "/api/v1/memories/query",
        json={"query": "网关熔断超时时间", "threshold": 0.2},
        headers={"X-User-Id": user_id},
    )
    assert recall_active.status_code == 200
    assert len(recall_active.json()) == 1

    # 3. Deactivate memory
    deactivate_res = await client.patch(
        f"/api/v1/memories/{mem_id}/status?is_active=false",
        headers={"X-User-Id": user_id},
    )
    assert deactivate_res.status_code == 200
    assert deactivate_res.json()["is_active"] is False

    # 4. Verify it is NOT recalled when deactivated
    recall_inactive = await client.post(
        "/api/v1/memories/query",
        json={"query": "网关熔断超时时间", "threshold": 0.2},
        headers={"X-User-Id": user_id},
    )
    assert recall_inactive.status_code == 200
    assert len(recall_inactive.json()) == 0


@pytest.mark.asyncio
async def test_scenario_6_deleted_memory_not_recalled(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 6: Deleted memories are purged and never enter recall results again.
    """
    user_id = "usr_delete_test"

    # Create memory
    res = await client.post(
        "/api/v1/memories",
        json={"content": "旧系统已经废弃使用 SOAP 协议"},
        headers={"X-User-Id": user_id},
    )
    assert res.status_code == 201
    mem_id = res.json()["id"]

    # Delete memory
    del_res = await client.delete(f"/api/v1/memories/{mem_id}", headers={"X-User-Id": user_id})
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True

    # Detail query returns 404
    get_res = await client.get(f"/api/v1/memories/{mem_id}", headers={"X-User-Id": user_id})
    assert get_res.status_code == 404

    # Vector query returns empty
    query_res = await client.post(
        "/api/v1/memories/query",
        json={"query": "SOAP 协议", "threshold": 0.1},
        headers={"X-User-Id": user_id},
    )
    assert query_res.status_code == 200
    assert len(query_res.json()) == 0


@pytest.mark.asyncio
async def test_scenario_7_empty_memories_handled_gracefully(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 7: When user has no memories, all operations return graceful empty structures.
    """
    user_id = "usr_empty_brand_new"

    # List returns empty page
    list_res = await client.get("/api/v1/memories", headers={"X-User-Id": user_id})
    assert list_res.status_code == 200
    data = list_res.json()
    assert data["items"] == []
    assert data["total"] == 0

    # Query returns empty list
    query_res = await client.post(
        "/api/v1/memories/query",
        json={"query": "任意问题", "threshold": 0.1},
        headers={"X-User-Id": user_id},
    )
    assert query_res.status_code == 200
    assert query_res.json() == []

    # Clear returns 0 deleted
    clear_res = await client.delete("/api/v1/memories", headers={"X-User-Id": user_id})
    assert clear_res.status_code == 200
    assert clear_res.json()["deleted_count"] == 0


@pytest.mark.asyncio
async def test_scenario_8_embedding_failure_graceful_degradation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """
    Scenario 8: Graceful degradation when Embedding service fails.
    Querying gracefully logs warning and returns empty results without crashing into 500 error.
    """
    user_id = "usr_embed_fault_test"
    manager = MemoryManager(db_session)

    # 1. Create a memory normally first
    mem = await manager.create_memory(user_id=user_id, content="降级测试记忆")
    assert mem.id is not None

    # 2. Mock embedding_client.embed_query to raise an exception
    class FailingEmbeddingClient:
        async def embed_query(self, text: str):
            raise RuntimeError("Simulated remote embedding API 503 gateway timeout")

    faulty_manager = MemoryManager(db_session, embedding_client=FailingEmbeddingClient())

    # 3. Querying with faulty embedding client returns [] without raising an exception
    recalled = await faulty_manager.retrieve_relevant_memories(
        user_id=user_id,
        query="任意查询词",
    )
    assert recalled == []

    # 4. Creating memory with faulty embedding client still saves content safely with embedding=None
    created_degraded = await faulty_manager.create_memory(
        user_id=user_id,
        content="在 Embedding 宕机时保存的无向量记忆",
    )
    assert created_degraded.id is not None
    assert created_degraded.embedding is None
    assert created_degraded.content == "在 Embedding 宕机时保存的无向量记忆"


@pytest.mark.asyncio
async def test_scenario_9_conversation_source_validation(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 9: Validate conversation source existence.
    Passing a nonexistent source_conversation_id raises 404 ConversationNotFoundError.
    """
    res = await client.post(
        "/api/v1/memories",
        json={
            "content": "测试无效来源会话",
            "source_conversation_id": "nonexistent-conv-uuid-99999",
        },
    )
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()
