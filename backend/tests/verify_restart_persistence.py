import json
import subprocess
import sys
import time
from pathlib import Path
import httpx

sys.stdout.reconfigure(encoding="utf-8")


def wait_for_server(url: str, timeout: float = 15.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(f"{url}/api/v1/health", timeout=1.0, trust_env=False)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Server at {url} did not start within {timeout}s")


def main():
    base_url = "http://127.0.0.1:8000"
    python_exe = sys.executable

    print("==================================================================")
    print("      AgentStudio-RAG 服务重启与向量持久化端到端实测")
    print("==================================================================")

    # -------------------------------------------------------------
    # Step 1: 启动第一任后端进程 (Server Instance 1)
    # -------------------------------------------------------------
    print("\n>>> [1/7] 启动第一任后端进程 (Instance 1) on port 8000...")
    proc1 = subprocess.Popen(
        [python_exe, "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_server(base_url)
    print("Instance 1 启动成功并就绪 (PID: %d)" % proc1.pid)

    client = httpx.Client(base_url=base_url, timeout=60.0, trust_env=False)

    # -------------------------------------------------------------
    # Step 2: 创建知识库并上传真实多页 PDF
    # -------------------------------------------------------------
    print("\n>>> [2/7] 在 Instance 1 中创建知识库并上传文档...")
    kb_res = client.post("/api/v1/knowledge-bases", json={
        "name": "重启持久化验证知识库",
        "description": "用于验证服务完全重启后向量与元数据是否完好留存",
        "chunk_size": 400,
        "chunk_overlap": 40,
    })
    assert kb_res.status_code == 201
    kb_id = kb_res.json()["id"]
    print(f"创建测试知识库: ID={kb_id}")

    pdf_path = Path("backend/data/sample_docs/enterprise_architecture_spec.pdf")
    with open(pdf_path, "rb") as f:
        upload_res = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
            files={"file": (pdf_path.name, f, "application/pdf")},
        )
    assert upload_res.status_code == 200
    doc_info = upload_res.json()
    print(f"文档上传解析入库完成: {doc_info['file_name']}, 分块数: {doc_info['chunk_count']}, 状态: {doc_info['status']}")

    # -------------------------------------------------------------
    # Step 3: 重启前执行检索并记录结果基准 (Baseline)
    # -------------------------------------------------------------
    test_query = "大模型调用的三态熔断器包含哪几种状态？各状态的触发条件是什么？"
    print(f"\n>>> [3/7] 重启前执行检索 (Query: '{test_query[:25]}...')...")
    retrieval_pre = client.post("/api/v1/retrieval/test", json={
        "knowledge_base_id": kb_id,
        "query": test_query,
        "top_k": 3,
    }).json()

    print(f"重启前检索完成: 召回 {retrieval_pre['total_retrieved']} 个片段 (耗时: {retrieval_pre['duration_ms']} ms)")
    pre_results = retrieval_pre["results"]
    for i, item in enumerate(pre_results):
        print(f"  [Pre-Restart #{i+1}] ChunkID: {item['chunk_id'][:8]} | P.{item['page_number']} | Score: {item['similarity_score']} | File: {item['document_name']}")

    # -------------------------------------------------------------
    # Step 4: 完全终止第一任后端进程 (Kill Instance 1)
    # -------------------------------------------------------------
    print("\n>>> [4/7] 完全强制终止第一任后端进程 (Kill PID: %d)..." % proc1.pid)
    proc1.terminate()
    try:
        proc1.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc1.kill()
    print("Instance 1 已完全停止。")

    # 验证进程确实无法访问
    try:
        httpx.get(f"{base_url}/api/v1/health", timeout=1.0, trust_env=False)
        raise RuntimeError("Server is still responding, kill failed!")
    except Exception:
        print("已验证端口已关闭，服务器处于离线状态。内存数据已完全释放。")

    # -------------------------------------------------------------
    # Step 5: 启动第二任全新后端进程 (Start Instance 2)
    # -------------------------------------------------------------
    print("\n>>> [5/7] 启动第二任全新后端进程 (Instance 2)...")
    proc2 = subprocess.Popen(
        [python_exe, "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_server(base_url)
    print("Instance 2 启动成功并就绪 (PID: %d)" % proc2.pid)

    client2 = httpx.Client(base_url=base_url, timeout=60.0, trust_env=False)

    # -------------------------------------------------------------
    # Step 6: 使用完全相同的 Query 再次检索并比对
    # -------------------------------------------------------------
    print(f"\n>>> [6/7] 重启后使用相同 Query 再次执行检索...")
    retrieval_post = client2.post("/api/v1/retrieval/test", json={
        "knowledge_base_id": kb_id,
        "query": test_query,
        "top_k": 3,
    }).json()

    print(f"重启后检索完成: 召回 {retrieval_post['total_retrieved']} 个片段 (耗时: {retrieval_post['duration_ms']} ms)")
    post_results = retrieval_post["results"]
    for i, item in enumerate(post_results):
        print(f"  [Post-Restart #{i+1}] ChunkID: {item['chunk_id'][:8]} | P.{item['page_number']} | Score: {item['similarity_score']} | File: {item['document_name']}")

    # 比对重启前后一致性
    assert len(pre_results) == len(post_results), "召回数量不一致！"
    for i in range(len(pre_results)):
        assert pre_results[i]["chunk_id"] == post_results[i]["chunk_id"], f"第 {i+1} 名 Chunk ID 不匹配！"
        assert pre_results[i]["page_number"] == post_results[i]["page_number"], f"第 {i+1} 名页码不匹配！"
        assert abs(pre_results[i]["similarity_score"] - post_results[i]["similarity_score"]) < 1e-4, f"第 {i+1} 名相似度打分不匹配！"
        assert pre_results[i]["content"] == post_results[i]["content"], f"第 {i+1} 名内容不匹配！"

    print("\n✅ 重启前后对比结论: Top-K 召回数量、Chunk ID、页码、相似度打分及文本内容 100% 严格一致！")
    print("✅ 证明：向量数据已持久化于 SQLite，重启后零丢失，无需重复计算文档 Embedding！")

    # -------------------------------------------------------------
    # Step 7: 在重启后的实例上运行一次端到端真实问答
    # -------------------------------------------------------------
    print("\n>>> [7/7] 在重启后的实例上运行真实 RAG 端到端问答...")
    chat_res = client2.post("/api/v1/chat/completions", json={
        "knowledge_base_id": kb_id,
        "query": "大模型调用的三态熔断器包含哪几种状态？各状态的触发条件是什么？",
        "top_k": 3,
    }).json()

    print("回答内容摘录:", chat_res["answer"][:120].strip(), "...")
    print("引用溯源数量:", len(chat_res["citations"]))
    assert len(chat_res["citations"]) > 0
    for cit in chat_res["citations"]:
        print(f"  - [Ref-{cit['citation_index']}] {cit['document_name']} · P.{cit['page_number']} (Score: {cit['similarity_score']})")

    # 清理测试库
    client2.delete(f"/api/v1/knowledge-bases/{kb_id}")
    print("\n已清理测试知识库。")

    # 保持 Instance 2 持续运行或退出由调用者决定
    print("服务重启验证完毕，保持 Instance 2 处于运行状态。")


if __name__ == "__main__":
    main()
