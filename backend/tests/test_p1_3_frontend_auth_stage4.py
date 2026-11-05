import asyncio
import io
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.admin import create_admin, promote_user
from app.core.auth import generate_server_token
from app.core.config import settings
from app.core.password import hash_password, verify_password
from app.models.conversation import Conversation
from app.models.user import User, UserRole


@pytest.fixture(autouse=True)
def optimize_password_iterations_for_tests(monkeypatch):
    """Speed up PBKDF2 iterations in test execution while preserving OWASP algorithm mechanics."""
    monkeypatch.setattr(settings, "PASSWORD_HASH_ITERATIONS", 10_000)


# ==============================================================================
# Scenarios 1 & 2: System Discovery Endpoint (/auth/mode)
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_1_auth_mode_discovery_local(client: AsyncClient, monkeypatch):
    """Scenario 1: In Local mode, /auth/mode reports local deployment without sensitive data."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", False)

    res = await client.get("/api/v1/auth/mode")
    assert res.status_code == 200
    data = res.json()
    assert data["deployment_mode"] == "local"
    assert data["allow_registration"] is False
    assert "version" in data


@pytest.mark.asyncio
async def test_scenario_2_auth_mode_discovery_server(client: AsyncClient, monkeypatch):
    """Scenario 2: In Server mode, /auth/mode reports server deployment and registration toggle."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", True)

    res = await client.get("/api/v1/auth/mode")
    assert res.status_code == 200
    data = res.json()
    assert data["deployment_mode"] == "server"
    assert data["allow_registration"] is True
    assert data["version"] == "1.3.0"


# ==============================================================================
# Scenarios 3 & 4: Public Registration Control & Standard Role Enforcement
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_3_registration_disabled_returns_403(client: AsyncClient, monkeypatch):
    """Scenario 3: When SERVER_ALLOW_REGISTRATION is False, registration is strictly rejected."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", False)

    res = await client.post(
        "/api/v1/auth/register",
        json={"username": "unauthorized_guest", "password": "Password123!"},
    )
    assert res.status_code == 403
    assert "disabled" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_scenario_4_public_registration_standard_user_role(client: AsyncClient, monkeypatch):
    """Scenario 4: When AUTO_FIRST_USER_ADMIN is False, public web registrants receive standard user role."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", True)
    monkeypatch.setattr(settings, "AUTO_FIRST_USER_ADMIN", False)

    res = await client.post(
        "/api/v1/auth/register",
        json={"username": "web_registrant_01", "password": "Password123!"},
    )
    assert res.status_code == 201
    assert res.json()["role"] == UserRole.USER


# ==============================================================================
# Scenarios 5 & 6: CLI Admin Creation & Promotion Tools
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_5_cli_admin_creation_and_login(client: AsyncClient, monkeypatch):
    """Scenario 5: First admin is initialized via CLI tool, then logs in with admin privileges."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Run CLI creation
    await create_admin(username="cli_super_admin", password="AdminSecurePass123!", email="admin@corp.internal")

    # Login via API using created admin credentials
    res = await client.post(
        "/api/v1/auth/login",
        json={"username": "cli_super_admin", "password": "AdminSecurePass123!"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["user"]["username"] == "cli_super_admin"
    assert data["user"]["role"] == UserRole.ADMIN
    assert data["token_type"].lower() == "bearer"
    assert data["access_token"]


@pytest.mark.asyncio
async def test_scenario_6_cli_promote_existing_user(client: AsyncClient, monkeypatch, db_session: AsyncSession):
    """Scenario 6: Existing standard user is promoted to admin via CLI tool."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "AUTO_FIRST_USER_ADMIN", False)

    # Register standard user
    res = await client.post(
        "/api/v1/auth/register",
        json={"username": "standard_dev", "password": "Password123!"},
    )
    assert res.status_code == 201
    assert res.json()["role"] == UserRole.USER

    # Promote to admin
    await promote_user("standard_dev")

    # Verify user now has admin role
    stmt = select(User).where(User.username == "standard_dev")
    user = (await db_session.execute(stmt)).scalar_one()
    assert user.role == UserRole.ADMIN


# ==============================================================================
# Scenarios 7, 8, 9, 10, 11: Authentication Security & Enumeration Defense
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_7_login_success_bearer_token(client: AsyncClient, monkeypatch):
    """Scenario 7: Successful login returns token response matching schema."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", True)

    await client.post(
        "/api/v1/auth/register",
        json={"username": "valid_user", "password": "Password123!"},
    )

    res = await client.post(
        "/api/v1/auth/login",
        json={"username": "valid_user", "password": "Password123!"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 86400
    assert data["user"]["username"] == "valid_user"


@pytest.mark.asyncio
async def test_scenario_8_login_invalid_password_generic_error(client: AsyncClient, monkeypatch):
    """Scenario 8: Wrong password returns 401 with generic error message."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    await client.post(
        "/api/v1/auth/register",
        json={"username": "test_pw_user", "password": "CorrectPassword123!"},
    )

    res = await client.post(
        "/api/v1/auth/login",
        json={"username": "test_pw_user", "password": "WrongPassword123!"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid username or password."


@pytest.mark.asyncio
async def test_scenario_9_login_nonexistent_user_generic_error(client: AsyncClient, monkeypatch):
    """Scenario 9: Non-existent user returns exact same generic 401 error to prevent enumeration."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    res = await client.post(
        "/api/v1/auth/login",
        json={"username": "nobody_exists_here", "password": "SomePassword123!"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid username or password."


@pytest.mark.asyncio
async def test_scenario_10_timing_attack_mitigation_dummy_hash(client: AsyncClient, monkeypatch, mocker):
    """Scenario 10: Non-existent user login executes dummy_verify_password for timing defense."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    from app.api.v1 import auth as auth_module

    spy = mocker.spy(auth_module, "dummy_verify_password")

    res = await client.post(
        "/api/v1/auth/login",
        json={"username": "ghost_user", "password": "SomePassword123!"},
    )
    assert res.status_code == 401
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_scenario_11_deactivated_user_cannot_login(client: AsyncClient, monkeypatch, db_session: AsyncSession):
    """Scenario 11: Deactivated user (is_active=False) cannot log in."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    res = await client.post(
        "/api/v1/auth/register",
        json={"username": "inactive_emp", "password": "Password123!"},
    )
    user_id = res.json()["id"]

    # Deactivate in database
    stmt = select(User).where(User.id == user_id)
    user = (await db_session.execute(stmt)).scalar_one()
    user.is_active = False
    await db_session.commit()

    res = await client.post(
        "/api/v1/auth/login",
        json={"username": "inactive_emp", "password": "Password123!"},
    )
    assert res.status_code == 401
    assert "deactivated" in res.json()["detail"].lower()


# ==============================================================================
# Scenarios 12, 13, 14: Password Change Revocation & Logout Semantics
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_12_password_change_revokes_previous_token(client: AsyncClient, monkeypatch):
    """Scenario 12: Password change increments token_version; previously issued token becomes immediately invalid."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Register & Login
    await client.post(
        "/api/v1/auth/register",
        json={"username": "pwd_user", "password": "OldPassword123!"},
    )
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"username": "pwd_user", "password": "OldPassword123!"},
    )
    old_token = login_res.json()["access_token"]
    old_headers = {"Authorization": f"Bearer {old_token}"}

    # Verify old token works
    res_me = await client.get("/api/v1/auth/me", headers=old_headers)
    assert res_me.status_code == 200

    # Change password
    chg_res = await client.post(
        "/api/v1/auth/password",
        headers=old_headers,
        json={"old_password": "OldPassword123!", "new_password": "NewSecurePassword456!"},
    )
    assert chg_res.status_code == 200

    # Verify old token is now REVOKED (token_version mismatch)
    res_me_revoked = await client.get("/api/v1/auth/me", headers=old_headers)
    assert res_me_revoked.status_code == 401


@pytest.mark.asyncio
async def test_scenario_13_new_password_allows_new_login_and_access(client: AsyncClient, monkeypatch):
    """Scenario 13: Login with new password issues a valid token that works."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    await client.post(
        "/api/v1/auth/register",
        json={"username": "pwd_user2", "password": "InitialPass123!"},
    )
    login1 = await client.post(
        "/api/v1/auth/login",
        json={"username": "pwd_user2", "password": "InitialPass123!"},
    )
    token1 = login1.json()["access_token"]

    await client.post(
        "/api/v1/auth/password",
        headers={"Authorization": f"Bearer {token1}"},
        json={"old_password": "InitialPass123!", "new_password": "UpdatedPass456!"},
    )

    # Login with new password
    login2 = await client.post(
        "/api/v1/auth/login",
        json={"username": "pwd_user2", "password": "UpdatedPass456!"},
    )
    assert login2.status_code == 200
    token2 = login2.json()["access_token"]

    res_me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token2}"})
    assert res_me.status_code == 200
    assert res_me.json()["username"] == "pwd_user2"


@pytest.mark.asyncio
async def test_scenario_14_logout_transparent_client_discard(client: AsyncClient, monkeypatch):
    """Scenario 14: Logout endpoint reports transparent client_discard semantics."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    await client.post(
        "/api/v1/auth/register",
        json={"username": "logout_tester", "password": "Password123!"},
    )
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"username": "logout_tester", "password": "Password123!"},
    )
    token = login_res.json()["access_token"]

    res = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()
    assert data["revocation_type"] == "client_discard"
    assert "丢弃" in data["message"] or "token" in data["message"].lower()


# ==============================================================================
# Scenarios 15, 16, 17, 18: Resource & SSE Stream Bearer Token Enforcement
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_15_server_mode_requires_token_for_conversations(client: AsyncClient, monkeypatch):
    """Scenario 15: In Server mode, calling conversations API without Bearer token returns 401."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    res = await client.get("/api/v1/conversations")
    assert res.status_code == 401
    assert "authentication" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_scenario_16_server_mode_sse_chat_stream_with_bearer_token(client: AsyncClient, monkeypatch):
    """Scenario 16: SSE chat streaming endpoint accepts valid Bearer token."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    await client.post(
        "/api/v1/auth/register",
        json={"username": "stream_user", "password": "Password123!"},
    )
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"username": "stream_user", "password": "Password123!"},
    )
    token = login_res.json()["access_token"]

    res = await client.post(
        "/api/v1/chat/completions/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "Hello world from authenticated user"},
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]


@pytest.mark.asyncio
async def test_scenario_17_server_mode_sse_chat_stream_without_token_returns_401(client: AsyncClient, monkeypatch):
    """Scenario 17: SSE chat streaming endpoint without token returns 401 in server mode."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    res = await client.post(
        "/api/v1/chat/completions/stream",
        json={"query": "Unauthenticated stream probe"},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_scenario_18_server_mode_sse_chat_stream_with_revoked_token_returns_401(client: AsyncClient, monkeypatch):
    """Scenario 18: SSE chat streaming endpoint with revoked token returns 401."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Generate token with outdated token_version
    await client.post(
        "/api/v1/auth/register",
        json={"username": "revoked_streamer", "password": "Password123!"},
    )
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"username": "revoked_streamer", "password": "Password123!"},
    )
    token = login_res.json()["access_token"]

    # Invalidate token via password change
    await client.post(
        "/api/v1/auth/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": "Password123!", "new_password": "BrandNewPassword123!"},
    )

    # Calling SSE stream with old token must fail
    res = await client.post(
        "/api/v1/chat/completions/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "Stream after password change"},
    )
    assert res.status_code == 401


# ==============================================================================
# Scenarios 19 & 20: Data Isolation Between Accounts & Local Mode Zero-Login
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_19_multitenant_conversation_isolation(client: AsyncClient, monkeypatch):
    """Scenario 19: User A creates a conversation; User B cannot see it in list or detail."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # User A
    await client.post("/api/v1/auth/register", json={"username": "tenant_a", "password": "Password123!"})
    login_a = await client.post("/api/v1/auth/login", json={"username": "tenant_a", "password": "Password123!"})
    token_a = login_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # User B
    await client.post("/api/v1/auth/register", json={"username": "tenant_b", "password": "Password123!"})
    login_b = await client.post("/api/v1/auth/login", json={"username": "tenant_b", "password": "Password123!"})
    token_b = login_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User A creates conversation
    create_res = await client.post("/api/v1/conversations", headers=headers_a, json={"title": "User A Private Topic"})
    assert create_res.status_code == 201
    conv_id = create_res.json()["id"]

    # User B lists conversations -> must NOT contain User A's conversation
    list_b = await client.get("/api/v1/conversations", headers=headers_b)
    assert list_b.status_code == 200
    ids_b = [c["id"] for c in list_b.json()]
    assert conv_id not in ids_b

    # User B directly queries User A's conversation ID -> IDOR defense 404
    get_b = await client.get(f"/api/v1/conversations/{conv_id}", headers=headers_b)
    assert get_b.status_code == 404


@pytest.mark.asyncio
async def test_scenario_20_local_mode_zero_login_bypass(client: AsyncClient, monkeypatch, db_session: AsyncSession):
    """Scenario 20: In Local mode, requests without token automatically succeed under usr_local_default."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")

    # Create conversation with no headers (zero-login)
    res = await client.post("/api/v1/conversations", json={"title": "Local Mode Conversation"})
    assert res.status_code == 201
    data = res.json()
    conv_id = data["id"]

    # Verify database model assigned usr_local_default
    stmt = select(Conversation).where(Conversation.id == conv_id)
    conv = (await db_session.execute(stmt)).scalar_one()
    assert conv.owner_id == "usr_local_default"

    # List conversations with no headers
    list_res = await client.get("/api/v1/conversations")
    assert list_res.status_code == 200
    assert any(c["id"] == conv_id for c in list_res.json())
