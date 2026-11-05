import json
import sys
import time
from pathlib import Path
import httpx

sys.stdout.reconfigure(encoding="utf-8")


def main():
    base_url = "http://127.0.0.1:8000"
    client = httpx.Client(base_url=base_url, timeout=90.0, trust_env=False)

    print("==================================================================")
    print("      AgentStudio-RAG 真实 RAG 问答质量与防幻觉评测 (6 类问题)")
    print("==================================================================")

    # 1. 创建测试知识库并上传企业规范文档
    kb_res = client.post("/api/v1/knowledge-bases", json={
        "name": "M1-RAG评测知识库",
        "description": "企业架构与高可用规范评测集",
        "chunk_size": 400,
        "chunk_overlap": 40,
    }).json()
    kb_id = kb_res["id"]

    pdf_path = Path("backend/data/sample_docs/enterprise_architecture_spec.pdf")
    with open(pdf_path, "rb") as f:
        client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
            files={"file": (pdf_path.name, f, "application/pdf")},
        )

    # 创建一个独立的空知识库用于空库测试
    empty_kb_res = client.post("/api/v1/knowledge-bases", json={
        "name": "空知识库对照组",
        "description": "无任何文档",
    }).json()
    empty_kb_id = empty_kb_res["id"]

    test_cases = [
        {
            "category": "1. 明确事实检索 (单个 Chunk 明确答案)",
            "kb_id": kb_id,
            "query": "网关连接池的最小空闲连接数和最大连接数分别配置为多少？",
            "expected_behavior": "准确检索到第1页网关连接池配置，并回答最小10、最大100。",
        },
        {
            "category": "2. 多跳/跨分块综合问答 (需结合两个 Chunk)",
            "kb_id": kb_id,
            "query": "请综合说明系统在微服务网关接口超时时限上的规定，以及大模型服务在什么条件下会触发熔断进入 OPEN 状态？",
            "expected_behavior": "结合第1页(网关超时5000ms)和第2页(熔断连续失败5次或失败率超30%)综合回答。",
        },
        {
            "category": "3. 专有名词与精确数值检索",
            "kb_id": kb_id,
            "query": "ModelHealthStore 在 CLOSED 状态下允许的最大失败率阈值是多少？持续保持 CLOSED 的时间窗口是多久？",
            "expected_behavior": "识别专有名词 ModelHealthStore，回答过去60秒内失败率低于15%。",
        },
        {
            "category": "4. 知识库完全不存在答案的问题 (完全不相关领域)",
            "kb_id": kb_id,
            "query": "火星探测飞船的化学推进燃料中液氢与液氧的摩尔配比是多少？",
            "expected_behavior": "命中防幻觉防御：明确告知未找到充分事实证据，拒绝猜测编造。",
        },
        {
            "category": "5. 空知识库提问防御",
            "kb_id": empty_kb_id,
            "query": "系统的熔断器机制是怎样的？",
            "expected_behavior": "直接提示知识库尚未建立任何文档索引，耗时极低且不调用大模型。",
        },
        {
            "category": "6. 主题相似但事实未提及的提问 (诱导性问题)",
            "kb_id": kb_id,
            "query": "微服务接入网关是否支持 GraphQL 查询聚合与客户端 WebSocket 心跳握手？",
            "expected_behavior": "虽然提及微服务网关，但文档未提及 GraphQL/WebSocket，应拒答或指出未提及。",
        },
    ]

    all_results = []

    for tc in test_cases:
        print(f"\n------------------------------------------------------------------")
        print(f"【测试类别】: {tc['category']}")
        print(f"【用户提问】: {tc['query']}")
        print(f"【预期行为】: {tc['expected_behavior']}")

        start_t = time.perf_counter()
        resp = client.post("/api/v1/chat/completions", json={
            "knowledge_base_id": tc["kb_id"],
            "query": tc["query"],
            "top_k": 3,
        })
        wall_time = (time.perf_counter() - start_t) * 1000

        assert resp.status_code == 200, f"Error: {resp.text}"
        data = resp.json()

        exec_info = data["execution_info"]
        citations = data["citations"]
        answer = data["answer"]

        print(f"\n【回答摘要】:\n{answer}")
        print(f"\n【真实引用】: {len(citations)} 处")
        for c in citations:
            print(f"  - [Ref-{c['citation_index']}] {c['document_name']} · P.{c['page_number']} | 相似度: {c['similarity_score']}")
            print(f"    证据: {c['content_snippet'][:80]}...")

        print(f"\n【性能指标】: 检索耗时={exec_info['retrieval_duration_ms']} ms | LLM耗时={exec_info['generation_duration_ms']} ms | 总耗时={exec_info['total_duration_ms']} ms | 召回分块={exec_info['retrieved_chunks_count']}")

        result_record = {
            "category": tc["category"],
            "query": tc["query"],
            "expected": tc["expected_behavior"],
            "answer": answer,
            "citations": citations,
            "exec_info": exec_info,
            "retrieved_count": exec_info["retrieved_chunks_count"],
        }
        all_results.append(result_record)

    # 导出为 JSON 供报告引用
    with open("docs/rag_eval_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print("\n✅ 所有测试记录已保存至 docs/rag_eval_results.json")

    # 清理测试库
    client.delete(f"/api/v1/knowledge-bases/{kb_id}")
    client.delete(f"/api/v1/knowledge-bases/{empty_kb_id}")


if __name__ == "__main__":
    main()
