"""
Evaluation Benchmark for M3.3: Dense vs BM25 vs RRF vs RRF+Cross-Encoder Reranker
Comprehensive Ablation Study comparing 4 Retrieval Architectures:
- Scheme A: Dense Only (BGE-small-zh-v1.5, Cosine Similarity)
- Scheme B: BM25 Only (Lucene positive IDF, TechnicalBM25Tokenizer)
- Scheme C: Dense + BM25 + RRF Hybrid Fusion (k=60, w_d=1.0, w_b=1.0)
- Scheme D: Dense + BM25 + RRF + Cross-Encoder Reranker (bge-reranker-base ONNX INT8)

Evaluates:
- Recall@20 (Candidate pool ground truth recall)
- Recall@1, Recall@3, Recall@5, MRR, nDCG@5, Precision@5
- Latency breakdown: Dense time, BM25 time, RRF fusion time, Reranker time (P50, P95)
- Candidate Pool Size Sensitivity: Pool Top-10 vs Top-20 vs Top-30 vs Top-50
- Rank shift analysis and failure case investigation across 10 query categories
"""

import asyncio
import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory, init_db
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.bm25_retriever import BM25Retriever, bm25_index_manager
from app.retrieval.reranker import CrossEncoderReranker, LocalONNXReranker, TestDoubleReranker
from app.retrieval.vector_store import ScoredChunk, VectorRetriever
from app.retrieval.rrf_fusion import HybridRetriever, rrf_fuse
from app.retrieval.vector_store import VectorRetriever


BENCHMARK_QUERIES = [
    # Category 1: 自然语言长句提问 (Natural language descriptive queries)
    {
        "id": "Q1",
        "category": "自然语言长句提问",
        "query": "微服务网关接口最长允许客户端等待多长时间？",
        "target_page": 1,
        "keywords": ["超时", "5000", "毫秒"],
    },
    {
        "id": "Q2",
        "category": "自然语言长句提问",
        "query": "当外部大模型服务严重不可用时，系统如何进行过载保护与降级？",
        "target_page": 2,
        "keywords": ["熔断", "OPEN", "降级"],
    },

    # Category 2: 英文技术术语缩写 (English technical acronyms)
    {
        "id": "Q3",
        "category": "英文技术术语缩写",
        "query": "系统采用哪种非对称加密算法对 JWT 会话令牌进行数字签名？",
        "target_page": 1,
        "keywords": ["JWT", "RS256"],
    },
    {
        "id": "Q4",
        "category": "英文技术术语缩写",
        "query": "系统使用哪种网络协议进行端到端加密与传输层安全卸载？",
        "target_page": 1,
        "keywords": ["TLS", "HTTPS"],
    },

    # Category 3: 数字和参数混合 (Digits & parameter names)
    {
        "id": "Q5",
        "category": "数字和参数混合",
        "query": "网关连接池配置中 min_idle 与 max_connections 分别是多少？",
        "target_page": 1,
        "keywords": ["min_idle", "max_connections", "10", "100"],
    },
    {
        "id": "Q6",
        "category": "数字和参数混合",
        "query": "API 接口超时阈值 timeout 配置为多少毫秒？",
        "target_page": 1,
        "keywords": ["5000", "超时"],
    },

    # Category 4: 专业缩写和行业术语 (Specialized technical jargon)
    {
        "id": "Q7",
        "category": "专业缩写和行业术语",
        "query": "ModelHealthStore 模块的主要职责是什么？",
        "target_page": 2,
        "keywords": ["ModelHealthStore"],
    },
    {
        "id": "Q8",
        "category": "专业缩写和行业术语",
        "query": "分布式会话状态同步使用的是哪种缓存集群架构？",
        "target_page": 1,
        "keywords": ["Redis", "集群"],
    },

    # Category 5: 形式相似但答案完全不同的提问 (Surface-similar queries with distinct answers)
    {
        "id": "Q9",
        "category": "形式相似但答案不同",
        "query": "网关连接池容量约束与空闲连接下限",
        "target_page": 1,
        "keywords": ["连接池", "min_idle"],
    },
    {
        "id": "Q10",
        "category": "形式相似但答案不同",
        "query": "三态熔断器的三种标准状态名称及演进过程",
        "target_page": 2,
        "keywords": ["CLOSED", "OPEN", "HALF_OPEN"],
    },

    # Category 6: 隐式指代追问 (Implicit coreference follow-ups)
    {
        "id": "Q11",
        "category": "隐式指代追问",
        "query": "系统在什么状态下才会真正熔断阻断并处于 OPEN 状态？",
        "target_page": 2,
        "keywords": ["OPEN", "熔断"],
    },
    {
        "id": "Q12",
        "category": "隐式指代追问",
        "query": "HALF_OPEN 状态进入的触发条件与恢复标准是什么？",
        "target_page": 2,
        "keywords": ["HALF_OPEN", "探测"],
    },

    # Category 7: Query Rewriting 重写后复杂长问题 (Post-rewrite complex queries)
    {
        "id": "Q13",
        "category": "Query Rewriter重写问题",
        "query": "微服务网关针对长耗时大模型请求设定的超时限制具体是多少毫秒？",
        "target_page": 1,
        "keywords": ["5000", "超时"],
    },
    {
        "id": "Q14",
        "category": "Query Rewriter重写问题",
        "query": "系统三态熔断器在满足何种高错误率和探测成功率条件下完成自愈复位？",
        "target_page": 2,
        "keywords": ["HALF_OPEN", "CLOSED", "自愈"],
    },

    # Category 8: 多 Chunk 综合题 (Multi-chunk synthesis queries)
    {
        "id": "Q15",
        "category": "多Chunk综合检索",
        "query": "系统如何同时应对网络瞬时抖动与外部模型大面积宕机？",
        "target_page": 2,
        "keywords": ["重试", "熔断", "降级"],
    },
    {
        "id": "Q16",
        "category": "多Chunk综合检索",
        "query": "客户端认证与高可用容灾故障切换的协同流程是什么？",
        "target_page": 1,
        "keywords": ["JWT", "Redis", "高可用"],
    },

    # Category 9: 文档之间精确区分 (Cross-document discrimination)
    {
        "id": "Q17",
        "category": "文档间精确区分",
        "query": "系统安全白皮书中关于分布式鉴权与令牌时效的详细技术规定",
        "target_page": 1,
        "keywords": ["JWT", "72小时", "RS256"],
    },

    # Category 10: 极端 Case 与抗噪能力 (Adversarial edge cases)
    {
        "id": "Q18",
        "category": "极端Case/零字面交集",
        "query": "当网关服务因下游过载而持续卡顿时，系统设置的硬性熔断阈值具体有多大？",
        "target_page": 2,
        "keywords": ["熔断", "OPEN", "50%"],
    },
]


DOCUMENT_CHUNKS_DATA = [
    # Document 1: 微服务网关与系统通信规范 (Page 1)
    {
        "doc_name": "microservice_gateway_spec.md",
        "page_number": 1,
        "chunk_index": 0,
        "content": (
            "【微服务网关通信规范】\n"
            "1. 接口超时与连接池规范：网关作为整个系统的流量入口，对下游所有业务微服务与大模型交互代理服务"
            "设置统一的调用超时时间。默认全局调用超时时间为 5000 毫秒（即 5 秒）。在网络突发抖动或"
            "大模型服务端推理耗时超过 5000ms 时，网关将主动中断 HTTP 连接并向客户端抛出 504 Gateway Timeout 异常。\n"
            "2. 数据库与客户端连接池参数：底层连接池采用高性能异步连接池组件，其最小空闲连接数 min_idle 配置为 10，"
            "最大并发连接数 max_connections 设定为 100。当负载高峰期连接数耗尽且等待队列溢出时，触发弹性背压告警。"
        ),
    },
    {
        "doc_name": "microservice_gateway_spec.md",
        "page_number": 1,
        "chunk_index": 1,
        "content": (
            "【网关安全鉴权与分布式会话管理】\n"
            "3. 身份认证与 JWT 令牌规范：网关采用基于非对称加密算法 RS256 签名的 JSON Web Token (JWT) 进行用户无状态认证。"
            "每个颁发的 Access Token 具有严格的生命周期，默认有效期为 72 小时。网关本地维护公钥缓存以进行高速验签，"
            "避免频繁访问中心认证中心造成性能损耗。\n"
            "4. 分布式会话与状态存储：对于用户多轮对话状态及临时黑名单凭证，统一持久化至 Redis 高可用集群。"
            "网关入口层强制启用 TLS 1.3 传输加密，实施端到端证书卸载与严格的双向证书检验。"
        ),
    },

    # Document 2: 高可用容灾与三态熔断架构 (Page 2)
    {
        "doc_name": "high_availability_resilience.md",
        "page_number": 2,
        "chunk_index": 0,
        "content": (
            "【系统容灾与三态熔断器核心机制】\n"
            "1. 熔断状态机定义：为防止单点故障引发雪崩效应，系统集成了标准的三态熔断器（Circuit Breaker），"
            "包含 CLOSED（正常闭合闭锁）、OPEN（完全熔断打开）以及 HALF_OPEN（半开探测自愈）三种标准状态。\n"
            "2. 熔断进入标准（CLOSED -> OPEN）：在连续 30 秒的滑动统计时间窗口内，当针对外部模型服务或下游 API 的"
            "请求失败率（包括网络超时、5xx HTTP 错误及连接拒绝）超过 50% 时，三态熔断器将瞬间由 CLOSED 转移为 OPEN 状态。\n"
            "在 OPEN 状态下，系统将直接拦截并阻断 100% 的上游流量，不再发起实际远程调用，而是直接降级返回友好兜底回答。"
        ),
    },
    {
        "doc_name": "high_availability_resilience.md",
        "page_number": 2,
        "chunk_index": 1,
        "content": (
            "【自愈恢复流程与 ModelHealthStore 架构】\n"
            "3. 半开自愈探测机制（OPEN -> HALF_OPEN）：当熔断器保持在 OPEN 状态满 60 秒冷静恢复期后，"
            "系统会自动将状态切换至 HALF_OPEN 状态。在 HALF_OPEN 阶段，熔断器允许放行 10% 的真实试探流量以检测下游恢复情况。"
            "若试探请求连续 5 次成功响应且延迟低于 2000ms，则熔断器恢复为 CLOSED 正常状态；若依然失败则立刻重新进入 OPEN。\n"
            "4. ModelHealthStore 核心职责：系统内部的独立健康管理组件 ModelHealthStore 负责实时监控并汇总"
            "所有下游模型节点的吞吐量、P95 延迟、错误率及熔断器状态，并向控制台提供高精度 Prometheus 指标暴露与动态路由降级。"
        ),
    },

    # Document 3: 数据安全与审计合规规范 (Page 3 - Distractor / Negative)
    {
        "doc_name": "compliance_and_audit.md",
        "page_number": 3,
        "chunk_index": 0,
        "content": (
            "【企业级数据安全与合规审计指南】\n"
            "1. 数据分类分级原则：系统遵循 GDPR 与中国网络安全法，对用户输入的私有对话数据进行静态脱敏与落盘加密。"
            "所有敏感个人身份信息（PII）在进入 Embedding 模型向量化之前，需经过正则表达式与命名实体识别组件进行脱敏过滤。\n"
            "2. 审计日志留存要求：管理员操作记录与模型调用 Trace 元数据须在冷备份存储中保存不少于 180 天以备审计审查。"
        ),
    },
]


def calculate_metrics(target_page: int, ranked_chunks: list, pool_candidates: Optional[list] = None) -> Dict[str, float]:
    """Calculate Recall@K, MRR, nDCG@5, Precision@5, and Recall@20."""
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

    # Precision@5
    hits_in_top5 = sum(1 for c in ranked_chunks[:5] if c.page_number == target_page)
    p5 = hits_in_top5 / 5.0

    # Recall@20 (coverage in candidate pool)
    r20 = 0.0
    if pool_candidates:
        r20 = 1.0 if any(c.page_number == target_page for c in pool_candidates) else 0.0
    elif rank is not None and rank <= 20:
        r20 = 1.0

    return {
        "rank": rank,
        "recall@1": r1,
        "recall@3": r3,
        "recall@5": r5,
        "recall@20": r20,
        "mrr": mrr,
        "ndcg@5": ndcg5,
        "p@5": p5,
    }


async def setup_benchmark_kb(db: AsyncSession) -> KnowledgeBase:
    """Setup or get the evaluation knowledge base."""
    emb_client = get_embedding_client()
    kb_name = "M3.3-Reranker综合评测基准知识库"

    stmt = select(KnowledgeBase).where(KnowledgeBase.name == kb_name)
    kb = (await db.execute(stmt)).scalar_one_or_none()

    if not kb:
        kb = KnowledgeBase(
            name=kb_name,
            description="M3.3 Cross-Encoder Reranker 检索排序质量基准知识库",
            embedding_model=emb_client.model_name,
            embedding_dimensions=emb_client.dimensions,
            embedding_provider=emb_client.provider_name,
        )
        db.add(kb)
        await db.flush()

        doc_map = {}
        for item in DOCUMENT_CHUNKS_DATA:
            doc_name = item["doc_name"]
            if doc_name not in doc_map:
                doc = Document(
                    knowledge_base_id=kb.id,
                    file_name=doc_name,
                    file_type="md",
                    file_path=f"/benchmark/{doc_name}",
                    status="COMPLETED",
                )
                db.add(doc)
                await db.flush()
                doc_map[doc_name] = doc

            content = item["content"]
            embs = await emb_client.embed_documents([content])
            chunk = DocumentChunk(
                knowledge_base_id=kb.id,
                document_id=doc_map[doc_name].id,
                chunk_index=item["chunk_index"],
                page_number=item["page_number"],
                content=content,
                embedding_json=json.dumps(embs[0]),
            )
            db.add(chunk)

        await db.commit()
        # Invalidate BM25 cache so it indexes these chunks
        bm25_index_manager.invalidate(kb.id)

    return kb


async def run_benchmark():
    await init_db()

    async with async_session_factory() as db:
        print("=" * 96)
        print(" AgentStudio-RAG M3.3: Dense vs BM25 vs RRF vs Cross-Encoder Reranker Benchmark")
        print("=" * 96)

        kb = await setup_benchmark_kb(db)
        print(f"[*] Benchmark KnowledgeBase ID: {kb.id} ({kb.name})")

        dense_retriever = VectorRetriever(db)
        bm25_retriever = BM25Retriever(db)
        hybrid_retriever = HybridRetriever(db)

        # Initialize CrossEncoderReranker with real LocalONNX if available, else TestDouble
        try:
            onnx_client = LocalONNXReranker(batch_size=8)
            reranker = CrossEncoderReranker(client=onnx_client)
            print(f"[*] Cross-Encoder Client: LocalONNXReranker ({onnx_client.model_name}) on CPU")
        except Exception as e:
            print(f"[!] Warning: LocalONNX initialization failed ({e}), falling back to TestDoubleReranker.")
            reranker = CrossEncoderReranker(client=TestDoubleReranker())

        emb_client = get_embedding_client()

        # Warm-up phase to eliminate cold-start distortion
        print("[*] Executing warm-up phase (BM25 index pre-warm + ONNX runtime warmup)...")
        t_warm_start = time.perf_counter()
        warmup_q = BENCHMARK_QUERIES[0]["query"]
        q_vec_warm = await emb_client.embed_query(warmup_q)
        await dense_retriever.search(knowledge_base_id=kb.id, query_vector=q_vec_warm, top_k=5, threshold=0.0)
        await bm25_retriever.search(knowledge_base_id=kb.id, query=warmup_q, top_k=5)
        w_rrf = await hybrid_retriever.search(knowledge_base_id=kb.id, query=warmup_q, top_k=5)
        await reranker.rerank(query=warmup_q, candidates=w_rrf.chunks, top_k=5)
        warm_dt = (time.perf_counter() - t_warm_start) * 1000
        print(f"[*] Warm-up completed successfully in {warm_dt:.2f} ms.\n")

        results_dense: List[Dict[str, Any]] = []
        results_bm25: List[Dict[str, Any]] = []
        results_rrf: List[Dict[str, Any]] = []
        results_rerank: List[Dict[str, Any]] = []

        dense_latencies: List[float] = []
        bm25_latencies: List[float] = []
        hybrid_latencies: List[float] = []
        pure_rrf_latencies: List[float] = []
        rerank_latencies: List[float] = []
        total_latencies: List[float] = []

        rank_shifts: List[Dict[str, Any]] = []

        print(f"[*] Executing {len(BENCHMARK_QUERIES)} benchmark queries across 4 retrieval pipelines...\n")

        for q_item in BENCHMARK_QUERIES:
            qid = q_item["id"]
            query = q_item["query"]
            cat = q_item["category"]
            target_page = q_item["target_page"]

            # 1. Dense Only
            t0 = time.perf_counter()
            q_vec = await emb_client.embed_query(query)
            dense_chunks = await dense_retriever.search(
                knowledge_base_id=kb.id,
                query_vector=q_vec,
                top_k=20,
                threshold=0.0,
            )
            t_dense = (time.perf_counter() - t0) * 1000
            dense_latencies.append(t_dense)
            m_dense = calculate_metrics(target_page, dense_chunks[:5], dense_chunks)
            results_dense.append(m_dense)

            # 2. BM25 Only
            t0 = time.perf_counter()
            bm25_chunks = await bm25_retriever.search(
                knowledge_base_id=kb.id,
                query=query,
                top_k=20,
            )
            t_bm25 = (time.perf_counter() - t0) * 1000
            bm25_latencies.append(t_bm25)
            m_bm25 = calculate_metrics(target_page, bm25_chunks[:5], bm25_chunks)
            results_bm25.append(m_bm25)

            # 3. Dense + BM25 + RRF Hybrid Fusion
            t0 = time.perf_counter()
            rrf_res = await hybrid_retriever.search(
                knowledge_base_id=kb.id,
                query=query,
                top_k=20,
            )
            t_hybrid = (time.perf_counter() - t0) * 1000
            hybrid_latencies.append(t_hybrid)
            pure_rrf_latencies.append(rrf_res.rrf_duration_ms)
            m_rrf = calculate_metrics(target_page, rrf_res.chunks[:5], rrf_res.chunks)
            results_rrf.append(m_rrf)

            # 4. Dense + BM25 + RRF + Cross-Encoder Reranker
            t0 = time.perf_counter()
            rerank_res = await reranker.rerank(
                query=query,
                candidates=rrf_res.chunks,
                top_k=5,
            )
            t_rerank = (time.perf_counter() - t0) * 1000
            rerank_latencies.append(t_rerank)
            total_latencies.append(t_hybrid + t_rerank)
            m_rerank = calculate_metrics(target_page, rerank_res.chunks, rrf_res.chunks)
            results_rerank.append(m_rerank)

            # Record Rank Shift (RRF rank vs Reranker rank)
            rrf_rank = m_rrf["rank"]
            rerank_rank = m_rerank["rank"]
            shift = (rrf_rank - rerank_rank) if (rrf_rank and rerank_rank) else 0
            rank_shifts.append({
                "id": qid,
                "category": cat,
                "query": query,
                "target_page": target_page,
                "dense_rank": m_dense["rank"],
                "bm25_rank": m_bm25["rank"],
                "rrf_rank": rrf_rank,
                "rerank_rank": rerank_rank,
                "shift": shift,
                "reranker_score": rerank_res.chunks[0].reranker_score if rerank_res.chunks else 0.0,
            })

        # ==============================================================================
        # Aggregate Metrics Computation
        # ==============================================================================
        def mean_metric(results_list: list, metric_name: str) -> float:
            return sum(r[metric_name] for r in results_list) / len(results_list)

        dense_r1 = mean_metric(results_dense, "recall@1")
        dense_r3 = mean_metric(results_dense, "recall@3")
        dense_r5 = mean_metric(results_dense, "recall@5")
        dense_r20 = mean_metric(results_dense, "recall@20")
        dense_mrr = mean_metric(results_dense, "mrr")
        dense_ndcg = mean_metric(results_dense, "ndcg@5")
        dense_p5 = mean_metric(results_dense, "p@5")

        bm25_r1 = mean_metric(results_bm25, "recall@1")
        bm25_r3 = mean_metric(results_bm25, "recall@3")
        bm25_r5 = mean_metric(results_bm25, "recall@5")
        bm25_r20 = mean_metric(results_bm25, "recall@20")
        bm25_mrr = mean_metric(results_bm25, "mrr")
        bm25_ndcg = mean_metric(results_bm25, "ndcg@5")
        bm25_p5 = mean_metric(results_bm25, "p@5")

        rrf_r1 = mean_metric(results_rrf, "recall@1")
        rrf_r3 = mean_metric(results_rrf, "recall@3")
        rrf_r5 = mean_metric(results_rrf, "recall@5")
        rrf_r20 = mean_metric(results_rrf, "recall@20")
        rrf_mrr = mean_metric(results_rrf, "mrr")
        rrf_ndcg = mean_metric(results_rrf, "ndcg@5")
        rrf_p5 = mean_metric(results_rrf, "p@5")

        rerank_r1 = mean_metric(results_rerank, "recall@1")
        rerank_r3 = mean_metric(results_rerank, "recall@3")
        rerank_r5 = mean_metric(results_rerank, "recall@5")
        rerank_r20 = mean_metric(results_rerank, "recall@20")
        rerank_mrr = mean_metric(results_rerank, "mrr")
        rerank_ndcg = mean_metric(results_rerank, "ndcg@5")
        rerank_p5 = mean_metric(results_rerank, "p@5")

        # Latency quantiles
        def quantiles(lat_list: List[float]) -> Tuple[float, float, float]:
            s = sorted(lat_list)
            p50 = statistics.median(s)
            p95 = s[int(len(s) * 0.95)]
            mean = statistics.mean(s)
            return round(p50, 2), round(p95, 2), round(mean, 2)

        d_p50, d_p95, d_mean = quantiles(dense_latencies)
        b_p50, b_p95, b_mean = quantiles(bm25_latencies)
        h_p50, h_p95, h_mean = quantiles(hybrid_latencies)
        r_p50, r_p95, r_mean = quantiles(pure_rrf_latencies)
        k_p50, k_p95, k_mean = quantiles(rerank_latencies)
        tot_p50, tot_p95, tot_mean = quantiles(total_latencies)

        # Print Comparison Table
        print("\n" + "=" * 96)
        print(" 全量检索效果与质量对比总表 (18 条真实多类型 Query)")
        print("=" * 96)
        print(f"{'检索方案 (Scheme)':<32} | {'R@1':<7} | {'R@3':<7} | {'R@5':<7} | {'R@20':<7} | {'MRR':<7} | {'nDCG@5':<8} | {'P@5':<7}")
        print("-" * 96)
        print(f"{'A. Dense Only (BGE-small)':<32} | {dense_r1:<7.4f} | {dense_r3:<7.4f} | {dense_r5:<7.4f} | {dense_r20:<7.4f} | {dense_mrr:<7.4f} | {dense_ndcg:<8.4f} | {dense_p5:<7.4f}")
        print(f"{'B. BM25 Only (Lucene IDF)':<32} | {bm25_r1:<7.4f} | {bm25_r3:<7.4f} | {bm25_r5:<7.4f} | {bm25_r20:<7.4f} | {bm25_mrr:<7.4f} | {bm25_ndcg:<8.4f} | {bm25_p5:<7.4f}")
        print(f"{'C. Dense + BM25 (RRF Fusion)':<32} | {rrf_r1:<7.4f} | {rrf_r3:<7.4f} | {rrf_r5:<7.4f} | {rrf_r20:<7.4f} | {rrf_mrr:<7.4f} | {rrf_ndcg:<8.4f} | {rrf_p5:<7.4f}")
        print(f"{'D. RRF + Cross-Encoder Reranker':<32} | {rerank_r1:<7.4f} | {rerank_r3:<7.4f} | {rerank_r5:<7.4f} | {rerank_r20:<7.4f} | {rerank_mrr:<7.4f} | {rerank_ndcg:<8.4f} | {rerank_p5:<7.4f}")
        print("=" * 96)

        print("\n" + "=" * 96)
        print(" 各检索阶段延迟耗时统计 (Latency Breakdown in ms - 预热后各阶段独立统计)")
        print("=" * 96)
        print(f"{'阶段 (Stage)':<36} | {'P50 (ms)':<10} | {'P95 (ms)':<10} | {'Mean (ms)':<10}")
        print("-" * 96)
        print(f"{'Dense 语义检索阶段 (向量嵌入+检索)':<36} | {d_p50:<10.2f} | {d_p95:<10.2f} | {d_mean:<10.2f}")
        print(f"{'BM25 稀疏检索阶段 (分词+BM25评分)':<36} | {b_p50:<10.2f} | {b_p95:<10.2f} | {b_mean:<10.2f}")
        print(f"{'双路并行混合检索端到端 (Hybrid E2E)':<36} | {h_p50:<10.2f} | {h_p95:<10.2f} | {h_mean:<10.2f}")
        print(f"{'  └─ 细分：纯 RRF 倒数排名融合算法':<36} | {r_p50:<10.2f} | {r_p95:<10.2f} | {r_mean:<10.2f}")
        print(f"{'Cross-Encoder 精排阶段 (Local CPU)':<36} | {k_p50:<10.2f} | {k_p95:<10.2f} | {k_mean:<10.2f}")
        print(f"{'全链路端到端总检索耗时 (Hybrid+Rerank)':<36} | {tot_p50:<10.2f} | {tot_p95:<10.2f} | {tot_mean:<10.2f}")
        print("=" * 96)

        # Print Rank Shifts
        print("\n" + "=" * 96)
        print(" Rank Shift 细粒度排名位移分析表 (RRF vs RRF+Reranker)")
        print("=" * 96)
        print(f"{'Query ID & 问题':<36} | {'Dense':<6} | {'BM25':<6} | {'RRF':<6} | {'Rerank':<7} | {'位移':<6} | {'首位得分'}")
        print("-" * 96)
        for s in rank_shifts:
            q_disp = f"[{s['id']}] {s['query'][:22]}..."
            d_str = f"#{s['dense_rank']}" if s['dense_rank'] else "未召回"
            b_str = f"#{s['bm25_rank']}" if s['bm25_rank'] else "未召回"
            r_str = f"#{s['rrf_rank']}" if s['rrf_rank'] else "未召回"
            k_str = f"#{s['rerank_rank']}" if s['rerank_rank'] else "未召回"
            shift_symbol = f"+{s['shift']} ↑" if s['shift'] > 0 else (f"{s['shift']} ↓" if s['shift'] < 0 else "持平 -")
            print(f"{q_disp:<36} | {d_str:<6} | {b_str:<6} | {r_str:<6} | {k_str:<7} | {shift_symbol:<6} | {s['reranker_score']:.4f}")
        print("=" * 96)

        # Candidate pool ablation & physical scaling measurement
        print("\n" + "=" * 96)
        print(" 候选池规模敏感性消融与真实 ONNX 推理延迟实测 (Candidate Pool Scaling)")
        print("=" * 96)
        print(f"{'候选池物理规模':<20} | {'测试样本数':<10} | {'P50 (ms)':<10} | {'P95 (ms)':<10} | {'Mean (ms)':<10} | {'检索质量指标状态'}")
        print("-" * 96)

        # 1. Base Pool (5 chunks - actual full benchmark KB size)
        print(f"{'Top-5 候选池 (真实语料)':<20} | {'5 chunks':<10} | {k_p50:<10.2f} | {k_p95:<10.2f} | {k_mean:<10.2f} | {f'R@1={rerank_r1:.4f}, MRR={rerank_mrr:.4f}, nDCG@5={rerank_ndcg:.4f}'}")

        # 2. Measure actual physical inference scaling with real ONNX for pool sizes 10, 20, 30, 50
        sample_query = BENCHMARK_QUERIES[0]["query"]
        base_chunks = rrf_res.chunks
        for pool_size in [10, 20, 30, 50]:
            # Create synthetic distractor chunks replicating actual corpus text to test real ONNX batch latency
            test_chunks = []
            for i in range(pool_size):
                template = base_chunks[i % len(base_chunks)]
                test_chunks.append(ScoredChunk(
                    chunk_id=f"chk_ablation_{pool_size}_{i}",
                    document_id=template.document_id,
                    document_name=template.document_name,
                    page_number=template.page_number,
                    chunk_index=i,
                    content=template.content,
                    similarity_score=template.similarity_score,
                    fusion_score=0.01 / (i + 1),
                ))

            # Run 3 repetitions with real reranker
            pool_latencies = []
            for _ in range(3):
                t_p0 = time.perf_counter()
                await reranker.rerank(query=sample_query, candidates=test_chunks, top_k=5)
                pool_latencies.append((time.perf_counter() - t_p0) * 1000)

            p_p50, p_p95, p_mean = quantiles(pool_latencies)
            print(f"{f'Top-{pool_size} 候选池 (实测推理)':<20} | {f'{pool_size} pairs':<10} | {p_p50:<10.2f} | {p_p95:<10.2f} | {p_mean:<10.2f} | {'NOT VERIFIED (基准语料仅5分块，超出语料上限无法真实测试指标)'}")
        print("=" * 96)


if __name__ == "__main__":
    asyncio.run(run_benchmark())
