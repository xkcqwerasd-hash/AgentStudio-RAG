import json
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple
import numpy as np

from app.ingestion.parsers.pdf_parser import PDFParser
from app.ingestion.splitters.recursive_splitter import RecursiveCharacterTextSplitter
from app.llm.embeddings import LocalBGEEmbeddingClient, TestDoubleEmbeddingClient

# 1. Benchmark Queries & Ground Truth Annotations
BENCHMARK_QUERIES = [
    # 5 Exact keyword queries
    {
        "id": "Q1",
        "category": "直接关键词匹配",
        "query": "网关连接池的最大连接数 (max_connections) 是多少？",
        "ground_truth_chunks": [0],
    },
    {
        "id": "Q2",
        "category": "直接关键词匹配",
        "query": "JWT会话令牌的有效期是多长时间？",
        "ground_truth_chunks": [1],
    },
    {
        "id": "Q3",
        "category": "直接关键词匹配",
        "query": "熔断器在什么条件下会强制切换为 OPEN 状态？",
        "ground_truth_chunks": [2],
    },
    {
        "id": "Q4",
        "category": "直接关键词匹配",
        "query": "熔断器在 HALF_OPEN 半开状态下需要连续成功几次才能恢复？",
        "ground_truth_chunks": [3],
    },
    {
        "id": "Q5",
        "category": "直接关键词匹配",
        "query": "系统大模型调用的降级路由 Failover 包含哪些模型？",
        "ground_truth_chunks": [3],
    },
    # 3 Paraphrase / Semantic queries
    {
        "id": "Q6",
        "category": "同义词与改写提问",
        "query": "当外部 AI 接口频繁报错响应异常时，平台如何实现故障隔离防雪崩？",
        "ground_truth_chunks": [2],
    },
    {
        "id": "Q7",
        "category": "同义词与改写提问",
        "query": "用户登录凭证的过期时间与两步身份核验机制是怎样的？",
        "ground_truth_chunks": [1],
    },
    {
        "id": "Q8",
        "category": "同义词与改写提问",
        "query": "主用语言模型不可用时，系统如何进行后备服务切换？",
        "ground_truth_chunks": [3],
    },
    # 2 Cross-section / Broad semantic queries
    {
        "id": "Q9",
        "category": "跨段落语义问题",
        "query": "企业级微服务网关的核心通信参数与会话持久化方案是什么？",
        "ground_truth_chunks": [0, 1],
    },
    {
        "id": "Q10",
        "category": "跨段落语义问题",
        "query": "系统的模型健康防护状态机有哪些状态，流转机制是怎样的？",
        "ground_truth_chunks": [2, 3],
    },
]


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


async def run_benchmark():
    # 1. Parse and chunk the test PDF
    pdf_path = Path("backend/data/sample_docs/enterprise_architecture_spec.pdf")
    doc = PDFParser().parse(pdf_path)
    splitter = RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=30)

    chunks: List[Tuple[int, str]] = []
    chunk_texts: List[str] = []
    for p in doc.pages:
        for c in splitter.split_text(p.text):
            if c.strip():
                chunks.append((p.page_number, c.strip()))
                chunk_texts.append(c.strip())

    print(f"Loaded {len(chunks)} chunks from {pdf_path.name}")

    # 2. Build embeddings for both clients
    test_double_client = TestDoubleEmbeddingClient(dimension=1024)
    local_bge_client = LocalBGEEmbeddingClient(model_name="BAAI/bge-small-zh-v1.5", dimension=512)

    print("Generating embeddings for test_double...")
    td_doc_vecs = [
        np.array(v, dtype=np.float32)
        for v in await test_double_client.embed_documents(chunk_texts)
    ]

    print("Generating embeddings for local_bge (BGE-small-zh-v1.5)...")
    bge_doc_vecs = [
        np.array(v, dtype=np.float32)
        for v in await local_bge_client.embed_documents(chunk_texts)
    ]

    results_td: List[dict] = []
    results_bge: List[dict] = []

    # 3. Evaluate each query
    for q_item in BENCHMARK_QUERIES:
        qid = q_item["id"]
        q_text = q_item["query"]
        q_cat = q_item["category"]
        ground_truth: Set[int] = set(q_item["ground_truth_chunks"])

        # Evaluate TestDouble
        t0 = time.perf_counter()
        td_q_vec = np.array(await test_double_client.embed_query(q_text), dtype=np.float32)
        td_scores = [
            (idx, cosine_sim(td_q_vec, doc_v)) for idx, doc_v in enumerate(td_doc_vecs)
        ]
        td_scores.sort(key=lambda x: x[1], reverse=True)
        td_latency_ms = (time.perf_counter() - t0) * 1000

        # Evaluate LocalBGE
        t1 = time.perf_counter()
        bge_q_vec = np.array(await local_bge_client.embed_query(q_text), dtype=np.float32)
        bge_scores = [
            (idx, cosine_sim(bge_q_vec, doc_v)) for idx, doc_v in enumerate(bge_doc_vecs)
        ]
        bge_scores.sort(key=lambda x: x[1], reverse=True)
        bge_latency_ms = (time.perf_counter() - t1) * 1000

        def evaluate_ranking(ranking: List[Tuple[int, float]], latency: float):
            top1_hit = ranking[0][0] in ground_truth
            top3_hit = any(r[0] in ground_truth for r in ranking[:3])
            mrr = 0.0
            for rank_idx, (chunk_id, _) in enumerate(ranking):
                if chunk_id in ground_truth:
                    mrr = 1.0 / (rank_idx + 1)
                    break
            return {
                "top1_hit": top1_hit,
                "top3_hit": top3_hit,
                "mrr": mrr,
                "top1_chunk": ranking[0][0],
                "top1_score": round(ranking[0][1], 4),
                "latency_ms": round(latency, 2),
                "ranking": [
                    {"chunk_id": r[0], "score": round(r[1], 4), "is_hit": r[0] in ground_truth}
                    for r in ranking
                ],
            }

        td_eval = evaluate_ranking(td_scores, td_latency_ms)
        bge_eval = evaluate_ranking(bge_scores, bge_latency_ms)

        results_td.append({"query_id": qid, "category": q_cat, "query": q_text, **td_eval})
        results_bge.append({"query_id": qid, "category": q_cat, "query": q_text, **bge_eval})

    # Summary metrics
    n = len(BENCHMARK_QUERIES)
    td_r1 = sum(r["top1_hit"] for r in results_td) / n
    td_r3 = sum(r["top3_hit"] for r in results_td) / n
    td_mrr = sum(r["mrr"] for r in results_td) / n
    td_avg_lat = sum(r["latency_ms"] for r in results_td) / n

    bge_r1 = sum(r["top1_hit"] for r in results_bge) / n
    bge_r3 = sum(r["top3_hit"] for r in results_bge) / n
    bge_mrr = sum(r["mrr"] for r in results_bge) / n
    bge_avg_lat = sum(r["latency_ms"] for r in results_bge) / n

    report = {
        "summary": {
            "test_double": {
                "model": "hash-projection-v1 (1024d)",
                "recall@1": round(td_r1, 4),
                "recall@3": round(td_r3, 4),
                "mrr": round(td_mrr, 4),
                "avg_latency_ms": round(td_avg_lat, 2),
            },
            "local_bge": {
                "model": "BAAI/bge-small-zh-v1.5 (512d)",
                "recall@1": round(bge_r1, 4),
                "recall@3": round(bge_r3, 4),
                "mrr": round(bge_mrr, 4),
                "avg_latency_ms": round(bge_avg_lat, 2),
            },
        },
        "query_details": [
            {
                "id": q["id"],
                "category": q["category"],
                "query": q["query"],
                "ground_truth": q["ground_truth_chunks"],
                "test_double": results_td[i],
                "local_bge": results_bge[i],
            }
            for i, q in enumerate(BENCHMARK_QUERIES)
        ],
    }

    # Save to JSON
    output_path = Path("backend/data/benchmark_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n================== BENCHMARK SUMMARY ==================")
    print(f"Test Double (Hash 1024d):")
    print(f"  Recall@1: {td_r1*100:.1f}%, Recall@3: {td_r3*100:.1f}%, MRR: {td_mrr:.4f}, Avg Latency: {td_avg_lat:.2f}ms")
    print(f"Local BGE (ONNX 512d):")
    print(f"  Recall@1: {bge_r1*100:.1f}%, Recall@3: {bge_r3*100:.1f}%, MRR: {bge_mrr:.4f}, Avg Latency: {bge_avg_lat:.2f}ms")
    print("=======================================================\n")

    for item in report["query_details"]:
        td = item["test_double"]
        bge = item["local_bge"]
        print(f"[{item['id']}] ({item['category']}) {item['query']}")
        print(f"  Target Chunks: {item['ground_truth']}")
        print(f"  test_double: Top-1 Chunk={td['top1_chunk']} (Score={td['top1_score']}, Hit={td['top1_hit']}), MRR={td['mrr']}")
        print(f"  local_bge:   Top-1 Chunk={bge['top1_chunk']} (Score={bge['top1_score']}, Hit={bge['top1_hit']}), MRR={bge['mrr']}")
        print()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_benchmark())
