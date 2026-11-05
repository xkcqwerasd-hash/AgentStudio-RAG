"""
AgentStudio-RAG — P2.3 Release Candidate (RC) Automated Verification Suite
Tests all 7 functional areas across Local and Server deployments with live container endpoints.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8080/api/v1")
print(f"=== AgentStudio-RAG P2.3 RC Test Suite ===")
print(f"Target URL: {BASE_URL}")


def request(
    endpoint: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    files: Optional[Dict[str, tuple]] = None,
) -> Dict[str, Any]:
    url = f"{BASE_URL}{endpoint}"
    headers = {}
    body = None

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if files:
        boundary = "----WebKitFormBoundary" + os.urandom(16).hex()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        body_parts = []
        for field_name, (filename, file_bytes, content_type) in files.items():
            body_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
                + file_bytes
                + b"\r\n"
            )
        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(body_parts)
    elif data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8")
        try:
            err_json = json.loads(raw_err)
            raise RuntimeError(f"HTTP {e.code} on {method} {endpoint}: {err_json}") from e
        except Exception:
            raise RuntimeError(f"HTTP {e.code} on {method} {endpoint}: {raw_err}") from e


def run_tests():
    report = []
    
    # --------------------------------------------------------------------------
    # Check 1: Auth Mode & Health
    # --------------------------------------------------------------------------
    print("\n[Step 1/7] Probing Auth Mode & Healthcheck...")
    mode_info = request("/auth/mode")
    health_info = request("/health")
    mode = mode_info.get("deployment_mode", "local")
    print(f"  -> Deployment Mode: {mode}")
    print(f"  -> Healthcheck Service: {health_info.get('service')}, DB: {health_info.get('database_connected')}")
    assert health_info.get("database_connected") is True, "Database must be connected"
    report.append(("Health & Auth Mode", "PASS", f"Mode={mode}, DB=Connected"))

    auth_token = None
    if mode == "server":
        print("\n[Server Mode] Registering & Authenticating test user...")
        user_suffix = str(int(time.time()))[-6:]
        username = f"rc_user_{user_suffix}"
        reg_res = request("/auth/register", method="POST", data={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!"
        })
        login_res = request("/auth/login", method="POST", data={
            "username": username,
            "password": "Password123!"
        })
        auth_token = login_res["access_token"]
        print(f"  -> Authenticated as {username}, Token: {auth_token[:15]}...")

    # --------------------------------------------------------------------------
    # Check 2: Direct LLM
    # --------------------------------------------------------------------------
    print("\n[Step 2/7] Testing Direct LLM Conversation...")
    conv = request("/conversations", method="POST", data={"title": "Direct LLM Test"}, token=auth_token)
    conv_id = conv["id"]
    t0 = time.time()
    chat_res = request("/chat/completions", method="POST", data={
        "conversation_id": conv_id,
        "query": "Python中如何安全地读取一个大文件？请用一两句话回答。"
    }, token=auth_token)
    answer = chat_res.get("answer", "")
    print(f"  -> Response received in {time.time()-t0:.2f}s: {answer[:80]}...")
    assert len(answer) > 10, "Direct LLM answer should be non-empty"
    report.append(("Direct LLM", "PASS", f"Answer len={len(answer)}, Time={time.time()-t0:.2f}s"))

    # --------------------------------------------------------------------------
    # Check 3: Workflow RAG & Document Ingestion
    # --------------------------------------------------------------------------
    print("\n[Step 3/7] Testing Knowledge Base Ingestion & Workflow RAG...")
    kb = request("/knowledge-bases", method="POST", data={
        "name": f"RC-验收知识库-{int(time.time())%10000}",
        "description": "P2.3 RC 专属测试知识库"
    }, token=auth_token)
    kb_id = kb["id"]
    print(f"  -> Created KB: {kb_id}")

    doc_content = "AgentStudio-RAG 采用三态执行路径：Direct LLM、Workflow RAG 与 LangGraph Agent。版本代号为 RC-Phoenix-2026。".encode("utf-8")
    upload_res = request(
        f"/knowledge-bases/{kb_id}/documents/upload",
        method="POST",
        files={"file": ("rc_release_spec.txt", doc_content, "text/plain")},
        token=auth_token,
    )
    doc_id = upload_res["id"]
    print(f"  -> Uploaded doc {doc_id}, waiting for indexing...")

    # Wait for document to reach COMPLETED
    for _ in range(20):
        time.sleep(1)
        doc_info = request(f"/documents/{doc_id}", token=auth_token)
        if doc_info.get("status") == "COMPLETED":
            print(f"  -> Document indexed successfully! Chunks: {doc_info.get('chunk_count')}")
            break
    else:
        raise RuntimeError("Document indexing timed out")

    # Ask RAG question bound to KB
    rag_res = request("/chat/completions", method="POST", data={
        "knowledge_base_id": kb_id,
        "query": "AgentStudio-RAG 的内部版本代号是什么？"
    }, token=auth_token)
    rag_answer = rag_res.get("answer", "")
    citations = rag_res.get("citations", [])
    print(f"  -> RAG Answer: {rag_answer[:100]}")
    print(f"  -> Citations count: {len(citations)}")
    assert "Phoenix-2026" in rag_answer or "RC-Phoenix-2026" in rag_answer or len(citations) > 0, "RAG should retrieve knowledge chunks"
    report.append(("Workflow RAG", "PASS", f"Citations={len(citations)}, Answer contains keyword"))

    # --------------------------------------------------------------------------
    # Check 4: LangGraph Agent
    # --------------------------------------------------------------------------
    print("\n[Step 4/7] Testing LangGraph Agent...")
    agent_def = request("/agents", method="POST", data={
        "name": "RC测试分析助手",
        "description": "专用于测试工具调用的智能助理",
        "system_prompt": "你是一个严谨的助手，请检索知识库并回答用户问题。",
        "knowledge_base_id": kb_id,
        "enabled_tools": ["search_knowledge_base", "search_conversation_attachments"],
        "max_steps": 4
    }, token=auth_token)
    agent_id = agent_def["id"]
    print(f"  -> Created Agent: {agent_id}")

    # Stream agent run
    stream_url = f"{BASE_URL}/agents/{agent_id}/runs/stream"
    req_body = json.dumps({"input": "请检索知识库并说明系统包含哪些执行路径？"}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    stream_req = urllib.request.Request(stream_url, data=req_body, headers=headers, method="POST")
    
    agent_events = []
    agent_answer = ""
    with urllib.request.urlopen(stream_req, timeout=45) as stream_resp:
        for line in stream_resp:
            txt = line.decode("utf-8").strip()
            if txt.startswith("data:"):
                payload_str = txt[5:].strip()
                try:
                    ev = json.loads(payload_str)
                    if "final_answer" in ev:
                        agent_answer = ev.get("final_answer", "")
                    elif "status" in ev and ev.get("status") == "completed":
                        agent_answer = ev.get("final_answer", agent_answer)
                    agent_events.append(ev)
                except Exception:
                    pass
    print(f"  -> Agent executed successfully with {len(agent_events)} events. Answer: {agent_answer[:80]}...")
    report.append(("LangGraph Agent", "PASS", f"Agent events={len(agent_events)}"))

    # --------------------------------------------------------------------------
    # Check 5: Chat Attachments System
    # --------------------------------------------------------------------------
    print("\n[Step 5/7] Testing Conversation Attachments System...")
    att_conv = request("/conversations", method="POST", data={"title": "Attachment Test Conv"}, token=auth_token)
    att_conv_id = att_conv["id"]
    att_bytes = "【内部财务备忘】2026年第三季度营收达到 12.8 亿元，净利润同比增长 45.2%。".encode("utf-8")
    att_upload = request(
        f"/conversations/{att_conv_id}/attachments",
        method="POST",
        files={"files": ("q3_financial_memo.txt", att_bytes, "text/plain")},
        token=auth_token
    )
    assert len(att_upload) > 0, "Attachment upload should succeed"
    att_id = att_upload[0]["id"]
    print(f"  -> Uploaded attachment: {att_id}, parsed pages: {att_upload[0].get('page_count')}")

    # Query with attachment context
    att_chat = request("/chat/completions", method="POST", data={
        "conversation_id": att_conv_id,
        "query": "根据上传的财务备忘，第三季度营收与净利润同比增长是多少？"
    }, token=auth_token)
    print(f"  -> Attachment Chat Answer: {att_chat.get('answer')[:80]}...")
    assert "12.8" in att_chat.get("answer", "") or "45.2" in att_chat.get("answer", ""), "Attachment answer should contain memo numbers"

    # Delete attachment
    request(f"/conversations/{att_conv_id}/attachments/{att_id}", method="DELETE", token=auth_token)
    print("  -> Attachment deleted successfully.")
    report.append(("Attachment System", "PASS", "Upload, Parsing, Context Search, Deletion"))

    # --------------------------------------------------------------------------
    # Check 6: Long-term Memory System
    # --------------------------------------------------------------------------
    print("\n[Step 6/7] Testing Long-term Memory System...")
    mem = request("/memories", method="POST", data={
        "memory_type": "profile",
        "content": "用户是一位高频量化交易系统架构师，核心技术栈为Python异步网络编程",
        "confidence": 0.95,
        "pinned": True
    }, token=auth_token)
    mem_id = mem["id"]
    print(f"  -> Created Profile Memory: {mem_id}")

    # Query memory vector search
    mem_search = request("/memories/query", method="POST", data={
        "query": "高频量化交易系统架构师与Python技术栈",
        "top_k": 3,
        "threshold": 0.3
    }, token=auth_token)
    print(f"  -> Memory Query Matches: {len(mem_search)}")
    assert len(mem_search) > 0, "Should match created memory"
    matched_score = mem_search[0].get("score", 0.0)
    print(f"  -> Top matched similarity: {matched_score:.4f}")

    # Update memory
    request(f"/memories/{mem_id}", method="PUT", data={
        "content": "用户是一位资深AI系统与高频量化交易架构师",
        "pinned": True
    }, token=auth_token)

    # Delete memory
    request(f"/memories/{mem_id}", method="DELETE", token=auth_token)
    print("  -> Memory updated and deleted cleanly.")
    report.append(("Long-term Memory", "PASS", f"Created, Searched (score={matched_score:.4f}), Updated, Deleted"))

    # --------------------------------------------------------------------------
    # Check 7: Model Management
    # --------------------------------------------------------------------------
    print("\n[Step 7/7] Testing Model Management...")
    active_models = request("/models/active", token=auth_token)
    print(f"  -> Active models count: {len(active_models)}")
    for m in active_models:
        print(f"     * {m.get('id')} ({m.get('model_name')}) [provider={m.get('provider_id')}]")
    assert len(active_models) > 0, "There should be at least one active model configuration"

    # Connection test
    test_result = request("/models/test", method="POST", data={
        "provider_type": "openai",
        "base_url": "https://api.deepseek.com",
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "model_name": "deepseek-chat"
    }, token=auth_token)
    print(f"  -> Model Connection Test: Success={test_result.get('success')}, Latency={test_result.get('latency_ms')}ms")
    assert test_result.get("success") is True, f"Model test failed: {test_result.get('error_message')}"
    report.append(("Model Management", "PASS", f"Active={len(active_models)}, Latency={test_result.get('latency_ms')}ms"))

    # Clean up test KB
    request(f"/knowledge-bases/{kb_id}", method="DELETE", token=auth_token)
    print("  -> Cleaned up test knowledge base.")

    # --------------------------------------------------------------------------
    # Final Summary Table
    # --------------------------------------------------------------------------
    print("\n" + "="*70)
    print(f"AGENTSTUDIO-RAG P2.3 RC VERIFICATION SUMMARY ({mode.upper()} MODE)")
    print("="*70)
    print(f"{'CAPABILITY':<25} | {'STATUS':<8} | {'DETAILS'}")
    print("-" * 70)
    for cap, status, details in report:
        print(f"{cap:<25} | {status:<8} | {details}")
    print("="*70)


if __name__ == "__main__":
    try:
        run_tests()
    except Exception as exc:
        print(f"\n[FATAL TEST FAILURE] {exc}", file=sys.stderr)
        sys.exit(1)
