import json
import sys
from pathlib import Path
import httpx

sys.stdout.reconfigure(encoding="utf-8")


def main():
    base_url = "http://127.0.0.1:8000"
    client = httpx.Client(base_url=base_url, timeout=60.0, trust_env=False)

    # 1. Health check
    print("=== [1/6] 检查系统健康探针 ===")
    r = client.get("/api/v1/health")
    assert r.status_code == 200, f"Health check failed: {r.text}"
    health_data = r.json()
    print("Health Status:", health_data)

    # 2. Create Knowledge Base
    print("\n=== [2/6] 创建企业知识库 ===")
    kb_payload = {
        "name": "企业微服务与大模型规范库",
        "description": "涵盖微服务网关、连接池与大模型三态熔断器规范",
        "chunk_size": 400,
        "chunk_overlap": 40,
    }
    r = client.post("/api/v1/knowledge-bases", json=kb_payload)
    assert r.status_code == 201, f"Create KB failed: {r.text}"
    kb = r.json()
    kb_id = kb["id"]
    print(f"成功创建知识库: {kb['name']} (ID: {kb_id})")

    # 3. Upload real multi-page PDF
    print("\n=== [3/6] 上传并解析真实多页 PDF ===")
    pdf_path = Path("backend/data/sample_docs/enterprise_architecture_spec.pdf")
    with open(pdf_path, "rb") as f:
        files = {"file": (pdf_path.name, f, "application/pdf")}
        r = client.post(f"/api/v1/knowledge-bases/{kb_id}/documents/upload", files=files)
    assert r.status_code == 200, f"Upload failed: {r.text}"
    doc = r.json()
    doc_id = doc["id"]
    print(f"文档上传解析完成: {doc['file_name']}")
    print(f"总页数: {doc['page_count']} 页, 生成分块数: {doc['chunk_count']} 块, 状态: {doc['status']}")

    # 4. View Chunks
    print("\n=== [4/6] 查看文档分块及页码元数据 ===")
    r = client.get(f"/api/v1/documents/{doc_id}/chunks")
    assert r.status_code == 200
    chunks_data = r.json()
    for c in chunks_data["items"]:
        print(f"  - Chunk #{c['chunk_index']} (原始页码: 第 {c['page_number']} 页, 字符数: {c['char_length']})")
        print(f"    内容摘要: {c['content'][:65].strip()}...")

    # 5. Retrieval Debug
    print("\n=== [5/6] 检索调试接口测试 (测试熔断器相关问题) ===")
    retrieval_payload = {
        "knowledge_base_id": kb_id,
        "query": "大模型调用的三态熔断器包含哪几种状态？各状态的触发条件是什么？",
        "top_k": 3,
    }
    r = client.post("/api/v1/retrieval/test", json=retrieval_payload)
    assert r.status_code == 200
    retrieval_res = r.json()
    print(f"检索耗时: {retrieval_res['duration_ms']} ms, 召回数量: {retrieval_res['total_retrieved']}")
    for item in retrieval_res["results"]:
        print(f"  - [Rank] 来源: {item['document_name']} (第 {item['page_number']} 页) | 相似度得分: {item['similarity_score']}")

    # 6. Real Q&A with LLM (DeepSeek)
    print("\n=== [6/6] 真实 RAG 问答端到端测试 (调用 DeepSeek 生成并溯源) ===")
    chat_payload = {
        "knowledge_base_id": kb_id,
        "query": "请详细说明大模型调用三态熔断器的运行机制：CLOSED、OPEN、HALF_OPEN 分别在什么条件下切换？",
        "top_k": 3,
    }
    r = client.post("/api/v1/chat/completions", json=chat_payload)
    assert r.status_code == 200, f"Chat failed: {r.text}"
    chat_res = r.json()
    print("\n--- [DeepSeek AI 生成回答] ---")
    print(chat_res["answer"])
    print("\n--- [真实引用溯源列表 (Citations)] ---")
    for cit in chat_res["citations"]:
        print(f"  [Ref-{cit['citation_index']}] 来源文件: {cit['document_name']} | 原始页码: 第 {cit['page_number']} 页 | 匹配度: {cit['similarity_score']}")
        print(f"    证据摘录: {cit['content_snippet'][:100]}...")
    print("\n--- [执行指标追踪 (Trace)] ---")
    print(json.dumps(chat_res["execution_info"], indent=2, ensure_ascii=False))

    # 7. Non-existent query test
    print("\n=== [附加验证] 测试无答案问题的主动防幻觉拒答 ===")
    chat_unrelated = {
        "knowledge_base_id": kb_id,
        "query": "火星探测飞船的化学推进剂由什么成分构成？",
        "top_k": 3,
    }
    r = client.post("/api/v1/chat/completions", json=chat_unrelated)
    assert r.status_code == 200
    unrelated_res = r.json()
    print("模型/系统回答:", unrelated_res["answer"])
    print("引用数量:", len(unrelated_res["citations"]))
    assert "未找到与您提问相关的充分事实证据" in unrelated_res["answer"]
    print("成功验证防幻觉机制：未编造虚假信息！")


if __name__ == "__main__":
    main()
