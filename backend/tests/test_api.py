import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ["ok", "degraded"]
    assert data["service"] == "AgentStudio-RAG"


@pytest.mark.asyncio
async def test_knowledge_base_crud(client: AsyncClient):
    # 1. Create KB
    create_payload = {
        "name": "企业产品白皮书",
        "description": "包含企业核心系统架构与API说明",
        "chunk_size": 400,
        "chunk_overlap": 40,
    }
    res = await client.post("/api/v1/knowledge-bases", json=create_payload)
    assert res.status_code == 201
    kb = res.json()
    kb_id = kb["id"]
    assert kb["name"] == "企业产品白皮书"
    assert kb["document_count"] == 0

    # 2. Get list
    res = await client.get("/api/v1/knowledge-bases")
    assert res.status_code == 200
    kbs = res.json()
    assert any(k["id"] == kb_id for k in kbs)

    # 3. Get single
    res = await client.get(f"/api/v1/knowledge-bases/{kb_id}")
    assert res.status_code == 200
    assert res.json()["id"] == kb_id

    # 4. Upload a document
    file_content = b"# Architecture\n\nAgentStudio-RAG integrates multi-path retrieval with LLM."
    files = {"file": ("architecture.md", file_content, "text/markdown")}
    res = await client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/upload", files=files)
    assert res.status_code == 200
    doc = res.json()
    assert doc["file_name"] == "architecture.md"
    assert doc["status"] == "COMPLETED"
    assert doc["chunk_count"] > 0
    doc_id = doc["id"]

    # 5. Get document chunks
    res = await client.get(f"/api/v1/documents/{doc_id}/chunks")
    assert res.status_code == 200
    chunk_page = res.json()
    assert chunk_page["total"] == doc["chunk_count"]
    assert len(chunk_page["items"]) > 0

    # 6. Test retrieval API
    res = await client.post(
        "/api/v1/retrieval/test",
        json={"knowledge_base_id": kb_id, "query": "multi-path retrieval", "top_k": 2},
    )
    assert res.status_code == 200
    retrieval_data = res.json()
    assert retrieval_data["total_retrieved"] > 0
    assert retrieval_data["results"][0]["document_id"] == doc_id

    # 7. Test chat completions
    res = await client.post(
        "/api/v1/chat/completions",
        json={
            "knowledge_base_id": kb_id,
            "query": "What does AgentStudio-RAG integrate?",
            "top_k": 2,
        },
    )
    assert res.status_code == 200
    chat_data = res.json()
    assert chat_data["answer"] != ""
    assert len(chat_data["citations"]) > 0
    assert chat_data["execution_info"]["total_duration_ms"] > 0

    # 8. Delete document
    res = await client.delete(f"/api/v1/documents/{doc_id}")
    assert res.status_code == 204

    # 9. Delete KB
    res = await client.delete(f"/api/v1/knowledge-bases/{kb_id}")
    assert res.status_code == 204
