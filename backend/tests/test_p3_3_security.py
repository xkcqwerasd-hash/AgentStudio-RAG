import asyncio
import ipaddress
import os
import secrets
import socket
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import generate_server_token
from app.core.config import Settings, validate_server_security_settings
from app.core.security import (
    decrypt_api_key,
    encrypt_api_key,
    get_encryption_fernet,
)
from app.core.ssrf import (
    SSRFBlockedException,
    get_ssrf_safe_async_client,
    is_ip_allowed,
    validate_url_ssrf,
)
from app.db.session import async_session_factory, init_db
from app.llm.factory import (
    ModelDisabledError,
    ModelNotFoundError,
    resolve_llm_client,
)
from app.llm.manager import probe_ollama_models, probe_vllm_models
from app.main import app
from app.models.model_provider import ModelConfig, ModelProvider
from app.models.user import User, UserRole


# ==============================================================================
# SECTION 1: Problem 1 — Server Mode Secret Key Security
# ==============================================================================


def test_server_secret_validation_rejects_empty():
    cfg = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET="")
    with pytest.raises(RuntimeError, match="cannot be empty"):
        validate_server_security_settings(cfg)


def test_server_secret_validation_rejects_default_placeholder():
    cfg = Settings(
        DEPLOYMENT_MODE="server",
        SERVER_AUTH_SECRET="agentstudio_server_secret_key_change_in_production",
    )
    with pytest.raises(RuntimeError, match="must be explicitly configured"):
        validate_server_security_settings(cfg)


def test_server_secret_validation_rejects_short_secret():
    cfg = Settings(
        DEPLOYMENT_MODE="server",
        SERVER_AUTH_SECRET="too_short_secret_123",
    )
    with pytest.raises(RuntimeError, match="too short"):
        validate_server_security_settings(cfg)


def test_server_secret_validation_rejects_common_placeholders():
    placeholders = [
        "change_me_to_a_secure_random_server_secret_key_12345",
        "example_secret_key_that_is_long_enough_for_length_check_1",
        "this_is_a_default_secret_key_for_testing_purposes_only",
        "placeholder_secret_key_long_enough_12345678901234567890",
    ]
    for p in placeholders:
        cfg = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET=p)
        with pytest.raises(RuntimeError, match="insecure placeholder or default"):
            validate_server_security_settings(cfg)


def test_server_secret_validation_rejects_repetitive_and_predictable():
    # 1. Identical character repeat
    cfg1 = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET="a" * 40)
    with pytest.raises(RuntimeError, match="repetitive sequence|insufficient"):
        validate_server_security_settings(cfg1)

    # 2. Cycle repeat
    cfg2 = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET="abab" * 10)
    with pytest.raises(RuntimeError, match="repetitive sequence|low Shannon entropy"):
        validate_server_security_settings(cfg2)

    # 3. Predictable alphabet run
    cfg3 = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET="abcdefghijklmnopqrstuvwxyzABCDEFGHIJ")
    with pytest.raises(RuntimeError, match="predictable pattern|low Shannon entropy"):
        validate_server_security_settings(cfg3)

    # 4. Predictable numbers run
    cfg4 = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET="0123456789012345678901234567890123456789")
    with pytest.raises(RuntimeError, match="predictable pattern|low Shannon entropy|insecure placeholder"):
        validate_server_security_settings(cfg4)


def test_server_secret_validation_accepts_strong_secrets():
    # 1. Token urlsafe (32 bytes -> 43 chars, recommended)
    strong_urlsafe = secrets.token_urlsafe(32)
    cfg1 = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET=strong_urlsafe)
    validate_server_security_settings(cfg1)

    # 2. Token hex (32 bytes -> 64 chars)
    strong_hex = secrets.token_hex(32)
    cfg2 = Settings(DEPLOYMENT_MODE="server", SERVER_AUTH_SECRET=strong_hex)
    validate_server_security_settings(cfg2)


def test_local_mode_allows_default_secret():
    # In local mode, default secret should NOT block startup
    cfg = Settings(
        DEPLOYMENT_MODE="local",
        SERVER_AUTH_SECRET="agentstudio_server_secret_key_change_in_production",
    )
    validate_server_security_settings(cfg)


@pytest.mark.asyncio
async def test_server_mode_init_db_does_not_create_local_user(
    db_session: AsyncSession,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Run init_db in server mode
    await init_db()

    # Verify usr_local_default was NOT created
    stmt = select(User).where(User.id == settings.DEFAULT_LOCAL_USER_ID)
    res = await db_session.execute(stmt)
    user = res.scalar_one_or_none()
    assert user is None, "usr_local_default should NOT be created in server deployment mode"


def test_model_encryption_roundtrip_and_persistence(tmp_path, monkeypatch):
    import app.core.security as sec_mod
    from app.core.config import settings

    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(settings, "MODEL_ENCRYPTION_KEY", None)
    sec_mod._cached_fernet = None

    plain_key = "sk-live-test-encryption-key-123456789"
    ciphertext = encrypt_api_key(plain_key)
    assert ciphertext != plain_key
    assert isinstance(ciphertext, str)

    # Keyfile should be written to storage dir
    key_file = tmp_path / ".model_master_key"
    assert key_file.exists()

    # Decrypt using cached cipher
    decrypted = decrypt_api_key(ciphertext)
    assert decrypted == plain_key

    # Clear cached cipher to simulate process restart
    sec_mod._cached_fernet = None
    decrypted_after_restart = decrypt_api_key(ciphertext)
    assert decrypted_after_restart == plain_key


# ==============================================================================
# SECTION 2: Problem 2 — SSRF Risk in Model Probing & DNS Rebinding
# ==============================================================================


def test_ssrf_ip_allowed_policy():
    # Loopback
    assert is_ip_allowed(ipaddress.ip_address("127.0.0.1"), allow_local=False) is False
    assert is_ip_allowed(ipaddress.ip_address("127.0.0.1"), allow_local=True) is True
    assert is_ip_allowed(ipaddress.ip_address("::1"), allow_local=False) is False
    assert is_ip_allowed(ipaddress.ip_address("::1"), allow_local=True) is True

    # RFC 1918 Private subnets
    assert is_ip_allowed(ipaddress.ip_address("10.0.0.1"), allow_local=False) is False
    assert is_ip_allowed(ipaddress.ip_address("10.0.0.1"), allow_local=True) is True
    assert is_ip_allowed(ipaddress.ip_address("172.16.5.1"), allow_local=False) is False
    assert is_ip_allowed(ipaddress.ip_address("172.16.5.1"), allow_local=True) is True
    assert is_ip_allowed(ipaddress.ip_address("192.168.1.100"), allow_local=False) is False
    assert is_ip_allowed(ipaddress.ip_address("192.168.1.100"), allow_local=True) is True

    # Cloud metadata (Never allowed in any mode)
    assert is_ip_allowed(ipaddress.ip_address("169.254.169.254"), allow_local=False) is False
    assert is_ip_allowed(ipaddress.ip_address("169.254.169.254"), allow_local=True) is False
    assert is_ip_allowed(ipaddress.ip_address("100.100.100.200"), allow_local=True) is False
    assert is_ip_allowed(ipaddress.ip_address("fd00:ec2::254"), allow_local=True) is False

    # Public Routable IP
    assert is_ip_allowed(ipaddress.ip_address("8.8.8.8"), allow_local=False) is True
    assert is_ip_allowed(ipaddress.ip_address("1.1.1.1"), allow_local=False) is True

    # IPv4-mapped IPv6 loopback
    mapped_loopback = ipaddress.ip_address("::ffff:127.0.0.1")
    assert is_ip_allowed(mapped_loopback, allow_local=False) is False
    assert is_ip_allowed(mapped_loopback, allow_local=True) is True


def test_ssrf_validate_url_schemes():
    # Only HTTP and HTTPS
    with pytest.raises(SSRFBlockedException, match="Unsupported protocol scheme"):
        validate_url_ssrf("file:///etc/passwd", allow_local=False)

    with pytest.raises(SSRFBlockedException, match="Unsupported protocol scheme"):
        validate_url_ssrf("gopher://127.0.0.1:11434", allow_local=False)

    with pytest.raises(SSRFBlockedException, match="Unsupported protocol scheme"):
        validate_url_ssrf("ftp://127.0.0.1/models", allow_local=False)


def test_ssrf_validate_url_cloud_metadata_hosts():
    with pytest.raises(SSRFBlockedException, match="cloud metadata"):
        validate_url_ssrf("http://metadata.google.internal/computeMetadata/v1", allow_local=True)

    with pytest.raises(SSRFBlockedException, match="cloud metadata"):
        validate_url_ssrf("http://instance-data/latest/meta-data", allow_local=True)


def test_ssrf_validate_url_server_mode_blocks_internal():
    with pytest.raises(SSRFBlockedException, match="Security Policy Restriction"):
        validate_url_ssrf("http://127.0.0.1:11434", allow_local=False)

    with pytest.raises(SSRFBlockedException, match="Security Policy Restriction"):
        validate_url_ssrf("http://localhost:11434", allow_local=False)

    with pytest.raises(SSRFBlockedException, match="Security Policy Restriction"):
        validate_url_ssrf("http://192.168.1.10:8000/v1", allow_local=False)


def test_ssrf_validate_url_local_mode_allows_localhost():
    clean_url, resolved_ips = validate_url_ssrf("http://127.0.0.1:11434", allow_local=True)
    assert clean_url == "http://127.0.0.1:11434"
    assert "127.0.0.1" in resolved_ips


@pytest.mark.asyncio
async def test_probing_endpoints_require_auth_in_server_mode(
    client: AsyncClient,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Unauthenticated Ollama probe in server mode should return 401
    resp_ollama = await client.post("/api/v1/models/probe/ollama", json={"base_url": "http://example.com"})
    assert resp_ollama.status_code == 401

    # Unauthenticated vLLM probe in server mode should return 401
    resp_vllm = await client.post("/api/v1/models/probe/vllm", json={"base_url": "http://example.com"})
    assert resp_vllm.status_code == 401


@pytest.mark.asyncio
async def test_server_mode_probe_blocks_ssrf(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Create an authenticated user
    user = User(
        username="sec_tester",
        password_hash="fake_hash",
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    token = generate_server_token(user_id=user.id, username=user.username, role=user.role)
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Probe loopback should be rejected by SSRF guard
    resp = await client.post(
        "/api/v1/models/probe/ollama",
        json={"base_url": "http://127.0.0.1:11434"},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "SSRF" in data["error"] or "Security Policy" in data["error"]

    # 2. Probe cloud metadata should be rejected
    resp_meta = await client.post(
        "/api/v1/models/probe/ollama",
        json={"base_url": "http://169.254.169.254/latest/meta-data"},
        headers=headers,
    )
    assert resp_meta.status_code == 200
    data_meta = resp_meta.json()
    assert data_meta["success"] is False
    assert "SSRF" in data_meta["error"] or "Security Policy" in data_meta["error"]


# ------------------------------------------------------------------------------
# Targeted DNS Rebinding & Redirect Attack Tests
# ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dns_rebind_to_private_ip_blocked_at_socket_level(monkeypatch):
    """
    Simulate DNS Rebinding:
    - 1st DNS resolution (during URL pre-validation): returns public IP 93.184.216.34 (passes).
    - 2nd DNS resolution (during actual TCP connect): returns private IP 192.168.1.1 (rebind attack).
    Verify that SSRFSafeBackend.connect_tcp intercepts the socket connection and blocks it!
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    call_count = 0

    def mock_getaddrinfo(host, port, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 1st call: Public IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        else:
            # 2nd call: Rebound to private subnet IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", port))]

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        res = await probe_ollama_models(base_url="http://rebind-private.attacker.com:11434")
        assert res.success is False
        assert "SSRF Socket Intercept" in res.error
        assert "192.168.1.1" in res.error


@pytest.mark.asyncio
async def test_dns_rebind_to_loopback_blocked_at_socket_level(monkeypatch):
    """
    Simulate DNS Rebinding to loopback:
    - 1st resolution: returns public IP 93.184.216.34 (passes).
    - 2nd resolution: returns loopback 127.0.0.1 (rebind attack).
    Verify that socket-level TCP connection is intercepted and blocked!
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    call_count = 0

    def mock_getaddrinfo(host, port, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        else:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        res = await probe_vllm_models(base_url="http://rebind-loopback.attacker.com:8000/v1")
        assert res.success is False
        assert "SSRF Socket Intercept" in res.error
        assert "127.0.0.1" in res.error


@pytest.mark.asyncio
async def test_redirect_to_localhost_blocked(monkeypatch):
    """
    Simulate public server returning 302 redirect to localhost (http://127.0.0.1:11434).
    Verify that client follows redirects safely and blocks the redirected target!
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    class Mock302Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "public-host" in str(request.url):
                return httpx.Response(
                    302,
                    headers={"Location": "http://127.0.0.1:11434/api/tags"},
                    request=request,
                )
            return httpx.Response(200, text="SHOULD_NEVER_REACH_LOCALHOST", request=request)

    async def _on_response(response: httpx.Response):
        if response.is_redirect and "location" in response.headers:
            loc = response.headers["location"]
            target_url = str(response.url.join(loc))
            validate_url_ssrf(target_url, allow_local=False)

    client = httpx.AsyncClient(
        transport=Mock302Transport(),
        follow_redirects=True,
        event_hooks={"response": [_on_response]},
    )

    with pytest.raises(SSRFBlockedException, match="Destination address '127.0.0.1' is not permitted"):
        await client.get("http://public-host.com/api/tags")

    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_to_private_ip_blocked(monkeypatch):
    """
    Simulate public server returning 302 redirect to RFC 1918 private IP (http://10.0.0.5:8000).
    Verify that redirect is intercepted and blocked!
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    class Mock302Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "public-host" in str(request.url):
                return httpx.Response(
                    302,
                    headers={"Location": "http://10.0.0.5:8000/v1/models"},
                    request=request,
                )
            return httpx.Response(200, text="SHOULD_NEVER_REACH_PRIVATE", request=request)

    async def _on_response(response: httpx.Response):
        if response.is_redirect and "location" in response.headers:
            loc = response.headers["location"]
            target_url = str(response.url.join(loc))
            validate_url_ssrf(target_url, allow_local=False)

    client = httpx.AsyncClient(
        transport=Mock302Transport(),
        follow_redirects=True,
        event_hooks={"response": [_on_response]},
    )

    with pytest.raises(SSRFBlockedException, match="Destination address '10.0.0.5' is not permitted"):
        await client.get("http://public-host.com/v1/models")

    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_to_cloud_metadata_blocked(monkeypatch):
    """
    Simulate public server returning 302 redirect to cloud metadata (169.254.169.254).
    Verify that redirect to metadata is blocked!
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    class Mock302Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "public-host" in str(request.url):
                return httpx.Response(
                    302,
                    headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                    request=request,
                )
            return httpx.Response(200, text="SHOULD_NEVER_REACH_METADATA", request=request)

    async def _on_response(response: httpx.Response):
        if response.is_redirect and "location" in response.headers:
            loc = response.headers["location"]
            target_url = str(response.url.join(loc))
            validate_url_ssrf(target_url, allow_local=False)

    client = httpx.AsyncClient(
        transport=Mock302Transport(),
        follow_redirects=True,
        event_hooks={"response": [_on_response]},
    )

    with pytest.raises(SSRFBlockedException, match="Destination address '169.254.169.254' is not permitted"):
        await client.get("http://public-host.com/info")

    await client.aclose()


# ==============================================================================
# SECTION 3: Problem 3 — Multi-tenant Default Model Isolation
# ==============================================================================


@pytest.mark.asyncio
async def test_multitenant_default_model_scoping(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Create User A and User B
    user_a = User(username="alice", password_hash="hash_a", role=UserRole.USER, is_active=True)
    user_b = User(username="bob", password_hash="hash_b", role=UserRole.USER, is_active=True)
    admin = User(username="superadmin", password_hash="hash_adm", role=UserRole.ADMIN, is_active=True)
    db_session.add_all([user_a, user_b, admin])
    await db_session.commit()
    await db_session.refresh(user_a)
    await db_session.refresh(user_b)
    await db_session.refresh(admin)

    # 1. Create a System Public Provider & Model
    sys_prov = ModelProvider(
        name="System Public Provider",
        provider_type="openai_compatible",
        base_url="https://api.system.example",
        owner_id=None,
        enabled=True,
    )
    db_session.add(sys_prov)
    await db_session.flush()

    sys_model = ModelConfig(
        provider_id=sys_prov.id,
        model_name="sys-gpt-4o",
        display_name="System GPT-4o",
        is_default=True,
        enabled=True,
    )
    db_session.add(sys_model)
    await db_session.commit()

    # 2. User A creates private provider & model and marks is_default=True
    prov_a = ModelProvider(
        name="Alice Private Provider",
        provider_type="openai_compatible",
        base_url="https://api.alice.example",
        owner_id=user_a.id,
        enabled=True,
    )
    db_session.add(prov_a)
    await db_session.flush()

    model_a = ModelConfig(
        provider_id=prov_a.id,
        model_name="alice-deepseek",
        display_name="Alice DeepSeek",
        is_default=True,
        enabled=True,
    )
    db_session.add(model_a)
    await db_session.commit()

    # 3. User B creates private provider & model and marks is_default=True
    prov_b = ModelProvider(
        name="Bob Private Provider",
        provider_type="openai_compatible",
        base_url="https://api.bob.example",
        owner_id=user_b.id,
        enabled=True,
    )
    db_session.add(prov_b)
    await db_session.flush()

    # Set default via API endpoint for User B
    token_b = generate_server_token(user_id=user_b.id, username=user_b.username, role=user_b.role)
    headers_b = {"Authorization": f"Bearer {token_b}"}

    create_b_res = await client.post(
        f"/api/v1/models/providers/{prov_b.id}/models",
        json={
            "model_name": "bob-qwen",
            "display_name": "Bob Qwen",
            "model_type": "chat",
            "is_default": True,
            "enabled": True,
        },
        headers=headers_b,
    )
    assert create_b_res.status_code == 201
    model_b_id = create_b_res.json()["id"]

    # 4. Verify User B setting is_default DID NOT clear User A's default!
    await db_session.refresh(model_a)
    assert model_a.is_default is True, "User A's default model was incorrectly cleared by User B!"

    # Verify System public default was NOT cleared
    await db_session.refresh(sys_model)
    assert sys_model.is_default is True, "System default model was incorrectly cleared by User B!"

    # 5. Verify resolve_llm_client hierarchical resolution:
    # Alice (user_a) resolves Alice's private default
    _, resolved_a = await resolve_llm_client(db_session, model_id=None, user_id=user_a.id)
    assert resolved_a.id == model_a.id
    assert resolved_a.display_name == "Alice DeepSeek"

    # Bob (user_b) resolves Bob's private default
    _, resolved_b = await resolve_llm_client(db_session, model_id=None, user_id=user_b.id)
    assert resolved_b.id == model_b_id
    assert resolved_b.display_name == "Bob Qwen"

    # User C (no private default) resolves System Public default
    user_c = User(username="charlie", password_hash="hash_c", role=UserRole.USER, is_active=True)
    db_session.add(user_c)
    await db_session.commit()
    await db_session.refresh(user_c)

    _, resolved_c = await resolve_llm_client(db_session, model_id=None, user_id=user_c.id)
    assert resolved_c.id == sys_model.id
    assert resolved_c.display_name == "System GPT-4o"

    # 6. IDOR Guard: User A attempting to use User B's private model must be strictly rejected
    with pytest.raises(ModelNotFoundError):
        await resolve_llm_client(db_session, model_id=model_b_id, user_id=user_a.id)

    # 7. Setting default model permission rules:
    # Regular user cannot set system public model as default
    token_a = generate_server_token(user_id=user_a.id, username=user_a.username, role=user_a.role)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    resp_adm_only = await client.post(
        f"/api/v1/models/configs/{sys_model.id}/set-default",
        headers=headers_a,
    )
    assert resp_adm_only.status_code == 403

    # Regular user cannot set another user's private model as default
    resp_other_user = await client.post(
        f"/api/v1/models/configs/{model_b_id}/set-default",
        headers=headers_a,
    )
    assert resp_other_user.status_code == 404

    # User CAN set their own model as default
    resp_own = await client.post(
        f"/api/v1/models/configs/{model_a.id}/set-default",
        headers=headers_a,
    )
    assert resp_own.status_code == 200
    assert resp_own.json()["is_default"] is True


@pytest.mark.asyncio
async def test_concurrent_default_model_modification_uniqueness(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    """
    Test concurrent requests modifying default models within the same tenant.
    Guarantees that at the end of concurrent operations, exactly ONE model has is_default=True.
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # Create tenant user
    user = User(username="concurrent_user", password_hash="hash", role=UserRole.USER, is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    prov = ModelProvider(
        name="Concurrent Provider",
        provider_type="openai_compatible",
        base_url="https://api.concurrent.example",
        owner_id=user.id,
        enabled=True,
    )
    db_session.add(prov)
    await db_session.flush()

    m1 = ModelConfig(provider_id=prov.id, model_name="m1", display_name="Model 1", is_default=True, enabled=True)
    m2 = ModelConfig(provider_id=prov.id, model_name="m2", display_name="Model 2", is_default=False, enabled=True)
    m3 = ModelConfig(provider_id=prov.id, model_name="m3", display_name="Model 3", is_default=False, enabled=True)
    db_session.add_all([m1, m2, m3])
    await db_session.commit()
    await db_session.refresh(m1)
    await db_session.refresh(m2)
    await db_session.refresh(m3)

    token = generate_server_token(user_id=user.id, username=user.username, role=user.role)
    headers = {"Authorization": f"Bearer {token}"}

    # Simulate 12 concurrent requests toggling between m1, m2, m3
    model_ids = [m1.id, m2.id, m3.id] * 4

    async def _toggle_default(target_id: str):
        return await client.post(f"/api/v1/models/configs/{target_id}/set-default", headers=headers)

    responses = await asyncio.gather(*[_toggle_default(mid) for mid in model_ids])
    for r in responses:
        assert r.status_code == 200

    # Verify that in database, exactly ONE model is is_default=True for this provider
    subq = select(ModelProvider.id).where(ModelProvider.owner_id == user.id)
    count_stmt = select(func.count(ModelConfig.id)).where(
        ModelConfig.provider_id.in_(subq),
        ModelConfig.is_default == True,
    )
    res = await db_session.execute(count_stmt)
    default_count = res.scalar()

    assert default_count == 1, f"Expected exactly 1 default model, found {default_count}!"


@pytest.mark.asyncio
async def test_three_execution_engines_identity_wiring(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    """
    Verify that DirectChatEngine, WorkflowRAGEngine, and LangGraphAgentRuntime
    execution routes correctly pass the authenticated user_id to resolve_llm_client,
    guaranteeing that User A gets User A's private default model and User B gets User B's.
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # 1. Create User A and User B
    user_a = User(username="alice_engine", password_hash="hash_a", role=UserRole.USER, is_active=True)
    user_b = User(username="bob_engine", password_hash="hash_b", role=UserRole.USER, is_active=True)
    db_session.add_all([user_a, user_b])
    await db_session.commit()
    await db_session.refresh(user_a)
    await db_session.refresh(user_b)

    # 2. User A private provider & default model
    prov_a = ModelProvider(
        name="Alice Provider",
        provider_type="openai_compatible",
        base_url="https://api.alice.example",
        owner_id=user_a.id,
        enabled=True,
    )
    db_session.add(prov_a)
    await db_session.flush()

    model_a = ModelConfig(
        provider_id=prov_a.id,
        model_name="alice-gpt4",
        display_name="Alice GPT-4",
        is_default=True,
        enabled=True,
    )
    db_session.add(model_a)

    # 3. User B private provider & default model
    prov_b = ModelProvider(
        name="Bob Provider",
        provider_type="openai_compatible",
        base_url="https://api.bob.example",
        owner_id=user_b.id,
        enabled=True,
    )
    db_session.add(prov_b)
    await db_session.flush()

    model_b = ModelConfig(
        provider_id=prov_b.id,
        model_name="bob-claude",
        display_name="Bob Claude",
        is_default=True,
        enabled=True,
    )
    db_session.add(model_b)
    await db_session.commit()

    token_a = generate_server_token(user_id=user_a.id, username=user_a.username, role=user_a.role)
    token_b = generate_server_token(user_id=user_b.id, username=user_b.username, role=user_b.role)

    # Check 1: DirectChatEngine / WorkflowRAGEngine in /api/v1/chat
    captured_models = []

    from app.routing.direct_chat import DirectChatEngine

    original_direct_init = DirectChatEngine.__init__

    def mock_direct_init(self, db, llm_client=None, llm_model=None, **kwargs):
        captured_models.append((llm_model, llm_client))
        original_direct_init(self, db, llm_client=llm_client, llm_model=llm_model, **kwargs)

    with patch.object(DirectChatEngine, "__init__", mock_direct_init):
        # Alice requests chat (without specifying model_id)
        res_a = await client.post(
            "/api/v1/chat/completions",
            json={"query": "Hello from Alice", "mode": "direct"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert res_a.status_code == 200
        assert captured_models[-1][0] == "alice-gpt4", "Alice's request did not resolve Alice's private default model!"

        # Bob requests chat (without specifying model_id)
        res_b = await client.post(
            "/api/v1/chat/completions",
            json={"query": "Hello from Bob", "mode": "direct"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert res_b.status_code == 200
        assert captured_models[-1][0] == "bob-claude", "Bob's request did not resolve Bob's private default model!"

    # Check 2: LangGraphAgentRuntime endpoint in /api/v1/agents/{agent_id}/runs/stream
    # Create dummy AgentDefinition for testing
    import json
    from app.models.agent import AgentDefinition
    agent_def = AgentDefinition(
        name="Test Agent",
        description="assistant",
        system_prompt="You are a helpful assistant",
        enabled_tools=json.dumps(["search_knowledge_base"]),
        owner_id=None,
    )
    db_session.add(agent_def)
    await db_session.commit()
    await db_session.refresh(agent_def)

    with patch("app.api.v1.agent.resolve_llm_client", wraps=resolve_llm_client) as mock_agent_resolve:
        # Alice requests agent run stream
        run_res_a = await client.post(
            f"/api/v1/agents/{agent_def.id}/runs/stream",
            json={"input": "Analyze task for Alice"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert run_res_a.status_code == 200

        # Verify resolve_llm_client received Alice's user_id and resolved Alice's model
        call_a = mock_agent_resolve.call_args_list[-1]
        assert call_a.kwargs["user_id"] == user_a.id

        # Bob requests agent run stream
        run_res_b = await client.post(
            f"/api/v1/agents/{agent_def.id}/runs/stream",
            json={"input": "Analyze task for Bob"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert run_res_b.status_code == 200

        # Verify resolve_llm_client received Bob's user_id and resolved Bob's model
        call_b = mock_agent_resolve.call_args_list[-1]
        assert call_b.kwargs["user_id"] == user_b.id


@pytest.mark.asyncio
async def test_ssrf_https_local_server_sni_and_host_preservation():
    """
    Verify that when SSRFSafeBackend.connect_tcp connects directly to verified_ip,
    the TLS SNI (server_hostname) and HTTP Host header are still strictly preserved
    as the original domain name.
    """
    from datetime import datetime, timedelta, timezone
    import ssl
    import tempfile
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app.core.ssrf import SSRFSafeTransport

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "secure.target.local")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1001)
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("secure.target.local")]), critical=False)
        .sign(key, hashes.SHA256())
    )

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cert_f, \
         tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as key_f:
        cert_f.write(cert.public_bytes(serialization.Encoding.PEM))
        cert_f.flush()
        key_f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        key_f.flush()
        cert_path, key_path = cert_f.name, key_f.name

    captured = {}

    def sni_callback(ssl_sock, server_name, initial_ctx):
        captured["sni"] = server_name
        return None

    ssl_srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_srv_ctx.load_cert_chain(cert_path, key_path)
    ssl_srv_ctx.sni_callback = sni_callback

    async def handle_https(reader, writer):
        data = await reader.read(4096)
        req = data.decode("latin1", errors="replace")
        for line in req.splitlines():
            if line.lower().startswith("host:"):
                captured["host_header"] = line.split(":", 1)[1].strip()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_https, "127.0.0.1", 0, ssl=ssl_srv_ctx)
    port = server.sockets[0].getsockname()[1]

    client_ssl_ctx = ssl.create_default_context()
    client_ssl_ctx.load_verify_locations(cert_path)
    client_ssl_ctx.check_hostname = True

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "secure.target.local":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    socket.getaddrinfo = mock_getaddrinfo
    try:
        transport = SSRFSafeTransport(allow_local=True, verify=client_ssl_ctx)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get(f"https://secure.target.local:{port}/ping")
            assert resp.status_code == 200
            assert resp.text == "OK"
            assert captured.get("sni") == "secure.target.local"
            assert captured.get("host_header") == f"secure.target.local:{port}"
    finally:
        socket.getaddrinfo = orig_getaddrinfo
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ssrf_proxy_environment_bypass_immunity(monkeypatch):
    """
    Verify that environmental proxy settings (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY)
    cannot bypass or hijack SSRF protection. get_ssrf_safe_async_client enforces
    trust_env=False and SSRFSafeTransport ensures direct IP inspection.
    """
    monkeypatch.setenv("HTTP_PROXY", "http://10.254.254.1:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.254.254.1:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://10.254.254.1:1080")

    client = get_ssrf_safe_async_client(allow_local=False)
    async with client:
        # 1. Verify trust_env is disabled
        assert client.trust_env is False

        # 2. Verify forbidden target is blocked by SSRFSafeBackend, NOT forwarded to proxy
        with pytest.raises(SSRFBlockedException) as exc_info:
            await client.get("http://192.168.1.100:8000/models")
        assert "Security Policy Restriction" in str(exc_info.value.detail) or "SSRF" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_redirect_relative_and_protocol_relative_handling():
    """
    Verify that relative path redirects (/relative, ../path) and protocol-relative
    redirects (//127.0.0.1, //169.254.169.254) are correctly resolved and validated.
    """
    # 1. Normal relative redirect resolved on public URL remains valid
    base_public = httpx.URL("http://api.example.com/v1/chat")
    rel_joined = str(base_public.join("/v1/models"))

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "api.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    socket.getaddrinfo = mock_getaddrinfo
    try:
        clean_url, ips = validate_url_ssrf(rel_joined, allow_local=False)
        assert clean_url == "http://api.example.com/v1/models"
    finally:
        socket.getaddrinfo = orig_getaddrinfo

    # 2. Protocol-relative redirect to localhost is blocked in server mode
    proto_rel_local = str(base_public.join("//127.0.0.1/admin"))
    assert proto_rel_local == "http://127.0.0.1/admin"
    with pytest.raises(SSRFBlockedException):
        validate_url_ssrf(proto_rel_local, allow_local=False)

    # 3. Protocol-relative redirect to cloud metadata is blocked in all modes
    proto_rel_meta = str(base_public.join("//169.254.169.254/latest/meta-data"))
    assert proto_rel_meta == "http://169.254.169.254/latest/meta-data"
    with pytest.raises(SSRFBlockedException):
        validate_url_ssrf(proto_rel_meta, allow_local=True)
    with pytest.raises(SSRFBlockedException):
        validate_url_ssrf(proto_rel_meta, allow_local=False)


@pytest.mark.asyncio
async def test_cross_worker_db_concurrency_default_models():
    """
    Simulate multiple independent Uvicorn worker processes modifying default models
    concurrently using separate database sessions/connections and transactions.
    Verifies database-level lock coordination, transactional atomicity,
    and guarantees exactly one default model per tenant and system scope.
    """
    import os
    import tempfile
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.api.v1.models import _acquire_model_default_lock, _clear_other_defaults
    from app.db.session import Base

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(url, connect_args={"timeout": 30})
        factory = async_sessionmaker(bind=engine, class_=AsyncSession)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with factory() as seed_session:
            user1 = User(id="worker_u1", username="worker_u1", password_hash="h1", role=UserRole.USER, is_active=True)
            user2 = User(id="worker_u2", username="worker_u2", password_hash="h2", role=UserRole.USER, is_active=True)
            sys_prov = ModelProvider(id="sys_p", name="Sys Prov", provider_type="openai_compatible", base_url="https://api.sys.example", owner_id=None, enabled=True)
            u1_prov = ModelProvider(id="u1_p", name="U1 Prov", provider_type="openai_compatible", base_url="https://api.u1.example", owner_id=user1.id, enabled=True)
            u2_prov = ModelProvider(id="u2_p", name="U2 Prov", provider_type="openai_compatible", base_url="https://api.u2.example", owner_id=user2.id, enabled=True)
            seed_session.add_all([user1, user2, sys_prov, u1_prov, u2_prov])

            sys_models = [ModelConfig(id=f"sm_{i}", provider_id="sys_p", model_name=f"sys-m{i}", display_name=f"Sys {i}", is_default=(i == 0), enabled=True) for i in range(3)]
            u1_models = [ModelConfig(id=f"u1m_{i}", provider_id="u1_p", model_name=f"u1-m{i}", display_name=f"U1 {i}", is_default=(i == 0), enabled=True) for i in range(3)]
            u2_models = [ModelConfig(id=f"u2m_{i}", provider_id="u2_p", model_name=f"u2-m{i}", display_name=f"U2 {i}", is_default=(i == 0), enabled=True) for i in range(3)]
            seed_session.add_all(sys_models + u1_models + u2_models)
            await seed_session.commit()

        # Simulate separate Uvicorn worker setting default in distinct DB transactions
        async def _worker_set_default(provider_owner_id: str | None, target_model_id: str):
            async with factory() as session:
                await _acquire_model_default_lock(session, provider_owner_id)
                await _clear_other_defaults(session, provider_owner_id)
                cfg = await session.get(ModelConfig, target_model_id)
                cfg.is_default = True
                await session.commit()

        # Launch 12 concurrent worker tasks across different tenants and system scope
        tasks = [
            _worker_set_default("worker_u1", "u1m_1"),
            _worker_set_default("worker_u1", "u1m_2"),
            _worker_set_default("worker_u1", "u1m_0"),
            _worker_set_default("worker_u1", "u1m_2"),
            _worker_set_default("worker_u2", "u2m_2"),
            _worker_set_default("worker_u2", "u2m_0"),
            _worker_set_default("worker_u2", "u2m_1"),
            _worker_set_default("worker_u2", "u2m_0"),
            _worker_set_default(None, "sm_1"),
            _worker_set_default(None, "sm_2"),
            _worker_set_default(None, "sm_0"),
            _worker_set_default(None, "sm_1"),
        ]
        await asyncio.gather(*tasks)

        # Verify results in fresh session
        async with factory() as verify_session:
            u1_count_res = await verify_session.execute(
                select(func.count(ModelConfig.id))
                .join(ModelProvider)
                .where(ModelProvider.owner_id == "worker_u1", ModelConfig.is_default == True)
            )
            assert u1_count_res.scalar() == 1, "Tenant 1 must have exactly 1 default model"

            u2_count_res = await verify_session.execute(
                select(func.count(ModelConfig.id))
                .join(ModelProvider)
                .where(ModelProvider.owner_id == "worker_u2", ModelConfig.is_default == True)
            )
            assert u2_count_res.scalar() == 1, "Tenant 2 must have exactly 1 default model"

            sys_count_res = await verify_session.execute(
                select(func.count(ModelConfig.id))
                .join(ModelProvider)
                .where(ModelProvider.owner_id.is_(None), ModelConfig.is_default == True)
            )
            assert sys_count_res.scalar() == 1, "System public must have exactly 1 default model"
    finally:
        await engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)

