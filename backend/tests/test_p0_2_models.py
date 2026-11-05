"""
AgentStudio-RAG — P0.2 Model Management Center Test Suite
Validates:
1. Provider CRUD (Add, List, Update, Delete).
2. ModelConfig CRUD (Add, List, Update, Delete).
3. API Key encryption at rest & masking in API responses.
4. OpenAI Compatible connectivity test.
5. Ollama models probing (/api/tags).
6. vLLM models probing (/v1/models).
7. Workflow RAG runtime model dynamic switching.
8. Agent Tool Calling runtime model dynamic switching.
9. Concurrent requests model isolation (no cross-contamination).
10. Default model switching.
11. Invalid API key (401) and network timeout handling.
12. Safety check for models without Tool Calling capability.
13. Original DeepSeek backward compatibility regression.
"""

import asyncio
import json
import uuid
import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, patch
import httpx

from langchain_core.messages import AIMessage
from app.core.security import decrypt_api_key, encrypt_api_key, mask_api_key
from app.llm.factory import (
    ModelDisabledError,
    ModelNotFoundError,
    ToolCallingNotSupportedError,
    resolve_llm_client,
    validate_tool_calling_capability,
)
from app.llm.manager import (
    probe_ollama_models,
    probe_vllm_models,
    test_model_connectivity as run_model_connectivity_test,
)
from app.llm.openai_client import get_llm_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.models.model_provider import ModelConfig, ModelProvider
from app.schemas.model_provider import ModelTestResponse


@pytest.fixture(autouse=True)
def setup_test_double():
    llm = get_llm_client("test_double")
    if hasattr(llm, "reset"):
        llm.reset()
    yield llm


async def _create_test_kb(db: AsyncSession, name: str = "P0.2测试知识库"):
    from app.llm.embeddings import get_embedding_client

    emb = get_embedding_client()
    kb = KnowledgeBase(
        id=f"kb-{uuid.uuid4().hex[:8]}",
        name=name,
        embedding_provider=emb.provider_name,
        embedding_model=emb.model_name,
        embedding_dimensions=emb.dimensions,
    )
    db.add(kb)
    await db.flush()

    doc = Document(
        id=f"doc-{uuid.uuid4().hex[:8]}",
        knowledge_base_id=kb.id,
        file_name="spec.md",
        file_path="/data/spec.md",
        file_type="markdown",
        file_size=512,
        status="COMPLETED",
    )
    db.add(doc)
    await db.flush()

    chunk = DocumentChunk(
        id=f"chunk-{uuid.uuid4().hex[:8]}",
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="网关最大连接池数量设置为 500，接口超时时间设置为 5000 毫秒。",
        page_number=1,
        char_length=40,
        embedding_json=json.dumps([0.05] * 512),
    )
    db.add(chunk)
    await db.commit()
    return kb, chunk


@pytest.mark.asyncio
async def test_provider_and_model_crud(client: AsyncClient, db_session: AsyncSession):
    """Scenario 1 & 2: Provider and Model Config CRUD operations."""
    # 1. Create Provider
    create_payload = {
        "name": "SiliconFlow 硅基流动",
        "provider_type": "openai_compatible",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "sk-siliconflow-secret-12345678",
        "enabled": True,
        "models": [
            {
                "model_name": "Qwen/Qwen2.5-72B-Instruct",
                "display_name": "通义千问 72B",
                "model_type": "chat",
                "is_default": False,
                "capabilities": {
                    "supports_streaming": True,
                    "supports_tool_calling": True,
                    "supports_vision": False,
                    "context_window": 32768,
                },
            }
        ],
    }
    res = await client.post("/api/v1/models/providers", json=create_payload)
    assert res.status_code == 201
    prov_data = res.json()
    provider_id = prov_data["id"]
    assert prov_data["name"] == "SiliconFlow 硅基流动"
    assert prov_data["has_api_key"] is True
    assert "sk-siliconflow-secret-12345678" not in prov_data["masked_api_key"]
    assert len(prov_data["models"]) == 1
    model_id = prov_data["models"][0]["id"]

    # 2. Add another Model to this Provider
    add_model_payload = {
        "model_name": "deepseek-ai/DeepSeek-V3",
        "display_name": "DeepSeek-V3 (硅基托管)",
        "model_type": "chat",
        "is_default": False,
    }
    res_m = await client.post(f"/api/v1/models/providers/{provider_id}/models", json=add_model_payload)
    assert res_m.status_code == 201
    new_model_id = res_m.json()["id"]

    # 3. Update Model
    up_m_payload = {"display_name": "DeepSeek-V3 Pro"}
    res_up_m = await client.put(f"/api/v1/models/configs/{new_model_id}", json=up_m_payload)
    assert res_up_m.status_code == 200
    assert res_up_m.json()["display_name"] == "DeepSeek-V3 Pro"

    # 4. List Active Models for ChatPage
    res_active = await client.get("/api/v1/models/active")
    assert res_active.status_code == 200
    active_ids = [m["id"] for m in res_active.json()]
    assert model_id in active_ids
    assert new_model_id in active_ids

    # 5. Delete Model
    del_m = await client.delete(f"/api/v1/models/configs/{new_model_id}")
    assert del_m.status_code == 204

    # 6. Delete Provider
    del_p = await client.delete(f"/api/v1/models/providers/{provider_id}")
    assert del_p.status_code == 204


@pytest.mark.asyncio
async def test_api_key_encryption_and_masking(client: AsyncClient, db_session: AsyncSession):
    """Scenario 3: API Key encrypted at rest in DB and masked in API responses."""
    raw_key = "sk-deepseek-super-confidential-key-998877"
    create_payload = {
        "name": "安全测试服务商",
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": raw_key,
        "enabled": True,
    }
    res = await client.post("/api/v1/models/providers", json=create_payload)
    assert res.status_code == 201
    data = res.json()
    provider_id = data["id"]

    # Plain text API key NEVER returned
    assert raw_key not in json.dumps(data)
    assert data["has_api_key"] is True
    assert data["masked_api_key"] == mask_api_key(raw_key)

    # Check database storage
    stmt = select(ModelProvider).where(ModelProvider.id == provider_id)
    prov_in_db = (await db_session.execute(stmt)).scalar_one()
    # Stored value is ciphertext, not plain text
    assert prov_in_db.api_key_ciphertext != raw_key
    assert "confidential" not in prov_in_db.api_key_ciphertext

    # Decryption works with master key
    decrypted = decrypt_api_key(prov_in_db.api_key_ciphertext)
    assert decrypted == raw_key


@pytest.mark.asyncio
async def test_connection_test_endpoint(client: AsyncClient):
    """Scenario 4: OpenAI Compatible connectivity test endpoint."""
    with patch("app.api.v1.models.test_model_connectivity", new_callable=AsyncMock) as mock_test:
        mock_test.return_value = ModelTestResponse(
            success=True,
            latency_ms=125.4,
            reply_snippet="Hello from mock LLM!",
        )
        test_payload = {
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-mock-key-12345",
            "model_name": "gpt-4o",
        }
        res = await client.post("/api/v1/models/test", json=test_payload)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["latency_ms"] == 125.4
        assert "Hello from mock LLM!" in data["reply_snippet"]


@pytest.mark.asyncio
async def test_direct_connectivity_logic():
    """Scenario 4b: Real test_model_connectivity function execution."""
    mock_resp = Response(
        status_code=200,
        json={"choices": [{"message": {"content": "Direct test reply snippet"}}]},
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await run_model_connectivity_test("https://api.test.com", "sk-123", "m-1")
        assert result.success is True
        assert result.latency_ms >= 0
        assert "Direct test reply snippet" in result.reply_snippet


@pytest.mark.asyncio
async def test_ollama_and_vllm_probing(client: AsyncClient):
    """Scenario 5 & 6: Ollama /api/tags and vLLM /v1/models probing."""
    # 1. Test Ollama probing logic directly
    ollama_mock = Response(
        status_code=200,
        json={
            "models": [
                {
                    "name": "llama3:8b",
                    "size": 4920732352,
                    "modified_at": "2026-05-01T12:00:00Z",
                    "details": {
                        "family": "llama",
                        "parameter_size": "8.0B",
                        "quantization_level": "Q4_0",
                    },
                }
            ]
        },
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = ollama_mock
        res = await probe_ollama_models("http://localhost:11434")
        assert res.success is True
        assert len(res.models) == 1
        assert res.models[0].name == "llama3:8b"
        assert res.models[0].family == "llama"

    # 2. Test vLLM probing logic directly
    vllm_mock = Response(
        status_code=200,
        json={"data": [{"id": "meta-llama/Meta-Llama-3-8B-Instruct"}, {"id": "Qwen/Qwen2.5-7B"}]},
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = vllm_mock
        res = await probe_vllm_models("http://localhost:8000/v1")
        assert res.success is True
        assert "Qwen/Qwen2.5-7B" in res.models


@pytest.mark.asyncio
async def test_workflow_and_agent_dynamic_model_switching(client: AsyncClient, db_session: AsyncSession):
    """Scenario 7 & 8: Dynamic model switching in Workflow RAG and Agentic mode."""
    kb, chunk = await _create_test_kb(db_session, "模型切换知识库")

    # Create two models in DB
    prov = ModelProvider(
        name="测试服务商",
        provider_type="openai_compatible",
        base_url="https://api.test.com/v1",
        api_key_ciphertext=encrypt_api_key("sk-test-model-key"),
        enabled=True,
    )
    db_session.add(prov)
    await db_session.flush()

    model_a = ModelConfig(
        provider_id=prov.id,
        model_name="model-alpha-v1",
        display_name="模型 Alpha",
        model_type="chat",
        is_default=True,
        enabled=True,
    )
    model_b = ModelConfig(
        provider_id=prov.id,
        model_name="model-beta-v2",
        display_name="模型 Beta",
        model_type="chat",
        is_default=False,
        enabled=True,
    )
    db_session.add_all([model_a, model_b])
    await db_session.commit()

    # 1. Workflow RAG request with model_b
    wf_payload = {
        "knowledge_base_id": kb.id,
        "query": "查询网关连接配置",
        "model_id": model_b.id,
    }
    wf_res = await client.post("/api/v1/chat/completions", json=wf_payload)
    assert wf_res.status_code == 200
    wf_data = wf_res.json()
    assert wf_data["execution_info"]["llm_model"] == "model-beta-v2"

    # 2. Agentic RAG request with model_b
    agent_payload = {
        "input": "查询网关超时时间",
        "knowledge_base_id": kb.id,
        "model_id": model_b.id,
    }
    agent_res = await client.post("/api/v1/agents/default/runs/stream", json=agent_payload)
    assert agent_res.status_code == 200
    assert "event: run_started" in agent_res.text
    assert "event: run_completed" in agent_res.text


@pytest.mark.asyncio
async def test_concurrent_requests_model_isolation(db_session: AsyncSession):
    """Scenario 9: Concurrent requests with different models resolve independent LLM clients."""
    prov_1 = ModelProvider(
        name="Provider 1",
        provider_type="openai_compatible",
        base_url="https://api.one.com/v1",
        api_key_ciphertext=encrypt_api_key("sk-key-one"),
        enabled=True,
    )
    prov_2 = ModelProvider(
        name="Provider 2",
        provider_type="openai_compatible",
        base_url="https://api.two.com/v1",
        api_key_ciphertext=encrypt_api_key("sk-key-two"),
        enabled=True,
    )
    db_session.add_all([prov_1, prov_2])
    await db_session.flush()

    m1 = ModelConfig(provider_id=prov_1.id, model_name="model-one", display_name="Model 1", enabled=True)
    m2 = ModelConfig(provider_id=prov_2.id, model_name="model-two", display_name="Model 2", enabled=True)
    db_session.add_all([m1, m2])
    await db_session.commit()

    # Resolve concurrently
    async def resolve_task(m_id):
        client, cfg = await resolve_llm_client(db_session, m_id)
        return client, cfg

    (client_1, cfg_1), (client_2, cfg_2) = await asyncio.gather(
        resolve_task(m1.id),
        resolve_task(m2.id),
    )

    assert cfg_1.model_name == "model-one"
    assert cfg_2.model_name == "model-two"


@pytest.mark.asyncio
async def test_default_model_switching(client: AsyncClient, db_session: AsyncSession):
    """Scenario 10: Switching default model clears previous defaults."""
    prov = ModelProvider(
        name="默认切换测试",
        provider_type="openai_compatible",
        base_url="https://api.def.com",
        enabled=True,
    )
    db_session.add(prov)
    await db_session.flush()

    m_old = ModelConfig(provider_id=prov.id, model_name="old-default", display_name="旧默认", is_default=True, enabled=True)
    m_new = ModelConfig(provider_id=prov.id, model_name="new-default", display_name="新默认", is_default=False, enabled=True)
    db_session.add_all([m_old, m_new])
    await db_session.commit()

    # Set new default
    res = await client.post(f"/api/v1/models/configs/{m_new.id}/set-default")
    assert res.status_code == 200
    assert res.json()["is_default"] is True

    # Check DB
    await db_session.refresh(m_old)
    await db_session.refresh(m_new)
    assert m_new.is_default is True
    assert m_old.is_default is False


@pytest.mark.asyncio
async def test_invalid_api_key_and_network_error():
    """Scenario 11: Invalid API key (401) and network timeout handling."""
    # 1. 401 Unauthorized
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = Response(status_code=401, text="Unauthorized")
        result = await run_model_connectivity_test("https://api.ai.com", "sk-invalid", "test")
        assert result.success is False
        assert "401" in result.error or "鉴权失败" in result.error

    # 2. Timeout
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.TimeoutException("Connection timed out")
        result = await run_model_connectivity_test("https://api.ai.com", None, "test")
        assert result.success is False
        assert "超时" in result.error


@pytest.mark.asyncio
async def test_unsupported_tool_calling_safety_check(client: AsyncClient, db_session: AsyncSession):
    """Scenario 12: Models without Tool Calling capability are rejected safely in Agentic mode."""
    kb, chunk = await _create_test_kb(db_session, "无工具调用模型库")

    prov = ModelProvider(name="小模型服务商", provider_type="ollama", base_url="http://localhost:11434", enabled=True)
    db_session.add(prov)
    await db_session.flush()

    no_tc_model = ModelConfig(
        provider_id=prov.id,
        model_name="tiny-llama:1b",
        display_name="TinyLlama 1B (不支持工具调用)",
        model_type="chat",
        capabilities_json=json.dumps({"supports_streaming": True, "supports_tool_calling": False, "supports_vision": False, "context_window": 4096}),
        is_default=False,
        enabled=True,
    )
    db_session.add(no_tc_model)
    await db_session.commit()

    # Attempt to run Agentic stream with this model
    payload = {
        "input": "查询网关配置",
        "knowledge_base_id": kb.id,
        "model_id": no_tc_model.id,
    }
    res = await client.post("/api/v1/agents/default/runs/stream", json=payload)
    # Must fail with 400 Bad Request and descriptive advice
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "不支持原生工具调用" in detail
    assert "快速问答" in detail


@pytest.mark.asyncio
async def test_legacy_deepseek_regression(client: AsyncClient, db_session: AsyncSession):
    """Scenario 13: Backward compatibility regression when model_id is omitted."""
    kb, chunk = await _create_test_kb(db_session, "兼容性测试库")

    # Workflow without model_id uses default
    wf_res = await client.post(
        "/api/v1/chat/completions",
        json={"knowledge_base_id": kb.id, "query": "测试默认模型兼容性"},
    )
    assert wf_res.status_code == 200

    # Agent without model_id uses default
    agent_res = await client.post(
        "/api/v1/agents/default/runs/stream",
        json={"knowledge_base_id": kb.id, "input": "测试默认Agent模型兼容性"},
    )
    assert agent_res.status_code == 200
    assert "event: run_started" in agent_res.text
