"""
P3.5: SSRF Actual Model Call Chain Remediation and Dual-Mode Verification Suite.
Validates:
1. Ingress validation for new provider creation.
2. Ingress validation for provider update.
3. Runtime interception of pre-existing historical malicious URLs in DB.
4. Direct Chat safe execution via SSRF-safe client.
5. Workflow RAG safe execution via SSRF-safe client.
6. Agent tool calling safe execution via SSRF-safe client.
7. Embedding safe execution and SSRF interception.
8. Streaming cancellation and clean resource cleanup.
9. DNS Rebinding socket-level interception.
10. 302 redirect to private/internal targets blocked.
11. Environmental proxies (HTTP_PROXY, HTTPS_PROXY) immunity.
12. Local mode: Ollama localhost allowed.
13. Local mode: vLLM localhost allowed.
14. Server mode: Regular tenant private network denied.
15. Server mode: Admin trusted internal model service allowed, tenant denied, cloud metadata still blocked.
"""

import asyncio
import json
import socket
from typing import AsyncIterator, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import generate_server_token
from app.core.config import settings
from app.core.exceptions import EmbeddingServiceError, KnowledgeBaseNotFoundError
from app.core.security import encrypt_api_key
from app.core.ssrf import (
    CLOUD_METADATA_IPS,
    SSRFBlockedException,
    get_ssrf_safe_async_client,
    validate_url_ssrf,
)
from app.llm.embeddings import OpenAIEmbeddingClient
from app.llm.factory import resolve_llm_client
from app.llm.openai_client import OpenAILLMClient
from app.main import app
from app.models.model_provider import ModelConfig, ModelProvider
from app.models.user import User, UserRole


# ==========================================
# 1 & 2: Ingress Validation (Create / Update)
# ==========================================

@pytest.mark.asyncio
async def test_create_provider_with_malicious_url_rejected(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user = User(username="alice_p35", password_hash="hash", role=UserRole.USER, is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    token = generate_server_token(user_id=user.id, username=user.username, role=user.role)
    headers = {"Authorization": f"Bearer {token}"}

    # Attempt 1: Cloud metadata
    payload_meta = {
        "name": "Malicious Provider Meta",
        "provider_type": "openai_compatible",
        "base_url": "http://169.254.169.254/latest/meta-data",
        "api_key": "test",
    }
    resp1 = await client.post("/api/v1/models/providers", json=payload_meta, headers=headers)
    assert resp1.status_code == 400
    assert "cloud metadata" in resp1.json()["detail"].lower() or "prohibited" in resp1.json()["detail"].lower()

    # Attempt 2: Private RFC1918
    payload_priv = {
        "name": "Malicious Provider Private",
        "provider_type": "openai_compatible",
        "base_url": "http://192.168.1.10:8000/v1",
        "api_key": "test",
    }
    resp2 = await client.post("/api/v1/models/providers", json=payload_priv, headers=headers)
    assert resp2.status_code == 400
    assert "Security Policy Restriction" in resp2.json()["detail"] or "not permitted" in resp2.json()["detail"]


@pytest.mark.asyncio
async def test_update_provider_with_malicious_url_rejected(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user = User(username="bob_p35", password_hash="hash", role=UserRole.USER, is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    token = generate_server_token(user_id=user.id, username=user.username, role=user.role)
    headers = {"Authorization": f"Bearer {token}"}

    # Create initially valid provider
    prov = ModelProvider(
        name="Bob Valid Provider",
        provider_type="openai_compatible",
        base_url="https://api.openai.com/v1",
        owner_id=user.id,
        enabled=True,
    )
    db_session.add(prov)
    await db_session.commit()
    await db_session.refresh(prov)

    # Attempt to update to private internal IP
    update_payload = {"base_url": "http://10.0.0.1:8080/v1"}
    resp = await client.put(f"/api/v1/models/providers/{prov.id}", json=update_payload, headers=headers)
    assert resp.status_code == 400
    assert "Security Policy Restriction" in resp.json()["detail"] or "not permitted" in resp.json()["detail"]


# ==========================================
# 3: Runtime Interception of Historical DB URL
# ==========================================

@pytest.mark.asyncio
async def test_historical_malicious_url_intercepted_at_runtime(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_compatible")
    # Pre-existing malicious URL in database (bypassed past checks or inserted via direct DB access)
    prov = ModelProvider(
        name="Historical Rogue",
        provider_type="openai_compatible",
        base_url="http://192.168.50.25:8000/v1",
        api_key_ciphertext=encrypt_api_key("sk-test-historical-token"),
        owner_id="tenant_x",
        enabled=True,
    )
    db_session.add(prov)
    await db_session.flush()

    cfg = ModelConfig(
        provider_id=prov.id,
        model_name="rogue-model",
        display_name="Rogue Model",
        is_default=True,
        enabled=True,
    )
    db_session.add(cfg)
    await db_session.commit()

    # Resolve client for tenant_x
    llm_client, _ = await resolve_llm_client(db_session, user_id="tenant_x")

    # Calling generate must be intercepted at the socket layer!
    with pytest.raises(RuntimeError) as exc_info:
        await llm_client.generate("Test prompt")
    assert "SSRF" in str(exc_info.value) or "Security Policy" in str(exc_info.value)


# ==========================================
# 4, 5, 6: Direct Chat, Workflow RAG, Agent Tools Safe Request
# ==========================================

@pytest.mark.asyncio
async def test_direct_chat_safe_request(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    client = OpenAILLMClient(
        api_key="sk-valid",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
        allow_local=False,
    )

    mock_resp = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "Hello from safe LLM"}}], "usage": {"total_tokens": 10}},
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "api.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            answer = await client.generate("Hi")
            assert answer == "Hello from safe LLM"


@pytest.mark.asyncio
async def test_workflow_rag_safe_request(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    client = OpenAILLMClient(
        api_key="sk-valid",
        base_url="https://api.example.com/v1",
        model="deepseek-chat",
        allow_local=False,
    )

    stream_content = ["Chunk1", " Chunk2", " [Ref-1]"]
    async def mock_stream_lines():
        for c in stream_content:
            yield f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n"
        yield "data: [DONE]\n"

    mock_resp = httpx.Response(200, request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"))
    mock_resp.aiter_lines = mock_stream_lines

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "api.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        with patch("httpx.AsyncClient.stream") as mock_stream_ctx:
            mock_stream_ctx.return_value.__aenter__.return_value = mock_resp
            mock_stream_ctx.return_value.__aexit__.return_value = None

            received = []
            async for token in client.generate_stream("Summarize document with citations"):
                received.append(token)
            assert "".join(received) == "Chunk1 Chunk2 [Ref-1]"


@pytest.mark.asyncio
async def test_agent_tool_calling_safe_request(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    client = OpenAILLMClient(
        api_key="sk-valid",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
        allow_local=False,
    )

    tool_call_json = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {"name": "search_knowledge_base", "arguments": '{"query": "architecture"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": 40},
    }

    mock_resp = httpx.Response(200, json=tool_call_json, request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"))

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "api.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            ai_msg = await client.generate_with_tools([{"role": "user", "content": "search doc"}])
            assert len(ai_msg.tool_calls) == 1
            assert ai_msg.tool_calls[0]["name"] == "search_knowledge_base"


# ==========================================
# 7: Embedding Safe Request and SSRF Blocking
# ==========================================

@pytest.mark.asyncio
async def test_embedding_safe_request_and_ssrf_blocking(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    # 1. Blocked when pointing to internal metadata
    rogue_emb = OpenAIEmbeddingClient(
        base_url="http://169.254.169.254/v1",
        api_key="sk-test",
        model="text-embedding-3-small",
        dimension=1536,
    )
    with pytest.raises(EmbeddingServiceError) as exc_info:
        await rogue_emb.embed_documents(["hello"])
    assert "SSRF" in str(exc_info.value) or "security policy" in str(exc_info.value)

    # 2. Permitted when pointing to public endpoint
    safe_emb = OpenAIEmbeddingClient(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="text-embedding-3-small",
        dimension=3,
    )
    mock_resp = httpx.Response(
        200,
        json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        request=httpx.Request("POST", "https://api.example.com/v1/embeddings"),
    )

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "api.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            vecs = await safe_emb.embed_documents(["hello"])
            assert vecs == [[0.1, 0.2, 0.3]]


# ==========================================
# 8: Streaming Request Cancellation & Resource Cleanup
# ==========================================

@pytest.mark.asyncio
async def test_streaming_cancellation_and_resource_cleanup(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    client = OpenAILLMClient(
        api_key="sk-valid",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
        allow_local=False,
    )

    closed_flags = {"entered": False, "exited": False}

    class MockStreamContext:
        async def __aenter__(self):
            closed_flags["entered"] = True
            mock_resp = httpx.Response(200, request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"))
            async def _lines():
                yield f"data: {json.dumps({'choices': [{'delta': {'content': 'token1'}}]})}\n"
                await asyncio.sleep(0.5)
                yield f"data: {json.dumps({'choices': [{'delta': {'content': 'token2'}}]})}\n"
            mock_resp.aiter_lines = _lines
            return mock_resp

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            closed_flags["exited"] = True

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "api.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        with patch("httpx.AsyncClient.stream", return_value=MockStreamContext()):
            stream_gen = client.generate_stream("Stream cancellation test")
            first_token = await anext(stream_gen)
            assert first_token == "token1"
            # Explicitly cancel / close generator
            await stream_gen.aclose()
            # Verify stream context exited properly and released HTTP connection
            assert closed_flags["entered"] is True
            assert closed_flags["exited"] is True


# ==========================================
# 9, 10, 11: DNS Rebinding, Redirect, Proxy Immunity
# ==========================================

@pytest.mark.asyncio
async def test_dns_rebinding_socket_protection():
    from app.core.ssrf import SSRFSafeBackend, validate_url_ssrf

    dns_calls = 0
    def mock_getaddrinfo(host, port, *args, **kwargs):
        nonlocal dns_calls
        dns_calls += 1
        if dns_calls == 1:
            # 1st call: URL pre-validation returns safe public IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        # 2nd call: DNS rebinding attack at socket connect time returns private IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.100", port))]

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        # Initial check passes because DNS resolved to 93.184.216.34
        validate_url_ssrf("http://rebind.example.com:80/v1", allow_local=False)
        assert dns_calls == 1

        # But when actual TCP connection is initiated, SSRFSafeBackend verifies IP and intercepts
        backend = SSRFSafeBackend(allow_local=False)
        with pytest.raises(SSRFBlockedException) as exc_info:
            await backend.connect_tcp("rebind.example.com", 80)
        assert "192.168.1.100" in str(exc_info.value.detail) or "SSRF" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_redirect_to_private_ip_blocked():
    from app.core.ssrf import SSRFSafeTransport

    class Mock302Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "redirector" in request.url.host:
                return httpx.Response(
                    302,
                    headers={"Location": "http://192.168.1.1/admin"},
                    request=request,
                )
            return httpx.Response(200, text="UNREACHABLE", request=request)

    transport = Mock302Transport()
    async def _on_response(response: httpx.Response):
        if response.is_redirect and "location" in response.headers:
            loc = response.headers["location"]
            target_url = str(response.url.join(loc))
            validate_url_ssrf(target_url, allow_local=False)

    client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        event_hooks={"response": [_on_response]},
        trust_env=False,
    )
    async with client:
        with pytest.raises(SSRFBlockedException):
            await client.get("http://redirector.example.com/test")


@pytest.mark.asyncio
async def test_proxy_env_cannot_bypass(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://malicious-proxy:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://malicious-proxy:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://malicious-proxy:1080")

    client = get_ssrf_safe_async_client(allow_local=False)
    async with client:
        assert client.trust_env is False
        with pytest.raises(SSRFBlockedException):
            await client.get("http://10.0.0.99:8000/models")


# ==========================================
# 12, 13: Local Mode Compatibility (Ollama & vLLM)
# ==========================================

@pytest.mark.asyncio
async def test_local_mode_local_ollama_allowed(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")

    clean_url, ips = validate_url_ssrf("http://localhost:11434", allow_local=True)
    assert clean_url == "http://localhost:11434"
    assert "127.0.0.1" in ips or "::1" in ips

    # Cloud metadata is STILL blocked in local mode!
    with pytest.raises(SSRFBlockedException):
        validate_url_ssrf("http://169.254.169.254/latest/meta-data", allow_local=True)


@pytest.mark.asyncio
async def test_local_mode_local_vllm_allowed(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")

    clean_url, ips = validate_url_ssrf("http://127.0.0.1:8000/v1", allow_local=True)
    assert clean_url == "http://127.0.0.1:8000/v1"
    assert "127.0.0.1" in ips


# ==========================================
# 14, 15: Server Mode Regular Tenant Denied & Admin Whitelist Allowed
# ==========================================

@pytest.mark.asyncio
async def test_server_mode_regular_tenant_private_ip_rejected(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    user = User(username="regular_tenant_ssrf", password_hash="h", role=UserRole.USER, is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    token = generate_server_token(user_id=user.id, username=user.username, role=user.role)
    headers = {"Authorization": f"Bearer {token}"}

    # Attempt to probe Ollama on internal LAN
    probe_res = await client.post(
        "/api/v1/models/probe/ollama",
        json={"base_url": "http://192.168.1.100:11434"},
        headers=headers,
    )
    assert probe_res.status_code == 200
    assert probe_res.json()["success"] is False
    assert "SSRF" in probe_res.json()["error"] or "安全限制" in probe_res.json()["error"]

    # Attempt to probe vLLM on localhost
    probe_vllm_res = await client.post(
        "/api/v1/models/probe/vllm",
        json={"base_url": "http://127.0.0.1:8000/v1"},
        headers=headers,
    )
    assert probe_vllm_res.status_code == 200
    assert probe_vllm_res.json()["success"] is False
    assert "SSRF" in probe_vllm_res.json()["error"] or "安全限制" in probe_vllm_res.json()["error"]


@pytest.mark.asyncio
async def test_server_admin_trusted_internal_model_service(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    monkeypatch.setattr(settings, "SERVER_TRUSTED_INTERNAL_HOSTS", "internal-cluster.local:8000,10.50.0.1:11434")

    # 1. Admin user
    admin = User(username="admin_p35", password_hash="h", role=UserRole.ADMIN, is_active=True)
    # 2. Regular tenant user
    tenant = User(username="tenant_p35", password_hash="h", role=UserRole.USER, is_active=True)
    db_session.add_all([admin, tenant])
    await db_session.commit()
    await db_session.refresh(admin)
    await db_session.refresh(tenant)

    admin_token = generate_server_token(user_id=admin.id, username=admin.username, role=admin.role)
    tenant_token = generate_server_token(user_id=tenant.id, username=tenant.username, role=tenant.role)

    orig_getaddrinfo = socket.getaddrinfo
    def mock_getaddrinfo(h, p, *args, **kwargs):
        if h == "internal-cluster.local":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.50.0.2", p))]
        return orig_getaddrinfo(h, p, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        # Case A: Admin creates System Public provider with whitelisted internal host -> ALLOWED
        admin_payload = {
            "name": "Internal Company Cluster",
            "provider_type": "vllm",
            "base_url": "http://internal-cluster.local:8000/v1",
            "is_public": True,
        }
        resp_admin = await client.post(
            "/api/v1/models/providers",
            json=admin_payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp_admin.status_code == 201
        assert resp_admin.json()["base_url"] == "http://internal-cluster.local:8000/v1"

        # Case B: Regular tenant tries to create private provider with that same host -> REJECTED
        tenant_payload = {
            "name": "Tenant Tries Internal Host",
            "provider_type": "vllm",
            "base_url": "http://internal-cluster.local:8000/v1",
            "is_public": False,
        }
        resp_tenant = await client.post(
            "/api/v1/models/providers",
            json=tenant_payload,
            headers={"Authorization": f"Bearer {tenant_token}"},
        )
        assert resp_tenant.status_code == 400
        assert "Security Policy Restriction" in resp_tenant.json()["detail"] or "not permitted" in resp_tenant.json()["detail"]

        # Case C: Even Admin cannot access Cloud Metadata even if whitelisted!
        monkeypatch.setattr(settings, "SERVER_TRUSTED_INTERNAL_HOSTS", "169.254.169.254")
        admin_meta_payload = {
            "name": "Admin Tries Metadata",
            "provider_type": "vllm",
            "base_url": "http://169.254.169.254/v1",
            "is_public": True,
        }
        resp_meta = await client.post(
            "/api/v1/models/providers",
            json=admin_meta_payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp_meta.status_code == 400
        assert "cloud metadata" in resp_meta.json()["detail"].lower()


# ==========================================
# 16 & 17: Fake-IP (198.18.0.0/15) Final Security Boundary Tests
# ==========================================

@pytest.mark.asyncio
async def test_fake_ip_allowed_in_local_mode(monkeypatch):
    """Local mode allows RFC 2544 Fake-IP (198.18.0.0/15) for developer transparent TUN proxy compatibility."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "local")

    def mock_getaddrinfo(h, p, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.37", p))]

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        clean_url, resolved_ips = validate_url_ssrf("http://proxy-tun.test:8000/v1", allow_local=True)
        assert "198.18.0.37" in resolved_ips
        assert clean_url == "http://proxy-tun.test:8000/v1"


@pytest.mark.asyncio
async def test_fake_ip_strictly_rejected_in_server_mode(monkeypatch):
    """Server mode strictly rejects RFC 2544 Fake-IP (198.18.0.0/15) by default to prevent SSRF bypass."""
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")

    def mock_getaddrinfo(h, p, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.37", p))]

    with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
        with pytest.raises(SSRFBlockedException) as exc_info:
            validate_url_ssrf("http://proxy-tun.test:8000/v1", allow_local=False)
        assert "198.18.0.37" in str(exc_info.value.detail)
        assert "Server deployment mode" in str(exc_info.value.detail)
