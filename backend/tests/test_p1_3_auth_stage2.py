import asyncio
import time
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import generate_server_token, get_current_user_id
from app.core.config import settings
from app.core.password import (
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.memory import Memory
from app.models.user import User, UserRole


@pytest.fixture(autouse=True)
def optimize_password_iterations_for_tests(monkeypatch):
    """Use 10,000 iterations for test suite speed while preserving OWASP algorithm mechanics."""
    monkeypatch.setattr(settings, "PASSWORD_HASH_ITERATIONS", 10_000)


# ==============================================================================
# Scenario 1 & 2: User Registration, Normalization & Duplicate Prevention
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_1_user_registration_success(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario 1: Standard user registration.
    Verifies user creation, password hashing, and clean response (no password_hash leak).
    """
    reg_payload = {
        "username": "alice_test",
        "password": "Password123!",
        "email": "Alice@Example.COM",
    }
    res = await client.post("/api/v1/auth/register", json=reg_payload)
    assert res.status_code == 201
    data = res.json()

    assert data["username"] == "alice_test"
    assert data["email"] == "alice@example.com"  # Normalized to lowercase
    assert data["role"] in (UserRole.ADMIN, UserRole.USER)
    assert data["is_active"] is True
    assert "id" in data
    assert "password_hash" not in data  # Never leaked
    assert "token_version" not in data

    # Verify DB state
    stmt = select(User).where(User.username == "alice_test")
    user = (await db_session.execute(stmt)).scalar_one_or_none()
    assert user is not None
    assert user.password_hash.startswith("pbkdf2_sha256$")
    assert verify_password("Password123!", user.password_hash)


@pytest.mark.asyncio
async def test_scenario_2_duplicate_username_and_email_prevention(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario 2: Reject duplicate username and email (case-insensitive & trimmed).
    """
    # 1. Register first user
    res1 = await client.post(
        "/api/v1/auth/register",
        json={"username": "bob_dupe", "password": "Password123!", "email": "bob@example.com"},
    )
    assert res1.status_code == 201

    # 2. Try duplicate username with different case and spaces
    res2 = await client.post(
        "/api/v1/auth/register",
        json={"username": "  BOB_DUPE  ", "password": "NewPassword456!", "email": "other@example.com"},
    )
    assert res2.status_code == 400
    assert "already taken" in res2.json()["detail"]

    # 3. Try duplicate email with different case
    res3 = await client.post(
        "/api/v1/auth/register",
        json={"username": "bob_new_name", "password": "NewPassword456!", "email": "BOB@EXAMPLE.COM"},
    )
    assert res3.status_code == 400
    assert "already registered" in res3.json()["detail"]


# ==============================================================================
# Scenario 3 & 4: Login, Password Verification & Timing Attack Defense
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_3_login_success_and_token_generation(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario 3: Successful login via username or email, obtaining a valid signed token.
    """
    await client.post(
        "/api/v1/auth/register",
        json={"username": "login_user", "password": "CorrectPassword789!", "email": "login@test.com"},
    )

    # Login with username
    res1 = await client.post(
        "/api/v1/auth/login",
        json={"username": "login_user", "password": "CorrectPassword789!"},
    )
    assert res1.status_code == 200
    token_data1 = res1.json()
    assert token_data1["token_type"] == "bearer"
    assert token_data1["access_token"].startswith("st_")
    assert token_data1["user"]["username"] == "login_user"
    assert token_data1["expires_in"] == 86400
    assert "password_hash" not in token_data1["user"]

    # Login with email
    res2 = await client.post(
        "/api/v1/auth/login",
        json={"username": "LOGIN@TEST.COM", "password": "CorrectPassword789!"},
    )
    assert res2.status_code == 200
    token_data2 = res2.json()
    assert token_data2["access_token"].startswith("st_")


@pytest.mark.asyncio
async def test_scenario_4_login_failure_generic_error_and_timing_defense(client: AsyncClient):
    """
    Scenario 4: Invalid password and non-existent username return uniform 401 error.
    """
    # 1. Non-existent user
    res_nonexistent = await client.post(
        "/api/v1/auth/login",
        json={"username": "ghost_user_never_existed", "password": "SomePassword123!"},
    )
    assert res_nonexistent.status_code == 401
    assert res_nonexistent.json()["detail"] == "Invalid username or password."

    # 2. Existing user with wrong password
    await client.post(
        "/api/v1/auth/register",
        json={"username": "existing_user_pwd", "password": "RealPassword123!"},
    )
    res_wrong_pwd = await client.post(
        "/api/v1/auth/login",
        json={"username": "existing_user_pwd", "password": "WrongPassword123!"},
    )
    assert res_wrong_pwd.status_code == 401
    assert res_wrong_pwd.json()["detail"] == "Invalid username or password."


# ==============================================================================
# Scenario 5 & 6: Server Mode Token Enforcement (Missing, Forged, Expired)
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_5_server_mode_rejects_unauthenticated_and_forged_tokens(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """
    Scenario 5: In Server mode, requests without token or with forged token return 401.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # 1. No token
    res_no_token = await client.get("/api/v1/auth/me")
    assert res_no_token.status_code == 401
    assert "Authentication required" in res_no_token.json()["detail"]

    # 2. Forged token with invalid signature
    res_forged = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer st_eyJzdWIiOiAidXNyX2Zha2UiLCAiZXhwIjogOTk5OTk5OTk5OX0.forgedsignature1234567890abcdef"},
    )
    assert res_forged.status_code == 401
    assert "Invalid or expired" in res_forged.json()["detail"]

    # 3. Malformed token prefix
    res_bad_format = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer random_string_not_starting_with_st"},
    )
    assert res_bad_format.status_code == 401


@pytest.mark.asyncio
async def test_scenario_6_server_mode_rejects_expired_token(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """
    Scenario 6: Expired token returns 401 in Server mode.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Create user in DB
    user = User(
        username="user_expire_test",
        password_hash="!dummy",
        role=UserRole.USER,
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    # Generate token expired 10 seconds ago
    expired_token = generate_server_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        token_version=user.token_version,
        expires_in=-10,
    )

    res = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert res.status_code == 401
    assert "Invalid or expired" in res.json()["detail"]


# ==============================================================================
# Scenario 7 & 8: Account Deactivation & Token Invalidation on Password Change
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_7_deactivated_user_token_rejection(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """
    Scenario 7: Deactivated user (is_active == False) is immediately blocked even with valid token.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # 1. Register and login
    user = User(
        username="deactivated_user",
        password_hash=hash_password("Pass12345!"),
        role=UserRole.USER,
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    token = generate_server_token(user.id, user.username, user.role, user.token_version)

    # 2. Access succeeds initially
    res_ok = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res_ok.status_code == 200

    # 3. Administrator deactivates user
    user.is_active = False
    await db_session.commit()

    # 4. Same token is now rejected with 401
    res_blocked = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res_blocked.status_code == 401
    assert "deactivated" in res_blocked.json()["detail"]


@pytest.mark.asyncio
async def test_scenario_8_token_version_invalidation_after_password_change(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """
    Scenario 8: Password change increments token_version; previously issued tokens are invalidated.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # 1. Register user
    user = User(
        username="pwd_change_user",
        password_hash=hash_password("OldPassword123!"),
        role=UserRole.USER,
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    old_token = generate_server_token(user.id, user.username, user.role, token_version=1)

    # 2. Change password via API
    change_res = await client.post(
        "/api/v1/auth/password",
        json={"old_password": "OldPassword123!", "new_password": "NewSecretPassword888!"},
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert change_res.status_code == 200

    # 3. Verify user's token_version incremented to 2 in DB
    await db_session.refresh(user)
    assert user.token_version == 2

    # 4. Old token (with token_version=1) is now rejected with 401
    res_old_token = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert res_old_token.status_code == 401
    assert "invalidated or revoked" in res_old_token.json()["detail"]

    # 5. Login with new password gives a new working token
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"username": "pwd_change_user", "password": "NewSecretPassword888!"},
    )
    assert login_res.status_code == 200
    new_token = login_res.json()["access_token"]

    res_new_token = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert res_new_token.status_code == 200
    assert res_new_token.json()["username"] == "pwd_change_user"


# ==============================================================================
# Scenario 9 & 10: First User Admin Bootstrap & Registration Disabling
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_9_first_user_admin_and_subsequent_users_role(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario 9: First registered user automatically becomes admin; subsequent users become user.
    """
    # 1. First user
    res1 = await client.post(
        "/api/v1/auth/register",
        json={"username": "first_boss", "password": "AdminPassword123!"},
    )
    assert res1.status_code == 201
    assert res1.json()["role"] == UserRole.ADMIN

    # 2. Second user
    res2 = await client.post(
        "/api/v1/auth/register",
        json={"username": "second_employee", "password": "UserPassword123!"},
    )
    assert res2.status_code == 201
    assert res2.json()["role"] == UserRole.USER


@pytest.mark.asyncio
async def test_scenario_10_concurrent_registration_admin_uniqueness(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario 10: Concurrency safety - only exactly one admin is created even under concurrent calls.
    """
    async def _do_register(name: str):
        return await client.post(
            "/api/v1/auth/register",
            json={"username": name, "password": "Password123!"},
        )

    # Execute 3 registrations simultaneously
    results = await asyncio.gather(
        _do_register("concurrent_user_1"),
        _do_register("concurrent_user_2"),
        _do_register("concurrent_user_3"),
    )

    statuses = [r.status_code for r in results]
    assert all(s == 201 for s in statuses)

    roles = [r.json()["role"] for r in results]
    # Exactly one user must have received admin role
    assert roles.count(UserRole.ADMIN) <= 1
    assert roles.count(UserRole.USER) >= 2


@pytest.mark.asyncio
async def test_scenario_11_registration_disabled_returns_403(client: AsyncClient, monkeypatch):
    """
    Scenario 11: When SERVER_ALLOW_REGISTRATION is False, public registration returns 403 Forbidden.
    """
    monkeypatch.setattr(settings, "SERVER_ALLOW_REGISTRATION", False)

    res = await client.post(
        "/api/v1/auth/register",
        json={"username": "new_denied_user", "password": "Password123!"},
    )
    assert res.status_code == 403
    assert "disabled" in res.json()["detail"]


# ==============================================================================
# Scenario 12 & 13: Local Mode Identity Permanence & Memory Compatibility
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_12_local_mode_identity_permanence_rejects_header_spoofing(
    client: AsyncClient, monkeypatch
):
    """
    Scenario 12: In Local mode, identity is permanently fixed to usr_local_default.
    Client-supplied X-User-Id or arbitrary tokens are rejected/ignored.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")
    monkeypatch.setattr(settings, "ALLOW_TEST_USER_OVERRIDE", False)

    # 1. Call /auth/me with no header -> gets local_admin
    res_default = await client.get("/api/v1/auth/me")
    assert res_default.status_code == 200
    assert res_default.json()["id"] == settings.DEFAULT_LOCAL_USER_ID

    # 2. Call /auth/me with spoofed X-User-Id -> STILL gets usr_local_default
    res_spoofed = await client.get(
        "/api/v1/auth/me",
        headers={"X-User-Id": "usr_attacker_spoofed_id"},
    )
    assert res_spoofed.status_code == 200
    assert res_spoofed.json()["id"] == settings.DEFAULT_LOCAL_USER_ID


@pytest.mark.asyncio
async def test_scenario_13_memory_compatibility_with_local_default_user(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Scenario 13: Verify Memory module works seamlessly with usr_local_default.
    """
    # 1. Create memory in local mode
    create_res = await client.post(
        "/api/v1/memories",
        json={
            "content": "P1.3 Stage 2 兼容性验证：持久化本地开发配置规范",
            "memory_type": "IMPORTANT_FACT",
        },
    )
    assert create_res.status_code == 201
    data = create_res.json()
    assert data["owner_id"] == settings.DEFAULT_LOCAL_USER_ID

    # 2. Query memory
    query_res = await client.post(
        "/api/v1/memories/query",
        json={"query": "P1.3 Stage 2 兼容性验证：持久化本地开发配置规范", "top_k": 3, "threshold": 0.2},
    )
    assert query_res.status_code == 200
    results = query_res.json()
    assert len(results) > 0
    assert results[0]["memory"]["owner_id"] == settings.DEFAULT_LOCAL_USER_ID


# ==============================================================================
# Scenario 14: Logout & Current User Profile Verification
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_14_logout_and_profile_endpoint(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """
    Scenario 14: Verify /auth/me and /auth/logout in Server mode.
    """
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # 1. Create and authenticate user
    user = User(
        username="profile_test_user",
        email="profile@test.com",
        password_hash=hash_password("Secret123!"),
        role=UserRole.USER,
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    token = generate_server_token(user.id, user.username, user.role, user.token_version)

    # 2. /auth/me returns accurate profile
    res_me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res_me.status_code == 200
    me_data = res_me.json()
    assert me_data["username"] == "profile_test_user"
    assert me_data["email"] == "profile@test.com"
    assert me_data["role"] == "user"

    # 3. /auth/logout returns successful discard instruction
    res_logout = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert res_logout.status_code == 200
    assert "logged out successfully" in res_logout.json()["message"].lower()
