import asyncio
import io
import time
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.knowledge_search import SearchKnowledgeBaseTool
from app.core.auth import generate_server_token
from app.core.config import settings
from app.core.exceptions import KnowledgeBaseNotFoundError
from app.core.password import hash_password
from app.models.agent import AgentDefinition, AgentRun
from app.models.conversation import Conversation, ConversationAttachment
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.models.model_provider import ModelConfig, ModelProvider
from app.models.user import User, UserRole


@pytest.fixture(autouse=True)
def optimize_password_iterations_for_tests(monkeypatch):
    """Speed up password hashing in tests."""
    monkeypatch.setattr(settings, "PASSWORD_HASH_ITERATIONS", 10_000)


async def _create_test_user(
    db: AsyncSession,
    username: str,
    role: str = UserRole.USER,
) -> tuple[User, str]:
    """Helper to create a user in DB and generate a valid Server token."""
    user = User(
        username=username,
        email=f"{username}@test.com",
        password_hash=hash_password("Password123!"),
        role=role,
        is_active=True,
        token_version=1,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = generate_server_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        token_version=user.token_version,
    )
    return user, token


# ==============================================================================
# Scenario 1: Conversation Isolation - Tenant B cannot read Tenant A's conversation
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_1_conversation_isolation_b_cannot_read_a(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s1")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s1")

    # A creates conversation
    res_a = await client.post(
        "/api/v1/conversations",
        json={"title": "A's Private Chat", "mode": "direct"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a.status_code == 201
    conv_a_id = res_a.json()["id"]

    # B attempts to read A's conversation
    res_b_get = await client.get(
        f"/api/v1/conversations/{conv_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_get.status_code == 404  # Uniform 404 IDOR defense

    # B attempts to read A's messages
    res_b_msgs = await client.get(
        f"/api/v1/conversations/{conv_a_id}/messages",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_msgs.status_code == 404

    # B lists conversations - does not contain A's conversation
    res_b_list = await client.get(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_list.status_code == 200
    b_conv_ids = [c["id"] for c in res_b_list.json()]
    assert conv_a_id not in b_conv_ids


# ==============================================================================
# Scenario 2: Conversation Isolation - Tenant B cannot delete Tenant A's conversation
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_2_conversation_isolation_b_cannot_delete_a(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s2")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s2")

    # A creates conversation
    res_a = await client.post(
        "/api/v1/conversations",
        json={"title": "A's Persistent Chat", "mode": "direct"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a.status_code == 201
    conv_a_id = res_a.json()["id"]

    # B attempts to delete A's conversation
    res_b_del = await client.delete(
        f"/api/v1/conversations/{conv_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_del.status_code == 404

    # Verify conversation still exists for A
    res_a_check = await client.get(
        f"/api/v1/conversations/{conv_a_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a_check.status_code == 200


# ==============================================================================
# Scenario 3: KnowledgeBase Isolation - Tenant B cannot read, list or delete A's KB
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_3_kb_isolation_b_cannot_access_a(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s3")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s3")

    # A creates KB
    res_a = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "A's Confidential KB", "description": "Strict isolation"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a.status_code == 201
    kb_a_id = res_a.json()["id"]

    # B lists KBs - should not see A's KB
    res_b_list = await client.get(
        "/api/v1/knowledge-bases",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_list.status_code == 200
    b_kb_ids = [k["id"] for k in res_b_list.json()]
    assert kb_a_id not in b_kb_ids

    # B gets A's KB directly
    res_b_get = await client.get(
        f"/api/v1/knowledge-bases/{kb_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_get.status_code == 404

    # B attempts to delete A's KB
    res_b_del = await client.delete(
        f"/api/v1/knowledge-bases/{kb_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_del.status_code == 404


# ==============================================================================
# Scenario 4 & 5: Document and Chunk Isolation
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_4_and_5_document_and_chunk_isolation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s4")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s4")

    # A creates KB
    res_a_kb = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "A's Doc KB"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    kb_a_id = res_a_kb.json()["id"]

    # A uploads document
    file_bytes = b"# Secret Document for Tenant A\n\nThis is secret company data."
    files = {"file": ("secret_a.md", io.BytesIO(file_bytes), "text/markdown")}
    res_upload = await client.post(
        f"/api/v1/knowledge-bases/{kb_a_id}/documents/upload",
        files=files,
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_upload.status_code == 200
    doc_a_id = res_upload.json()["id"]

    # B cannot list documents in A's KB
    res_b_docs = await client.get(
        f"/api/v1/knowledge-bases/{kb_a_id}/documents",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_docs.status_code == 404

    # B cannot read A's document detail
    res_b_doc = await client.get(
        f"/api/v1/documents/{doc_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_doc.status_code == 404

    # Scenario 5: B cannot read A's document chunks
    res_b_chunks = await client.get(
        f"/api/v1/documents/{doc_a_id}/chunks",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_chunks.status_code == 404

    # B cannot delete A's document
    res_b_del_doc = await client.delete(
        f"/api/v1/documents/{doc_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_del_doc.status_code == 404


# ==============================================================================
# Scenario 6 & 7: Conversation Attachment Isolation & Partitioned Physical Storage
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_6_and_7_attachment_isolation_and_partition(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s6")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s6")

    # A creates conversation
    res_a_conv = await client.post(
        "/api/v1/conversations",
        json={"title": "A's Attachment Chat", "mode": "direct"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a_conv.status_code == 201
    conv_a_id = res_a_conv.json()["id"]

    # A uploads attachment
    att_bytes = b"Important technical attachment for conversation A."
    files = [("files", ("project_spec.txt", io.BytesIO(att_bytes), "text/plain"))]
    res_upload = await client.post(
        f"/api/v1/conversations/{conv_a_id}/attachments",
        files=files,
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_upload.status_code == 201
    att_a_id = res_upload.json()[0]["id"]

    # Verify physical storage path contains user_a.id partition
    expected_partition_dir = settings.ATTACHMENTS_DIR / user_a.id / conv_a_id / att_a_id
    assert expected_partition_dir.exists()
    saved_files = list(expected_partition_dir.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].suffix == ".txt"

    # B cannot list attachments of A's conversation
    res_b_list = await client.get(
        f"/api/v1/conversations/{conv_a_id}/attachments",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_list.status_code == 404

    # B cannot get attachment detail
    res_b_get = await client.get(
        f"/api/v1/conversations/{conv_a_id}/attachments/{att_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_get.status_code == 404

    # B cannot delete attachment
    res_b_del = await client.delete(
        f"/api/v1/conversations/{conv_a_id}/attachments/{att_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_del.status_code == 404
    assert expected_partition_dir.exists() and len(list(expected_partition_dir.iterdir())) == 1  # Not deleted

    # A deletes attachment
    res_a_del = await client.delete(
        f"/api/v1/conversations/{conv_a_id}/attachments/{att_a_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a_del.status_code == 204
    assert not expected_partition_dir.exists() or len(list(expected_partition_dir.iterdir())) == 0  # Cleanly removed


# ==============================================================================
# Scenario 8: Model Provider Isolation - B cannot see or use A's private provider
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_8_private_model_isolation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s8")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s8")

    # A creates private model provider
    res_prov = await client.post(
        "/api/v1/models/providers",
        json={
            "name": "A's Private OpenAI",
            "provider_type": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-secret-key-of-a",
            "models": [
                {
                    "model_name": "gpt-4o-tenant-a",
                    "display_name": "GPT-4o (A's Key)",
                    "model_type": "chat",
                }
            ],
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_prov.status_code == 201
    prov_a_id = res_prov.json()["id"]
    model_a_id = res_prov.json()["models"][0]["id"]

    # B lists providers - must not see A's provider
    res_b_providers = await client.get(
        "/api/v1/models/providers",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_providers.status_code == 200
    b_prov_ids = [p["id"] for p in res_b_providers.json()]
    assert prov_a_id not in b_prov_ids

    # B lists active models - must not see A's model
    res_b_active = await client.get(
        "/api/v1/models/active",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_active.status_code == 200
    b_model_ids = [m["id"] for m in res_b_active.json()]
    assert model_a_id not in b_model_ids

    # B tests connection with A's model_id - receives rejection
    res_b_test = await client.post(
        "/api/v1/models/test",
        json={"model_id": model_a_id},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_test.status_code == 200
    test_data = res_b_test.json()
    assert test_data["success"] is False
    assert "不存在" in test_data["error"]


# ==============================================================================
# Scenario 9: System Public Model Provider - Only Admin can mutate
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_9_system_public_model_provider_mutation_guard(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    admin_user, admin_token = await _create_test_user(db_session, "admin_user_s9", role=UserRole.ADMIN)
    normal_user, user_token = await _create_test_user(db_session, "normal_user_s9", role=UserRole.USER)

    # Admin creates system public provider
    res_prov = await client.post(
        "/api/v1/models/providers",
        json={
            "name": "System Global DeepSeek",
            "provider_type": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-system-public-key",
            "is_public": True,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_prov.status_code == 201
    pub_prov_id = res_prov.json()["id"]

    # Normal user can view public provider
    res_user_get = await client.get(
        f"/api/v1/models/providers/{pub_prov_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res_user_get.status_code == 200

    # Normal user attempts to update system provider - 403 Forbidden
    res_user_update = await client.put(
        f"/api/v1/models/providers/{pub_prov_id}",
        json={"name": "Hacked Provider Name"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res_user_update.status_code == 403

    # Normal user attempts to delete system provider - 403 Forbidden
    res_user_delete = await client.delete(
        f"/api/v1/models/providers/{pub_prov_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res_user_delete.status_code == 403

    # Admin can successfully update
    res_admin_update = await client.put(
        f"/api/v1/models/providers/{pub_prov_id}",
        json={"name": "System Global DeepSeek (Updated)"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_admin_update.status_code == 200


# ==============================================================================
# Scenario 10: AgentRun Isolation - Tenant B cannot see or query A's Agent runs
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_10_agent_run_isolation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s10")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s10")

    # Create Agent Definition
    agent = AgentDefinition(
        name="Shared Analytics Agent",
        description="Public helper",
        system_prompt="Analyze data",
        owner_id=None,  # Public agent
    )
    agent.set_tools(["search_knowledge_base"])
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)

    # Insert an AgentRun belonging to User A
    run_a = AgentRun(
        agent_definition_id=agent.id,
        owner_id=user_a.id,
        status="completed",
        input="User A secret task query",
        final_answer="Sensitive financial report",
    )
    db_session.add(run_a)
    await db_session.commit()
    await db_session.refresh(run_a)

    # User B lists runs for this agent - should be empty
    res_b_runs = await client.get(
        f"/api/v1/agents/{agent.id}/runs",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_runs.status_code == 200
    b_run_ids = [r["id"] for r in res_b_runs.json()]
    assert run_a.id not in b_run_ids

    # User B queries run_a detail directly - 404 Not Found
    res_b_detail = await client.get(
        f"/api/v1/agent-runs/{run_a.id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_detail.status_code == 404

    # User A queries run_a detail - 200 OK
    res_a_detail = await client.get(
        f"/api/v1/agent-runs/{run_a.id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a_detail.status_code == 200
    assert res_a_detail.json()["input"] == "User A secret task query"


# ==============================================================================
# Scenario 11: Agent Tool SearchKnowledgeBaseTool Cross-Tenant Query Guard
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_11_agent_search_tool_cross_tenant_block(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, _ = await _create_test_user(db_session, "tenant_a_s11")
    user_b, _ = await _create_test_user(db_session, "tenant_b_s11")

    # Create KB owned by User A
    kb_a = KnowledgeBase(
        name="User A Confidential Manual",
        owner_id=user_a.id,
        embedding_provider="mock",
        embedding_model="mock",
        embedding_dimensions=1536,
    )
    db_session.add(kb_a)
    await db_session.commit()
    await db_session.refresh(kb_a)

    # Tool executed in context of User B
    tool_for_b = SearchKnowledgeBaseTool(db=db_session, user_id=user_b.id)

    # 1. Direct _run raises KnowledgeBaseNotFoundError
    with pytest.raises(KnowledgeBaseNotFoundError):
        await tool_for_b._run(knowledge_base_id=kb_a.id, query="financial figures")

    # 2. Standard tool.run returns ToolResult with success=False and 404 error
    tool_res = await tool_for_b.run(knowledge_base_id=kb_a.id, query="financial figures")
    assert tool_res.success is False
    assert "not found" in tool_res.error.lower()


# ==============================================================================
# Scenario 12: Memory Multi-Tenant Isolation Remains Intact
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_12_memory_multi_tenant_isolation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s12")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s12")

    # A adds a memory
    res_add = await client.post(
        "/api/v1/memories",
        json={"content": "My private secret project name is Apollo-11", "category": "fact"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_add.status_code == 201
    mem_a_id = res_add.json()["id"]

    # B lists memories - does not see A's memory
    res_b_list = await client.get(
        "/api/v1/memories",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_list.status_code == 200
    b_mem_ids = [m["id"] for m in res_b_list.json()["items"]]
    assert mem_a_id not in b_mem_ids

    # B queries A's memory directly - 404
    res_b_get = await client.get(
        f"/api/v1/memories/{mem_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_get.status_code == 404

    # B attempts to delete A's memory - 404
    res_b_del = await client.delete(
        f"/api/v1/memories/{mem_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res_b_del.status_code == 404


# ==============================================================================
# Scenario 13: Local Mode Seamless Legacy Data Mapping
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_13_local_mode_default_user_mapping(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")

    # In local mode, no token is required
    res_create_conv = await client.post(
        "/api/v1/conversations",
        json={"title": "Local Seamless Chat", "mode": "direct"},
    )
    assert res_create_conv.status_code == 201
    conv_id = res_create_conv.json()["id"]

    # Read back conversation
    res_get_conv = await client.get(f"/api/v1/conversations/{conv_id}")
    assert res_get_conv.status_code == 200
    assert res_get_conv.json()["title"] == "Local Seamless Chat"

    # Verify conversation is tagged with usr_local_default in DB
    conv_obj = (
        await db_session.execute(select(Conversation).where(Conversation.id == conv_id))
    ).scalar_one_or_none()
    assert conv_obj is not None
    assert conv_obj.owner_id == settings.DEFAULT_LOCAL_USER_ID


# ==============================================================================
# Scenario 14: Retrieval Debug Endpoints Enforce Tenant Isolation
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_14_retrieval_debug_endpoints_isolation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user_a, token_a = await _create_test_user(db_session, "tenant_a_s14")
    user_b, token_b = await _create_test_user(db_session, "tenant_b_s14")

    # A creates KB
    res_a_kb = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "A's Retrieval KB"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    kb_a_id = res_a_kb.json()["id"]

    # User B attempts to run retrieval debug endpoints against A's KB
    debug_payload = {"knowledge_base_id": kb_a_id, "query": "test query", "top_k": 3}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    for endpoint in ["/test", "/bm25-test", "/hybrid-test", "/rerank-test"]:
        res = await client.post(
            f"/api/v1/retrieval{endpoint}",
            json=debug_payload,
            headers=headers_b,
        )
        assert res.status_code == 404, f"Endpoint {endpoint} should return 404 for unauthorized KB"
