import json
import time
from pathlib import Path
import httpx

BASE_URL = "http://127.0.0.1:8000/api/v1"
client = httpx.Client(base_url=BASE_URL, timeout=60.0, trust_env=False)


def run_live_verification():
    print("\n========== [1] Verify Health ==========")
    r = client.get("/health")
    assert r.status_code == 200, f"Health check failed: {r.text}"
    health_data = r.json()
    print("Health Status:", json.dumps(health_data, indent=2, ensure_ascii=False))
    assert health_data["embedding_provider"] == "local_bge"
    assert health_data["embedding_dimensions"] == 512

    print("\n========== [2] Create Test Knowledge Base ==========")
    r = client.post(
        "/knowledge-bases",
        json={
            "name": f"M1.5验证库-{int(time.time())}",
            "description": "用于验证真实 BGE 语义向量检索与动态重索引",
            "chunk_size": 300,
            "chunk_overlap": 30,
        },
    )
    assert r.status_code == 201, f"Create KB failed: {r.text}"
    kb = r.json()
    kb_id = kb["id"]
    print(f"Created KB: id={kb_id}, name={kb['name']}, provider={kb['embedding_provider']}, dims={kb['embedding_dimensions']}")

    print("\n========== [3] Upload enterprise_architecture_spec.pdf ==========")
    pdf_path = Path("backend/data/sample_docs/enterprise_architecture_spec.pdf")
    with open(pdf_path, "rb") as f:
        files = {"file": ("enterprise_architecture_spec.pdf", f, "application/pdf")}
        r = client.post(f"/knowledge-bases/{kb_id}/documents/upload", files=files)
    assert r.status_code == 200, f"Upload document failed: {r.text}"
    doc = r.json()
    print(f"Uploaded & Ingested Document: id={doc['id']}, status={doc['status']}, chunk_count={doc['chunk_count']}")
    assert doc["status"] == "COMPLETED"
    assert doc["chunk_count"] > 0

    print("\n========== [4] Verify Chunks & 512-dim Embeddings ==========")
    r = client.get(f"/documents/{doc['id']}/chunks?page=1&size=10")
    assert r.status_code == 200
    chunks_page = r.json()
    print(f"Document has {chunks_page['total']} chunks:")
    for c in chunks_page["items"]:
        print(f"  Chunk {c['chunk_index']} (Page {c['page_number']}, {c['char_length']} chars): {c['content'][:50]}...")

    print("\n========== [5] Execute Workflow RAG Query with DeepSeek ==========")
    query_text = "熔断器在 HALF_OPEN 半开状态下的恢复机制与超时时长是多少？"
    chat_payload = {
        "knowledge_base_id": kb_id,
        "query": query_text,
        "mode": "workflow",
        "top_k": 3,
    }
    t0 = time.perf_counter()
    r = client.post("/chat/completions", json=chat_payload)
    latency_ms = (time.perf_counter() - t0) * 1000
    assert r.status_code == 200, f"Chat failed: {r.text}"
    chat_res = r.json()
    print(f"Question: {query_text}")
    print(f"Answer:\n{chat_res['answer']}")
    print(f"Citations count: {len(chat_res['citations'])}")
    for cit in chat_res["citations"]:
        print(f"  Citation [Ref-{cit['citation_index']}]: chunk_id={cit['chunk_id']}, doc={cit['document_name']}, page={cit['page_number']}, score={cit['similarity_score']}")
    print(f"Execution Info: {json.dumps(chat_res['execution_info'], indent=2, ensure_ascii=False)}")
    print(f"Roundtrip latency: {latency_ms:.1f}ms")

    print("\n========== [6] Test Reindex API: local_bge -> test_double ==========")
    r = client.post(
        f"/knowledge-bases/{kb_id}/reindex",
        json={"target_provider": "test_double"},
    )
    assert r.status_code == 200, f"Reindex to test_double failed: {r.text}"
    reindex_data = r.json()
    print("Reindex result:", json.dumps(reindex_data, indent=2, ensure_ascii=False))
    assert reindex_data["previous_provider"] == "local_bge"
    assert reindex_data["new_provider"] == "test_double"
    assert reindex_data["new_dimensions"] == 1024
    assert reindex_data["new_index_version"] == 2

    print("\n========== [7] Verify Model Mismatch Interception (HTTP 409) ==========")
    # Server is running local_bge, but KB index was reindexed to test_double 1024d!
    r = client.post("/chat/completions", json=chat_payload)
    print(f"Querying with mismatched model returned status code: {r.status_code}")
    assert r.status_code == 409, f"Expected 409 Conflict, got {r.status_code}: {r.text}"
    err_detail = r.json().get("detail", "")
    print(f"Mismatch Error Detail: {err_detail}")
    assert "Vector model mismatch" in err_detail
    assert "Please trigger reindex" in err_detail

    print("\n========== [8] Reindex back to local_bge 512d ==========")
    r = client.post(
        f"/knowledge-bases/{kb_id}/reindex",
        json={"target_provider": "local_bge", "target_model": "BAAI/bge-small-zh-v1.5"},
    )
    assert r.status_code == 200, f"Reindex back to local_bge failed: {r.text}"
    reindex_back = r.json()
    print("Reindex back result:", json.dumps(reindex_back, indent=2, ensure_ascii=False))
    assert reindex_back["new_provider"] == "local_bge"
    assert reindex_back["new_dimensions"] == 512
    assert reindex_back["new_index_version"] == 3

    print("\n========== [9] Query again after recovery ==========")
    r = client.post("/chat/completions", json=chat_payload)
    assert r.status_code == 200, f"Chat failed after recovery: {r.text}"
    print("Query successfully executed after recovery!")
    print(f"Answer summary: {r.json()['answer'][:100]}...")

    print("\n========== [10] Clean up test KB ==========")
    r = client.delete(f"/knowledge-bases/{kb_id}")
    assert r.status_code == 204
    print(f"Test KB {kb_id} deleted cleanly.")

    print("\nALL 10 M1.5 LIVE VERIFICATION STEPS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_live_verification()
