import asyncio
import io
import json
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.admin import create_admin, promote_user
from app.core.config import settings
from app.core.password import hash_password, verify_password
from app.models.agent import AgentDefinition, AgentRun
from app.models.conversation import Conversation, ConversationAttachment, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.models.memory import Memory, MemorySourceType, MemoryType
from app.models.model_provider import ModelConfig, ModelProvider
from app.models.user import User, UserRole


@pytest.fixture(autouse=True)
def optimize_password_iterations_for_tests(monkeypatch):
    """Ensure fast cryptographic testing while preserving full PBKDF2 logic."""
    monkeypatch.setattr(settings, "PASSWORD_HASH_ITERATIONS", 10_000)


# ==============================================================================
# 1. Comprehensive A/B Dual-User Resource Isolation & IDOR Verification
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_1_ab_cross_tenant_isolation_complete(client: AsyncClient, monkeypatch, db_session: AsyncSession):
    """
    Scenario 1: Complete A/B Multi-Tenant Resource Isolation & IDOR Defense.
    User A creates all types of resources.
    User B verifies zero leakage in list queries and strict 404 responses on direct ID attacks.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", True)
    monkeypatch.setattr(settings, "AUTO_FIRST_USER_ADMIN", False)

    # 1. Register User A and User B
    res_reg_a = await client.post("/api/v1/auth/register", json={"username": "tenant_alice", "password": "Password123!"})
    assert res_reg_a.status_code == 201
    user_a_id = res_reg_a.json()["id"]

    res_reg_b = await client.post("/api/v1/auth/register", json={"username": "tenant_bob", "password": "Password123!"})
    assert res_reg_b.status_code == 201
    user_b_id = res_reg_b.json()["id"]

    # 2. Login User A
    login_a = await client.post("/api/v1/auth/login", json={"username": "tenant_alice", "password": "Password123!"})
    token_a = login_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # 3. User A creates Knowledge Base & Document
    res_kb_a = await client.post("/api/v1/knowledge-bases", headers=headers_a, json={"name": "Alice KB", "description": "Alice Secret KB"})
    assert res_kb_a.status_code in (200, 201)
    kb_a_id = res_kb_a.json()["id"]

    # User A creates Conversation
    res_conv_a = await client.post("/api/v1/conversations", headers=headers_a, json={"title": "Alice Secret Chat", "knowledge_base_id": kb_a_id})
    assert res_conv_a.status_code == 201
    conv_a_id = res_conv_a.json()["id"]

    # User A uploads Attachment to Conversation
    sample_txt = b"Confidential business strategy for Alice company."
    res_att_a = await client.post(
        f"/api/v1/conversations/{conv_a_id}/attachments",
        headers=headers_a,
        files=[("files", ("alice_plan.txt", sample_txt, "text/plain"))],
    )
    assert res_att_a.status_code == 201
    att_a_id = res_att_a.json()[0]["id"]

    # User A creates Long-term Memories
    res_mem_a1 = await client.post(
        "/api/v1/memories",
        headers=headers_a,
        json={"content": "Alice prefers Python and concise explanations.", "memory_type": "USER_PROFILE"},
    )
    assert res_mem_a1.status_code == 201
    mem_a1_id = res_mem_a1.json()["id"]

    res_mem_a2 = await client.post(
        "/api/v1/memories",
        headers=headers_a,
        json={"content": "Project Phoenix budget is 1 million dollars.", "memory_type": "IMPORTANT_FACT"},
    )
    assert res_mem_a2.status_code == 201
    mem_a2_id = res_mem_a2.json()["id"]

    # User A creates Private ModelProvider
    res_prov_a = await client.post(
        "/api/v1/models/providers",
        headers=headers_a,
        json={
            "name": "Alice Private Provider",
            "provider_type": "openai",
            "base_url": "https://api.alice-ai.corp",
            "api_key": "sk-alice-secret-key-12345",
            "is_public": False,
        },
    )
    assert res_prov_a.status_code == 201
    prov_a_id = res_prov_a.json()["id"]

    # User A creates an AgentRun record directly in DB for Alice
    from app.api.v1.agent import DEFAULT_AGENT_ID, get_or_create_default_agent
    default_agent = await get_or_create_default_agent(db_session)

    run_a_id = str(uuid.uuid4())
    run_a = AgentRun(
        id=run_a_id,
        agent_definition_id=default_agent.id,
        conversation_id=conv_a_id,
        owner_id=user_a_id,
        status="completed",
        input="Analyze secret data",
        final_answer="Top secret answer for Alice",
    )
    db_session.add(run_a)
    await db_session.commit()

    # 4. Login User B
    login_b = await client.post("/api/v1/auth/login", json={"username": "tenant_bob", "password": "Password123!"})
    token_b = login_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 5. User B list queries: VERIFY COMPLETE ABSENCE of Alice's resources
    list_convs_b = await client.get("/api/v1/conversations", headers=headers_b)
    assert list_convs_b.status_code == 200
    assert not any(c["id"] == conv_a_id for c in list_convs_b.json())

    list_kbs_b = await client.get("/api/v1/knowledge-bases", headers=headers_b)
    assert list_kbs_b.status_code == 200
    assert not any(k["id"] == kb_a_id for k in list_kbs_b.json())

    list_mems_b = await client.get("/api/v1/memories", headers=headers_b)
    assert list_mems_b.status_code == 200
    assert not any(m["id"] in (mem_a1_id, mem_a2_id) for m in list_mems_b.json()["items"])

    list_provs_b = await client.get("/api/v1/models/providers", headers=headers_b)
    assert list_provs_b.status_code == 200
    assert not any(p["id"] == prov_a_id for p in list_provs_b.json())

    list_runs_b = await client.get(f"/api/v1/agents/{default_agent.id}/runs", headers=headers_b)
    assert list_runs_b.status_code == 200
    assert not any(r["id"] == run_a_id for r in list_runs_b.json())

    # 6. User B DIRECT IDOR ATTACKS: VERIFY 404 NOT FOUND ACROSS ALL ENDPOINTS
    # Conversations & Messages & Attachments
    assert (await client.get(f"/api/v1/conversations/{conv_a_id}", headers=headers_b)).status_code == 404
    assert (await client.get(f"/api/v1/conversations/{conv_a_id}/messages", headers=headers_b)).status_code == 404
    assert (await client.get(f"/api/v1/conversations/{conv_a_id}/attachments", headers=headers_b)).status_code == 404
    assert (await client.get(f"/api/v1/conversations/{conv_a_id}/attachments/{att_a_id}", headers=headers_b)).status_code == 404
    assert (await client.delete(f"/api/v1/conversations/{conv_a_id}/attachments/{att_a_id}", headers=headers_b)).status_code == 404
    assert (await client.delete(f"/api/v1/conversations/{conv_a_id}", headers=headers_b)).status_code == 404

    # Knowledge Bases
    assert (await client.get(f"/api/v1/knowledge-bases/{kb_a_id}", headers=headers_b)).status_code == 404
    assert (await client.get(f"/api/v1/knowledge-bases/{kb_a_id}/documents", headers=headers_b)).status_code == 404
    assert (await client.delete(f"/api/v1/knowledge-bases/{kb_a_id}", headers=headers_b)).status_code == 404

    # Memories
    assert (await client.get(f"/api/v1/memories/{mem_a1_id}", headers=headers_b)).status_code == 404
    assert (await client.put(f"/api/v1/memories/{mem_a1_id}", headers=headers_b, json={"content": "Hacked content"})).status_code == 404
    assert (await client.delete(f"/api/v1/memories/{mem_a1_id}", headers=headers_b)).status_code == 404

    # Model Providers
    assert (await client.get(f"/api/v1/models/providers/{prov_a_id}", headers=headers_b)).status_code == 404
    assert (await client.put(f"/api/v1/models/providers/{prov_a_id}", headers=headers_b, json={"name": "Tampered"})).status_code == 404
    assert (await client.delete(f"/api/v1/models/providers/{prov_a_id}", headers=headers_b)).status_code == 404

    # Agent Runs
    assert (await client.get(f"/api/v1/agent-runs/{run_a_id}", headers=headers_b)).status_code == 404


# ==============================================================================
# 2. Sequential A/B Switch Flow & Re-check
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_2_user_switch_sequence_and_no_crosstalk(client: AsyncClient, monkeypatch):
    """
    Scenario 2: Workflow verification:
    User A logs in -> creates data -> logout -> User B logs in -> checks -> reload -> checks.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", True)
    monkeypatch.setattr(settings, "AUTO_FIRST_USER_ADMIN", False)

    # 1. User A Register & Login
    await client.post("/api/v1/auth/register", json={"username": "user_alpha", "password": "Password123!"})
    login_a = await client.post("/api/v1/auth/login", json={"username": "user_alpha", "password": "Password123!"})
    token_a = login_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # User A creates Conversation
    res_c_a = await client.post("/api/v1/conversations", headers=headers_a, json={"title": "Alpha Research"})
    c_a_id = res_c_a.json()["id"]

    # User A logs out
    res_out_a = await client.post("/api/v1/auth/logout", headers=headers_a)
    assert res_out_a.status_code == 200

    # 2. User B Register & Login
    await client.post("/api/v1/auth/register", json={"username": "user_beta", "password": "Password123!"})
    login_b = await client.post("/api/v1/auth/login", json={"username": "user_beta", "password": "Password123!"})
    token_b = login_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User B checks conversations -> Alpha Research not present
    list_b1 = await client.get("/api/v1/conversations", headers=headers_b)
    assert list_b1.status_code == 200
    assert not any(c["id"] == c_a_id for c in list_b1.json())

    # User B creates conversation
    res_c_b = await client.post("/api/v1/conversations", headers=headers_b, json={"title": "Beta Operations"})
    c_b_id = res_c_b.json()["id"]

    # User B "reloads" (simulated via fresh request with token_b)
    list_b2 = await client.get("/api/v1/conversations", headers=headers_b)
    assert len(list_b2.json()) == 1
    assert list_b2.json()[0]["id"] == c_b_id

    # 3. User A logs in again -> sees only c_a_id, never c_b_id
    login_a2 = await client.post("/api/v1/auth/login", json={"username": "user_alpha", "password": "Password123!"})
    token_a2 = login_a2.json()["access_token"]
    list_a2 = await client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token_a2}"})
    assert len(list_a2.json()) == 1
    assert list_a2.json()[0]["id"] == c_a_id


# ==============================================================================
# 3. Local Mode Acceptance & Header Spoofing Resistance
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_3_local_mode_all_functional_acceptance(client: AsyncClient, monkeypatch, db_session: AsyncSession):
    """
    Scenario 3: Local mode operation verification:
    - Zero login, direct workbench access.
    - User identity pinned to usr_local_default.
    - Spoofed X-User-Id or Authorization headers are strictly ignored when ALLOW_TEST_USER_OVERRIDE=False.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")
    monkeypatch.setattr(settings, "ALLOW_TEST_USER_OVERRIDE", False)

    # 1. Discovery reports local
    mode_res = await client.get("/api/v1/auth/mode")
    assert mode_res.status_code == 200
    assert mode_res.json()["deployment_mode"] == "local"

    # 2. Create resource with NO headers
    conv_res = await client.post("/api/v1/conversations", json={"title": "Local Default Conv"})
    assert conv_res.status_code == 201
    conv_id = conv_res.json()["id"]

    stmt = select(Conversation).where(Conversation.id == conv_id)
    conv = (await db_session.execute(stmt)).scalar_one()
    assert conv.owner_id == settings.DEFAULT_LOCAL_USER_ID

    # 3. Attempt to spoof identity via forged header
    spoof_headers = {"X-User-Id": "forged_victim_user", "Authorization": "Bearer fake_token"}
    spoof_res = await client.post("/api/v1/conversations", headers=spoof_headers, json={"title": "Spoof Probe Conv"})
    assert spoof_res.status_code == 201
    spoof_conv_id = spoof_res.json()["id"]

    stmt_spoof = select(Conversation).where(Conversation.id == spoof_conv_id)
    spoof_conv = (await db_session.execute(stmt_spoof)).scalar_one()
    # Must STILL belong to usr_local_default, not forged identity!
    assert spoof_conv.owner_id == settings.DEFAULT_LOCAL_USER_ID


# ==============================================================================
# 4. Server Mode Unauthenticated & Disabled User Security
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_4_server_mode_unauthenticated_api_rejection(client: AsyncClient, monkeypatch):
    """
    Scenario 4: Verify unauthenticated requests are rejected across all core APIs in server mode.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Protected endpoints
    endpoints = [
        ("GET", "/api/v1/conversations"),
        ("POST", "/api/v1/conversations"),
        ("GET", "/api/v1/knowledge-bases"),
        ("POST", "/api/v1/knowledge-bases"),
        ("GET", "/api/v1/memories"),
        ("POST", "/api/v1/memories"),
        ("GET", "/api/v1/models/providers"),
        ("POST", "/api/v1/models/providers"),
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/password"),
        ("POST", "/api/v1/chat/completions/stream"),
        ("POST", "/api/v1/agents/default/runs/stream"),
    ]

    for method, path in endpoints:
        if method == "GET":
            res = await client.get(path)
        else:
            res = await client.post(path, json={})
        assert res.status_code == 401, f"Expected 401 for unauthenticated {method} {path}, got {res.status_code}"


# ==============================================================================
# 5. Public vs Private ModelProvider Permissions
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_5_public_vs_private_model_provider_permissions(client: AsyncClient, monkeypatch):
    """
    Scenario 5: Standard user cannot create or tamper with public (system-level) ModelProviders.
    Only admin can manage public ModelProviders.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "AUTO_FIRST_USER_ADMIN", False)

    # 1. CLI creates Admin
    await create_admin(username="sys_admin", password="AdminPass123!", email="admin@corp.com")
    login_admin = await client.post("/api/v1/auth/login", json={"username": "sys_admin", "password": "AdminPass123!"})
    token_admin = login_admin.json()["access_token"]
    headers_admin = {"Authorization": f"Bearer {token_admin}"}

    # Admin creates public ModelProvider
    res_pub = await client.post(
        "/api/v1/models/providers",
        headers=headers_admin,
        json={
            "name": "Global Company DeepSeek",
            "provider_type": "openai",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-company-global-key",
            "is_public": True,
        },
    )
    assert res_pub.status_code == 201
    pub_prov_id = res_pub.json()["id"]
    assert res_pub.json()["owner_id"] is None  # System public provider

    # 2. Standard user registers & logs in
    await client.post("/api/v1/auth/register", json={"username": "dev_standard", "password": "Password123!"})
    login_dev = await client.post("/api/v1/auth/login", json={"username": "dev_standard", "password": "Password123!"})
    token_dev = login_dev.json()["access_token"]
    headers_dev = {"Authorization": f"Bearer {token_dev}"}

    # Standard user CAN see the public provider in list
    list_dev = await client.get("/api/v1/models/providers", headers=headers_dev)
    assert list_dev.status_code == 200
    assert any(p["id"] == pub_prov_id for p in list_dev.json())

    # Standard user CANNOT modify the public provider (403 Forbidden)
    res_tamper = await client.put(
        f"/api/v1/models/providers/{pub_prov_id}",
        headers=headers_dev,
        json={"name": "Hacked Provider Name"},
    )
    assert res_tamper.status_code == 403
    assert "只有管理员" in res_tamper.json()["detail"]

    # Standard user tries to create a public provider -> gets silently/strictly forced to private
    res_fake_pub = await client.post(
        "/api/v1/models/providers",
        headers=headers_dev,
        json={
            "name": "Dev Fake Public Provider",
            "provider_type": "openai",
            "base_url": "https://api.dev.corp",
            "is_public": True,  # User tries to pass is_public=True
        },
    )
    assert res_fake_pub.status_code == 201
    # Must NOT be None! Must be assigned dev's user_id
    assert res_fake_pub.json()["owner_id"] is not None
