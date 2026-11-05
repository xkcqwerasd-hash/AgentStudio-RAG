"""
Evaluation Benchmark for M3.2: Dense Only vs BM25 Only vs Dense+BM25+RRF
Comprehensive Ablation Study comparing:
- Scheme A: Dense Only (BGE-small-zh-v1.5, Cosine Similarity)
- Scheme B: BM25 Only (Lucene positive IDF, TechnicalBM25Tokenizer)
- Scheme C: Dense + BM25 + RRF Hybrid Fusion (k=60, w_d=1.0, w_b=1.0)

Evaluates:
- Recall@1, Recall@3, Recall@5, MRR, nDCG@5
- Latency breakdown: Dense time, BM25 time, RRF fusion time, Total time (P50, P95)
- Parameter sensitivity: k in {20, 60, 100} and weights
- Failure case analysis and complementation effects across 8 distinct query categories
"""

import asyncio
import math
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory, init_db
from app.ingestion.pipeline import IngestionPipeline
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.bm25_retriever import BM25Retriever, bm25_index_manager
from app.retrieval.rrf_fusion import HybridRetriever, rrf_fuse
from app.retrieval.vector_store import VectorRetriever


BENCHMARK_QUERIES = [
    # Category 1: Chinese Semantic Paraphrasing (Dense advantage expected)
    {
        "id": "Q1",
        "category": "中文语义泛化/近义表述",
        "query": "微服务网关接口最长允许客户端等待多长时间？",
        "target_page": 1,
        "keywords": ["超时", "5000", "毫秒"],
    },
    {
        "id": "Q2",
        "category": "中文语义泛化/近义表述",
        "query": "当外部大模型服务严重不可用时，系统如何进行过载保护与降级？",
        "target_page": 2,
        "keywords": ["熔断", "OPEN", "降级"],
    },
    # Category 2: Exact Technical Proper Nouns (BM25 advantage expected)
    {
        "id": "Q3",
        "category": "精确专有名词",
        "query": "ModelHealthStore 模块的主要职责是什么？",
        "target_page": 2,
        "keywords": ["ModelHealthStore"],
    },
    {
        "id": "Q4",
        "category": "精确专有名词",
        "query": "系统采用哪种加密协议进行传输层安全握手与证书卸载？",
        "target_page": 1,
        "keywords": ["TLS", "证书"],
    },
    # Category 3: English Technical Identifiers
    {
        "id": "Q5",
        "category": "英文标识与状态",
        "query": "HALF_OPEN 状态进入的触发条件与恢复标准是什么？",
        "target_page": 2,
        "keywords": ["HALF_OPEN", "探测"],
    },
    {
        "id": "Q6",
        "category": "英文标识与状态",
        "query": "系统在什么状态下才会真正熔断阻断并处于 OPEN 状态？",
        "target_page": 2,
        "keywords": ["OPEN", "熔断"],
    },
    # Category 4: Parameter Names & Exact Numbers
    {
        "id": "Q7",
        "category": "参数名与精确数值",
        "query": "网关连接池配置中 min_idle 与 max_connections 分别是多少？",
        "target_page": 1,
        "keywords": ["min_idle", "max_connections", "10", "100"],
    },
    {
        "id": "Q8",
        "category": "参数名与精确数值",
        "query": "API 接口超时阈值 timeout 配置为多少毫秒？",
        "target_page": 1,
        "keywords": ["5000", "超时"],
    },
    # Category 5: Abbreviations & Acronyms
    {
        "id": "Q9",
        "category": "缩写与技术协议",
        "query": "用户分布式会话的签名机制与过期时间如何配置？",
        "target_page": 1,
        "keywords": ["JWT", "72小时"],
    },
    {
        "id": "Q10",
        "category": "缩写与技术协议",
        "query": "分布式会话状态同步使用的是哪种缓存集群？",
        "target_page": 1,
        "keywords": ["Redis"],
    },
    # Category 6: Semantic Collision / Hard Negatives
    {
        "id": "Q11",
        "category": "语义相似但目标不同",
        "query": "网关连接池参数规范与容量约束",
        "target_page": 1,
        "keywords": ["连接池", "min_idle"],
    },
    {
        "id": "Q12",
        "category": "语义相似但目标不同",
        "query": "三态熔断器的三种标准状态名称及演进",
        "target_page": 2,
        "keywords": ["CLOSED", "OPEN", "HALF_OPEN"],
    },
    # Category 7: Challenging Corner Cases (Failure Demonstrations)
    {
        "id": "Q13",
        "category": "同义泛化/零字面交集 (Dense优势)",
        "query": "当接口响应速度极慢甚至卡死时，平台规定的最长等待上限是几秒？",
        "target_page": 1,
        "keywords": ["超时", "5000ms"],
    },
    {
        "id": "Q14",
        "category": "极短专有词/语义稀释 (BM25优势)",
        "query": "JWT",
        "target_page": 1,
        "keywords": ["JWT"],
    },
    # Category 8: Query Rewriting Follow-ups & Cross-Chunk
    {
        "id": "Q15",
        "category": "重写后独立问题 (代词还原)",
        "query": "微服务网关的连接超时时间是多少毫秒？",
        "target_page": 1,
        "keywords": ["5000", "超时"],
    },
    {
        "id": "Q16",
        "category": "跨模块/跨切块综合检索",
        "query": "系统如何同时应对网络瞬时抖动与外部模型大面积宕机？",
        "target_page": 2,
        "keywords": ["重试", "熔断", "降级"],
    },
]


def calculate_metrics(target_page: int, ranked_chunks: list) -> Dict[str, float]:
    """Calculate Recall@K, MRR, nDCG@5 based on ground-truth target_page."""
    rank = None
    for idx, chunk in enumerate(ranked_chunks, start=1):
        if chunk.page_number == target_page:
            rank = idx
            break

    r1 = 1.0 if (rank is not None and rank <= 1) else 0.0
    r3 = 1.0 if (rank is not None and rank <= 3) else 0.0
    r5 = 1.0 if (rank is not None and rank <= 5) else 0.0
    mrr = 1.0 / rank if rank is not None else 0.0
    ndcg5 = (1.0 / math.log2(rank + 1)) if (rank is not None and rank <= 5) else 0.0

    return {
        "rank": rank,
        "recall@1": r1,
        "recall@3": r3,
        "recall@5": r5,
        "mrr": mrr,
        "ndcg@5": ndcg5,
    }


async def run_benchmark():
    await init_db()

    async with async_session_factory() as db:
        print("=" * 88)
        print(" AgentStudio-RAG M3.2: Dense vs BM25 vs Dense+BM25+RRF Benchmark")
        print("=" * 88)

        # 1. Setup or reuse evaluation knowledge base
        kb_stmt = select(KnowledgeBase).where(KnowledgeBase.name == "M3.2-RRF评测知识库")
        res = await db.execute(kb_stmt)
        kb = res.scalar_one_or_none()

        if not kb:
            kb = KnowledgeBase(
                name="M3.2-RRF评测知识库",
                description="Dense vs BM25 vs RRF 混合检索综合评测基准库",
                embedding_model="BAAI/bge-small-zh-v1.5",
                embedding_dimensions=512,
                embedding_provider="local_bge",
                chunk_size=400,
                chunk_overlap=40,
            )
            db.add(kb)
            await db.commit()
            await db.refresh(kb)

            pdf_path = Path("backend/data/sample_docs/enterprise_architecture_spec.pdf")
            if not pdf_path.exists():
                raise FileNotFoundError(f"PDF not found at {pdf_path}")

            doc = Document(
                knowledge_base_id=kb.id,
                file_name=pdf_path.name,
                file_type="pdf",
                file_size=pdf_path.stat().st_size,
                file_path=str(pdf_path.resolve()),
                status="PENDING",
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)

            print(f"[Ingestion] Parsing & Indexing benchmark document: {pdf_path.name}...")
            pipeline = IngestionPipeline(db)
            await pipeline.process_document(doc.id)
            print("[Ingestion] Document ingestion & local BGE embedding completed.")

        emb_client = get_embedding_client()
        vector_retriever = VectorRetriever(db)
        bm25_retriever = BM25Retriever(db)
        hybrid_retriever = HybridRetriever(db)

        # Pre-warm BM25 index and record index build time
        t_build_start = time.perf_counter()
        await bm25_index_manager.get_or_build_index(kb.id, db, force_rebuild=False)
        bm25_build_time_ms = (time.perf_counter() - t_build_start) * 1000

        print(f"[Index Warmup] BM25 in-memory & disk index ready. Time: {bm25_build_time_ms:.2f} ms")
        print("-" * 88)

        # Storage for Scheme results
        dense_results_all = []
        bm25_results_all = []
        rrf_results_all = []

        dense_times = []
        bm25_times = []
        rrf_fusion_times = []
        total_times = []

        # Parameter variations: k=20, k=60, k=100
        rrf_k20_results = []
        rrf_k100_results = []

        # Run query evaluation
        print(f"{'ID':<4} | {'Query (Preview)':<30} | {'GroundTruth':<10} | {'Dense Rk':<8} | {'BM25 Rk':<8} | {'RRF Rk':<8} | {'Latency':<8}")
        print("-" * 88)

        for q in BENCHMARK_QUERIES:
            query_str = q["query"]
            target_page = q["target_page"]

            # 1. Dense Only Search
            t0 = time.perf_counter()
            query_vec = await emb_client.embed_query(query_str)
            dense_chunks = await vector_retriever.search(
                knowledge_base_id=kb.id,
                query_vector=query_vec,
                top_k=5,
                threshold=0.0,
            )
            d_time = (time.perf_counter() - t0) * 1000
            dense_times.append(d_time)
            m_dense = calculate_metrics(target_page, dense_chunks)
            dense_results_all.append(m_dense)

            # 2. BM25 Only Search
            t0 = time.perf_counter()
            bm25_chunks = await bm25_retriever.search(
                knowledge_base_id=kb.id,
                query=query_str,
                top_k=5,
            )
            b_time = (time.perf_counter() - t0) * 1000
            bm25_times.append(b_time)
            m_bm25 = calculate_metrics(target_page, bm25_chunks)
            bm25_results_all.append(m_bm25)

            # 3. Hybrid Search (RRF k=60)
            t0 = time.perf_counter()
            hybrid_res = await hybrid_retriever.search(
                knowledge_base_id=kb.id,
                query=query_str,
                top_k=5,
                k=60,
            )
            h_time = (time.perf_counter() - t0) * 1000
            total_times.append(h_time)
            rrf_fusion_times.append(hybrid_res.rrf_duration_ms)
            m_rrf = calculate_metrics(target_page, hybrid_res.chunks)
            rrf_results_all.append(m_rrf)

            # RRF sensitivity: k=20 and k=100
            fused_k20 = rrf_fuse(hybrid_res.dense_chunks, hybrid_res.bm25_chunks, k=20, top_k=5)
            rrf_k20_results.append(calculate_metrics(target_page, fused_k20))
            fused_k100 = rrf_fuse(hybrid_res.dense_chunks, hybrid_res.bm25_chunks, k=100, top_k=5)
            rrf_k100_results.append(calculate_metrics(target_page, fused_k100))

            q_disp = (query_str[:26] + "...") if len(query_str) > 28 else query_str
            d_rk_str = f"#{m_dense['rank']}" if m_dense["rank"] else "Miss"
            b_rk_str = f"#{m_bm25['rank']}" if m_bm25["rank"] else "Miss"
            r_rk_str = f"#{m_rrf['rank']}" if m_rrf["rank"] else "Miss"

            print(f"{q['id']:<4} | {q_disp:<30} | Page {target_page:<5} | {d_rk_str:<8} | {b_rk_str:<8} | {r_rk_str:<8} | {h_time:.2f}ms")

        # Compute aggregate averages
        n = len(BENCHMARK_QUERIES)

        def avg_metric(res_list, key):
            return sum(r[key] for r in res_list) / n

        print("=" * 88)
        print(" ABLATION COMPARISON SUMMARY (16 Queries)")
        print("=" * 88)
        print(f"{'Evaluation Metric':<20} | {'Scheme A (Dense)':<18} | {'Scheme B (BM25)':<18} | {'Scheme C (RRF k=60)':<18}")
        print("-" * 88)

        metrics_keys = [
            ("Recall@1", "recall@1", "%"),
            ("Recall@3", "recall@3", "%"),
            ("Recall@5", "recall@5", "%"),
            ("MRR", "mrr", ""),
            ("nDCG@5", "ndcg@5", ""),
        ]

        for label, k, fmt in metrics_keys:
            v_dense = avg_metric(dense_results_all, k)
            v_bm25 = avg_metric(bm25_results_all, k)
            v_rrf = avg_metric(rrf_results_all, k)
            if fmt == "%":
                print(f"{label:<20} | {v_dense * 100:>16.1f}% | {v_bm25 * 100:>16.1f}% | {v_rrf * 100:>16.1f}%")
            else:
                print(f"{label:<20} | {v_dense:>17.4f} | {v_bm25:>17.4f} | {v_rrf:>17.4f}")

        # Latency Metrics
        dense_avg = sum(dense_times) / n
        bm25_avg = sum(bm25_times) / n
        rrf_avg = sum(rrf_fusion_times) / n
        total_avg = sum(total_times) / n

        dense_p50 = statistics.median(dense_times)
        dense_p95 = statistics.quantiles(dense_times, nplaces=20)[18] if len(dense_times) >= 20 else max(dense_times)

        bm25_p50 = statistics.median(bm25_times)
        bm25_p95 = statistics.quantiles(bm25_times, nplaces=20)[18] if len(bm25_times) >= 20 else max(bm25_times)

        rrf_p50 = statistics.median(total_times)
        rrf_p95 = statistics.quantiles(total_times, nplaces=20)[18] if len(total_times) >= 20 else max(total_times)

        print("-" * 88)
        print(f"{'Latency (P50 / P95)':<20} | {f'{dense_p50:.2f} / {dense_p95:.2f} ms':>18} | {f'{bm25_p50:.2f} / {bm25_p95:.2f} ms':>18} | {f'{rrf_p50:.2f} / {rrf_p95:.2f} ms':>18}")
        print(f"{'Latency (Mean)':<20} | {f'{dense_avg:.2f} ms':>18} | {f'{bm25_avg:.2f} ms':>18} | {f'{total_avg:.2f} ms (RRF: {rrf_avg:.2f}ms)':>18}")
        print("-" * 88)

        # Parameter Sensitivity Analysis
        print(" PARAMETER SENSITIVITY ANALYSIS (RRF Smoothing Constant k)")
        print("-" * 88)
        print(f"k = 20 : Recall@1 = {avg_metric(rrf_k20_results, 'recall@1')*100:.1f}%, MRR = {avg_metric(rrf_k20_results, 'mrr'):.4f}, nDCG@5 = {avg_metric(rrf_k20_results, 'ndcg@5'):.4f}")
        print(f"k = 60 : Recall@1 = {avg_metric(rrf_results_all, 'recall@1')*100:.1f}%, MRR = {avg_metric(rrf_results_all, 'mrr'):.4f}, nDCG@5 = {avg_metric(rrf_results_all, 'ndcg@5'):.4f}")
        print(f"k = 100: Recall@1 = {avg_metric(rrf_k100_results, 'recall@1')*100:.1f}%, MRR = {avg_metric(rrf_k100_results, 'mrr'):.4f}, nDCG@5 = {avg_metric(rrf_k100_results, 'ndcg@5'):.4f}")
        print("=" * 88)

        # Failure Case Analysis
        print(" FAILURE CASE & COMPLEMENTATION ANALYSIS")
        print("-" * 88)
        for idx, q in enumerate(BENCHMARK_QUERIES):
            md = dense_results_all[idx]["rank"]
            mb = bm25_results_all[idx]["rank"]
            mr = rrf_results_all[idx]["rank"]

            # Complementary cases
            if md != mb:
                print(f"[{q['id']}] {q['query']}")
                print(f"  - Dense Rank: #{md if md else 'Miss'} | BM25 Rank: #{mb if mb else 'Miss'} | RRF Rank: #{mr if mr else 'Miss'}")
                if md is not None and (mb is None or md < mb):
                    if mr == md:
                        print(f"  => [Dense Win Preserved]: RRF accurately preserved Top-{mr} ranking despite BM25 missing/lagging.")
                    else:
                        print(f"  => [Dense Win Softened]: RRF ranked at #{mr}.")
                elif mb is not None and (md is None or mb < md):
                    if mr == mb:
                        print(f"  => [BM25 Win Rescued]: RRF successfully rescued Dense Miss/Lag, pulling target to Top-{mr}!")
                    else:
                        print(f"  => [BM25 Win Softened]: RRF ranked at #{mr}.")


if __name__ == "__main__":
    asyncio.run(run_benchmark())
