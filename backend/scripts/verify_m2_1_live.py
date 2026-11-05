import json
import time
from pathlib import Path
import httpx

BASE_URL = "http://127.0.0.1:8000/api/v1"
client = httpx.Client(base_url=BASE_URL, timeout=60.0, trust_env=False)


def run_m2_1_verification():
    print("\n========== [1] Verify Health ==========")
    r = client.get("/health")
    assert r.status_code == 200, f"Health check failed: {r.text}"
    health_data = r.json()
    print("Health Status:", json.dumps(health_data, indent=2, ensure_ascii=False))
    assert health_data["status"] == "ok"

    print("\n========== [2] Create Test Knowledge Base ==========")
    kb_name = f"M2.1多轮对话验证库-{int(time.time())}"
    r = client.post(
        "/knowledge-bases",
        json={
            "name": kb_name,
            "description": "用于验证M2.1多轮对话管理、上下文装配与隔离",
            "chunk_size": 300,
            "chunk_overlap": 30,
        },
    )
    assert r.status_code == 201, f"Create KB failed: {r.text}"
    kb = r.json()
    kb_id = kb["id"]
    print(f"Created KB: id={kb_id}, name={kb['name']}")

    print("\n========== [3] Upload enterprise_architecture_spec.pdf ==========")
    pdf_path = Path("backend/data/sample_docs/enterprise_architecture_spec.pdf")
    with open(pdf_path, "rb") as f:
        files = {"file": ("enterprise_architecture_spec.pdf", f, "application/pdf")}
        r = client.post(f"/knowledge-bases/{kb_id}/documents/upload", files=files)
    assert r.status_code == 200, f"Upload document failed: {r.text}"
    doc = r.json()
    print(f"Document Ingested: id={doc['id']}, status={doc['status']}, chunk_count={doc['chunk_count']}")
    assert doc["status"] == "COMPLETED"

    print("\n========== [4] Create Conversation A ==========")
    r = client.post(
        "/conversations",
        json={"knowledge_base_id": kb_id, "title": "微服务高可用多轮对话"},
    )
    assert r.status_code == 201, f"Create conversation failed: {r.text}"
    conv_a = r.json()
    conv_a_id = conv_a["id"]
    print(f"Conversation A created: id={conv_a_id}, title={conv_a['title']}")

    print("\n========== [5] Conversation A - Turn 1 (基础提问) ==========")
    q1 = "系统大模型动态熔断规范中，包含哪几种标准状态？"
    r = client.post(
        "/chat/completions",
        json={
            "knowledge_base_id": kb_id,
            "conversation_id": conv_a_id,
            "query": q1,
            "mode": "workflow",
            "top_k": 3,
        },
    )
    assert r.status_code == 200, f"Turn 1 failed: {r.text}"
    res1 = r.json()
    print(f"Q1: {q1}")
    print(f"A1:\n{res1['answer']}")
    print(f"Citations: {len(res1['citations'])} | Exec: {res1['execution_info']['total_duration_ms']}ms")
    assert any("CLOSED" in res1["answer"] for _ in [1])
    assert any("OPEN" in res1["answer"] for _ in [1])
    assert len(res1["citations"]) > 0

    print("\n========== [6] Conversation A - Turn 2 (指代消解与多轮上下文) ==========")
    q2 = "刚才提到的第二种状态（OPEN 熔断开路），在什么具体条件下会被强制触发？"
    r = client.post(
        "/chat/completions",
        json={
            "knowledge_base_id": kb_id,
            "conversation_id": conv_a_id,
            "query": q2,
            "mode": "workflow",
            "top_k": 3,
        },
    )
    assert r.status_code == 200, f"Turn 2 failed: {r.text}"
    res2 = r.json()
    print(f"Q2: {q2}")
    print(f"A2:\n{res2['answer']}")
    print(f"Citations: {len(res2['citations'])} | Exec: {res2['execution_info']['total_duration_ms']}ms")
    # Verify accurate contextual answer (5 times or 30%)
    assert ("5" in res2["answer"] or "30%" in res2["answer"])

    print("\n========== [7] Conversation A - Turn 3 (深层连续追问) ==========")
    q3 = "那么在第三种状态（HALF_OPEN）下，需要连续成功几次才能恢复到第一种状态？"
    r = client.post(
        "/chat/completions",
        json={
            "knowledge_base_id": kb_id,
            "conversation_id": conv_a_id,
            "query": q3,
            "mode": "workflow",
            "top_k": 3,
        },
    )
    assert r.status_code == 200, f"Turn 3 failed: {r.text}"
    res3 = r.json()
    print(f"Q3: {q3}")
    print(f"A3:\n{res3['answer']}")
    print(f"Citations: {len(res3['citations'])} | Exec: {res3['execution_info']['total_duration_ms']}ms")
    assert "3" in res3["answer"] or "三次" in res3["answer"] or "CLOSED" in res3["answer"]

    print("\n========== [8] Verify Conversation A Message History ==========")
    r = client.get(f"/conversations/{conv_a_id}/messages")
    assert r.status_code == 200
    msgs_a = r.json()
    print(f"Conversation A has {len(msgs_a)} messages in history:")
    for idx, m in enumerate(msgs_a):
        print(f"  [{idx+1}] {m['role'].upper()}: {m['content'][:60]}... (citations={len(m['citations'])})")
    assert len(msgs_a) == 6  # 3 turns = 6 messages

    print("\n========== [9] Create Conversation B & Test Isolation ==========")
    r = client.post(
        "/conversations",
        json={"knowledge_base_id": kb_id, "title": "独立会话B-安全测试"},
    )
    assert r.status_code == 201
    conv_b_id = r.json()["id"]

    q_b = "JWT会话令牌的有效期是多长时间？"
    r = client.post(
        "/chat/completions",
        json={
            "knowledge_base_id": kb_id,
            "conversation_id": conv_b_id,
            "query": q_b,
            "mode": "workflow",
            "top_k": 3,
        },
    )
    assert r.status_code == 200
    res_b = r.json()
    print(f"Conv B Query: {q_b}")
    print(f"Conv B Answer:\n{res_b['answer']}")

    # Check isolation
    r_a = client.get(f"/conversations/{conv_a_id}/messages")
    r_b = client.get(f"/conversations/{conv_b_id}/messages")
    assert len(r_a.json()) == 6
    assert len(r_b.json()) == 2
    print("Conversation isolation verified: Conv A has 6 msgs, Conv B has 2 msgs.")

    print("\n========== [10] Test Conversation Deletion ==========")
    r = client.delete(f"/conversations/{conv_b_id}")
    assert r.status_code == 204
    print(f"Deleted Conversation B ({conv_b_id}).")
    # Verify B is gone
    r = client.get(f"/conversations/{conv_b_id}")
    assert r.status_code == 404
    # Verify A remains intact
    r = client.get(f"/conversations/{conv_a_id}")
    assert r.status_code == 200
    assert r.json()["message_count"] == 6

    print("\n========== [11] Clean up test KB ==========")
    r = client.delete(f"/knowledge-bases/{kb_id}")
    assert r.status_code == 204
    print(f"Cleaned up test KB ({kb_id}).")

    print("\nALL 11 M2.1 LIVE VERIFICATION STEPS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_m2_1_verification()
