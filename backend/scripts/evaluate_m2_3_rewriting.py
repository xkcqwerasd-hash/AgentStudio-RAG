import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import Base
from app.llm.embeddings import get_embedding_client
from app.llm.openai_client import get_llm_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.query_rewriter import QueryRewriter
from app.retrieval.vector_store import VectorRetriever


# Evaluation Corpus Chunks
CORPUS_CHUNKS = [
    {
        "id": "chunk-1-gateway-pool",
        "doc": "网关与连接池架构规范.md",
        "content": "微服务网关连接池配置规范：min_idle 最小空闲连接数固定为 10，max_connections 最大连接数为 100。连接池在服务启动时完成预热，超时释放空闲连接。",
    },
    {
        "id": "chunk-2-api-timeout",
        "doc": "网关与连接池架构规范.md",
        "content": "API 接口超时与重试规范：各微服务间内部 HTTP 调用超时阈值统一设定为 5000 毫秒（5秒）。若请求超时，由重试网关触发最多 2 次指数退避重试。",
    },
    {
        "id": "chunk-3-model-breaker",
        "doc": "大模型与熔断器架构规范.md",
        "content": "系统三态熔断器（ModelHealthStore）包含三种核心状态：CLOSED（正常）、OPEN（熔断开启）、HALF_OPEN（半开探测）。当过去 60 秒内调用失败次数达 5 次或失败率超过 30% 时，状态强制切为 OPEN 并阻断请求；CLOSED 状态下失败率阈值为 15%。",
    },
    {
        "id": "chunk-4-model-failover",
        "doc": "大模型与熔断器架构规范.md",
        "content": "多大模型主备降级与故障转移（Failover）规范：Primary 主选模型配置为 DeepSeek-V3；Secondary 备选模型配置为 SiliconFlow Qwen-2.5-72B；Fallback 兜底模型为本地轻量 Ollama。",
    },
    {
        "id": "chunk-5-redis-session",
        "doc": "用户鉴权与会话规范.md",
        "content": "分布式会话与认证规范：系统会话状态由 Redis 集群支持，在集群分片模式下部署。用户会话采用 JWT 无状态签名机制，Token 有效期过期时间为 72 小时，高危操作触发二次短信 OTP 验证。",
    },
    {
        "id": "chunk-6-bge-embeddings",
        "doc": "向量索引与Embedding规范.md",
        "content": "语义向量嵌入模型规范：系统当前采用 BAAI/bge-small-zh-v1.5 作为密集向量检索模型，向量维度为 512 维；对比 BAAI/bge-m3（1024 维密集多语言向量），bge-small 在 CPU 推理场景下具有极高吞吐与低显存优势。",
    },
    {
        "id": "chunk-7-python-decorators",
        "doc": "Python后端开发规范.md",
        "content": "Python 装饰器原理与工程规范：装饰器本质上是一个接收函数作为参数并返回新函数的高阶函数，语法糖 @ 简化了包装调用。工程上必须使用 @functools.wraps 保留原函数的 __name__ 和 __doc__ 元数据。",
    },
    {
        "id": "chunk-8-db-readwrite",
        "doc": "数据库与存储规范.md",
        "content": "数据库连接池与读写分离规范：主数据库负责 OLTP 写操作，只读副本负责全文分析与复杂报表查询。数据库连接池最小空闲为 5，最大连接数为 50。",
    },
]

# Benchmark Test Cases
TEST_CASES = [
    {
        "id": "TC-01",
        "category": "省略信息 (Ellipsis)",
        "history": [
            {"role": "user", "content": "网关连接池最大连接数是多少？"},
            {"role": "assistant", "content": "网关连接池最大连接数为100。"},
        ],
        "original_query": "那最小是多少？",
        "target_chunk_id": "chunk-1-gateway-pool",
        "notes": "缺失主语'网关连接池'和属性'最小空闲连接数'",
    },
    {
        "id": "TC-02",
        "category": "代词指代 (Pronoun)",
        "history": [
            {"role": "user", "content": "介绍一下系统的三态熔断器。"},
            {"role": "assistant", "content": "系统三态熔断器包含 CLOSED、OPEN、HALF_OPEN 三种状态。"},
        ],
        "original_query": "它什么时候进入 OPEN 状态？",
        "target_chunk_id": "chunk-3-model-breaker",
        "notes": "代词'它'指代'系统三态熔断器'",
    },
    {
        "id": "TC-03",
        "category": "序号指代 (Ordinal)",
        "history": [
            {"role": "user", "content": "系统三态熔断器包含 CLOSED、OPEN、HALF_OPEN 三种状态。"},
            {"role": "assistant", "content": "三种状态按健康度自动流转。"},
        ],
        "original_query": "第二种状态在什么条件下触发？",
        "target_chunk_id": "chunk-3-model-breaker",
        "notes": "'第二种状态'对应'OPEN 状态'",
    },
    {
        "id": "TC-04",
        "category": "连续追问 (Multi-turn)",
        "history": [
            {"role": "user", "content": "BGE-small 是什么模型？"},
            {"role": "assistant", "content": "BGE-small 是 BAAI 开源的 512 维语义向量模型。"},
            {"role": "user", "content": "它的向量维度是多少？"},
            {"role": "assistant", "content": "它的向量维度是 512 维。"},
        ],
        "original_query": "那和 BGE-M3 有什么区别？",
        "target_chunk_id": "chunk-6-bge-embeddings",
        "notes": "跨多轮上下文对比 BGE-small 与 BGE-M3",
    },
    {
        "id": "TC-05",
        "category": "独立新问题 (Independent)",
        "history": [
            {"role": "user", "content": "介绍一下系统三态熔断器。"},
            {"role": "assistant", "content": "熔断器包含 CLOSED、OPEN、HALF_OPEN。"},
        ],
        "original_query": "Python 装饰器的原理与语法糖是什么？",
        "target_chunk_id": "chunk-7-python-decorators",
        "notes": "与前文完全不相关的独立问题，严禁拼接熔断器",
    },
    {
        "id": "TC-06",
        "category": "实体省略 (Entity Omission)",
        "history": [
            {"role": "user", "content": "微服务网关的 API 接口规范是什么？"},
            {"role": "assistant", "content": "包含连接池、超时重试以及鉴权等机制。"},
        ],
        "original_query": "超时阈值统一设定为多少毫秒？",
        "target_chunk_id": "chunk-2-api-timeout",
        "notes": "省略主语'API 接口/微服务内部调用'",
    },
    {
        "id": "TC-07",
        "category": "指代转移 (Coreference Shift)",
        "history": [
            {"role": "user", "content": "用户认证与会话采用什么机制？"},
            {"role": "assistant", "content": "会话由 Redis 集群支持，Token 使用 JWT 签名。"},
        ],
        "original_query": "它的过期时间是多久？",
        "target_chunk_id": "chunk-5-redis-session",
        "notes": "代词'它'指代'JWT Token / 用户会话'",
    },
    {
        "id": "TC-08",
        "category": "无需重写完备问题 (Complete Query)",
        "history": [
            {"role": "user", "content": "介绍一下系统的缓存架构。"},
            {"role": "assistant", "content": "系统采用 Redis 分布式集群。"},
        ],
        "original_query": "详细解释分布式会话在 Redis 集群中的切片模式与高可用方案。",
        "target_chunk_id": "chunk-5-redis-session",
        "notes": "问题本身完备，无需重写",
    },
    {
        "id": "TC-09",
        "category": "负例/歧义指代 (Ambiguity Analysis)",
        "history": [
            {"role": "user", "content": "微服务网关与数据库连接池有什么区别？"},
            {"role": "assistant", "content": "网关连接池负责 HTTP 连接，数据库连接池负责 SQL 连接。"},
        ],
        "original_query": "它的最小空闲连接数是多少？",
        "target_chunk_id": "chunk-1-gateway-pool",
        "notes": "多实体共存的潜在歧义情况，分析重写偏向性",
    },
    {
        "id": "TC-10",
        "category": "长跨度多轮追问 (Long Context)",
        "history": [
            {"role": "user", "content": "系统三态熔断器包含哪些状态？"},
            {"role": "assistant", "content": "包含 CLOSED、OPEN、HALF_OPEN。"},
            {"role": "user", "content": "主备模型是如何配置的？"},
            {"role": "assistant", "content": "主选 DeepSeek，备选 SiliconFlow。"},
        ],
        "original_query": "它的 CLOSED 状态失败率阈值是多少？",
        "target_chunk_id": "chunk-3-model-breaker",
        "notes": "跨越中间无关模型话题，准确锚定熔断器 CLOSED 阈值",
    },
]


async def run_evaluation():
    print("=" * 70)
    print("AgentStudio-RAG M2.3 Query Rewriting Retrieval Evaluation Benchmark")
    print("=" * 70)

    # 1. Setup in-memory SQLite database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Ingest Corpus
    emb_client = get_embedding_client()
    print(f"Embedding Provider: {emb_client.provider_name} ({emb_client.model_name}, {emb_client.dimensions}d)")
    llm_client = get_llm_client()
    print(f"LLM Provider: {settings.LLM_PROVIDER} ({settings.LLM_MODEL})")

    async with async_session() as session:
        kb = KnowledgeBase(
            name="M2.3评测知识库",
            embedding_provider=emb_client.provider_name,
            embedding_model=emb_client.model_name,
            embedding_dimensions=emb_client.dimensions,
        )
        session.add(kb)
        await session.flush()

        doc = Document(
            knowledge_base_id=kb.id,
            file_name="benchmark_corpus.md",
            file_type="markdown",
            file_size=2000,
            file_path="/tmp/benchmark_corpus.md",
            status="COMPLETED",
            chunk_count=len(CORPUS_CHUNKS),
        )
        session.add(doc)
        await session.flush()

        texts = [c["content"] for c in CORPUS_CHUNKS]
        vectors = await emb_client.embed_documents(texts)

        for i, c in enumerate(CORPUS_CHUNKS):
            chunk = DocumentChunk(
                id=c["id"],
                document_id=doc.id,
                knowledge_base_id=kb.id,
                chunk_index=i,
                content=c["content"],
                page_number=1,
                char_length=len(c["content"]),
            )
            chunk.embedding = vectors[i]
            session.add(chunk)
        await session.commit()
        print(f"Successfully ingested {len(CORPUS_CHUNKS)} benchmark chunks into SQLite.\n")

    # 3. Initialize Evaluator
    query_rewriter = QueryRewriter(llm_client=llm_client)

    results = []
    orig_recalls_at_1 = []
    orig_recalls_at_3 = []
    orig_recalls_at_5 = []
    orig_mrrs = []

    rewr_recalls_at_1 = []
    rewr_recalls_at_3 = []
    rewr_recalls_at_5 = []
    rewr_mrrs = []

    print(f"{'ID':<6} | {'分类':<22} | {'原始问题':<22} | {'重写后检索Query':<30} | {'原Recall@1':<10} | {'新Recall@1':<10} | {'MRR提升'}")
    print("-" * 125)

    async with async_session() as session:
        retriever = VectorRetriever(session)

        for tc in TEST_CASES:
            target_id = tc["target_chunk_id"]

            # Method A: Original Query Retrieval
            t0 = time.perf_counter()
            orig_vec = await emb_client.embed_query(tc["original_query"])
            orig_chunks = await retriever.search(
                knowledge_base_id=kb.id,
                query_vector=orig_vec,
                top_k=5,
                threshold=0.0,
            )
            orig_retrieval_ms = (time.perf_counter() - t0) * 1000
            orig_ids = [c.chunk_id for c in orig_chunks]

            orig_r1 = 1 if target_id in orig_ids[:1] else 0
            orig_r3 = 1 if target_id in orig_ids[:3] else 0
            orig_r5 = 1 if target_id in orig_ids[:5] else 0
            orig_mrr = 1.0 / (orig_ids.index(target_id) + 1) if target_id in orig_ids else 0.0

            orig_recalls_at_1.append(orig_r1)
            orig_recalls_at_3.append(orig_r3)
            orig_recalls_at_5.append(orig_r5)
            orig_mrrs.append(orig_mrr)

            # Method B: Rewritten Query Retrieval
            rw_result = await query_rewriter.rewrite(
                query=tc["original_query"],
                history_messages=tc["history"],
            )

            t1 = time.perf_counter()
            rewr_vec = await emb_client.embed_query(rw_result.rewritten_query)
            rewr_chunks = await retriever.search(
                knowledge_base_id=kb.id,
                query_vector=rewr_vec,
                top_k=5,
                threshold=0.0,
            )
            rewr_retrieval_ms = (time.perf_counter() - t1) * 1000
            rewr_ids = [c.chunk_id for c in rewr_chunks]

            rewr_r1 = 1 if target_id in rewr_ids[:1] else 0
            rewr_r3 = 1 if target_id in rewr_ids[:3] else 0
            rewr_r5 = 1 if target_id in rewr_ids[:5] else 0
            rewr_mrr = 1.0 / (rewr_ids.index(target_id) + 1) if target_id in rewr_ids else 0.0

            rewr_recalls_at_1.append(rewr_r1)
            rewr_recalls_at_3.append(rewr_r3)
            rewr_recalls_at_5.append(rewr_r5)
            rewr_mrrs.append(rewr_mrr)

            mrr_delta = rewr_mrr - orig_mrr
            mrr_delta_str = f"{mrr_delta:+.2f}" if mrr_delta != 0 else "0.00"

            orig_disp = tc["original_query"][:18] + (".." if len(tc["original_query"]) > 18 else "")
            rewr_disp = rw_result.rewritten_query[:26] + (".." if len(rw_result.rewritten_query) > 26 else "")

            print(f"{tc['id']:<6} | {tc['category']:<22} | {orig_disp:<22} | {rewr_disp:<30} | {orig_r1:<10} | {rewr_r1:<10} | {mrr_delta_str}")

            results.append({
                "id": tc["id"],
                "category": tc["category"],
                "original_query": tc["original_query"],
                "rewritten_query": rw_result.rewritten_query,
                "is_rewritten": rw_result.is_rewritten,
                "rewrite_duration_ms": rw_result.duration_ms,
                "target_chunk_id": target_id,
                "original_search": {
                    "top_chunks": orig_ids,
                    "recall@1": orig_r1,
                    "recall@3": orig_r3,
                    "recall@5": orig_r5,
                    "mrr": round(orig_mrr, 4),
                    "retrieval_ms": round(orig_retrieval_ms, 2),
                },
                "rewritten_search": {
                    "top_chunks": rewr_ids,
                    "recall@1": rewr_r1,
                    "recall@3": rewr_r3,
                    "recall@5": rewr_r5,
                    "mrr": round(rewr_mrr, 4),
                    "retrieval_ms": round(rewr_retrieval_ms, 2),
                },
                "mrr_delta": round(mrr_delta, 4),
                "notes": tc["notes"],
            })

    # Summary
    avg_orig_r1 = sum(orig_recalls_at_1) / len(TEST_CASES)
    avg_orig_r3 = sum(orig_recalls_at_3) / len(TEST_CASES)
    avg_orig_r5 = sum(orig_recalls_at_5) / len(TEST_CASES)
    avg_orig_mrr = sum(orig_mrrs) / len(TEST_CASES)

    avg_rewr_r1 = sum(rewr_recalls_at_1) / len(TEST_CASES)
    avg_rewr_r3 = sum(rewr_recalls_at_3) / len(TEST_CASES)
    avg_rewr_r5 = sum(rewr_recalls_at_5) / len(TEST_CASES)
    avg_rewr_mrr = sum(rewr_mrrs) / len(TEST_CASES)

    print("\n" + "=" * 70)
    print("Benchmark Metrics Summary:")
    print(f"Metric      | Original Query (A) | Rewritten Query (B) | Delta")
    print(f"Recall@1    | {avg_orig_r1 * 100:>16.1f}% | {avg_rewr_r1 * 100:>17.1f}% | {(avg_rewr_r1 - avg_orig_r1) * 100:>+5.1f}%")
    print(f"Recall@3    | {avg_orig_r3 * 100:>16.1f}% | {avg_rewr_r3 * 100:>17.1f}% | {(avg_rewr_r3 - avg_orig_r3) * 100:>+5.1f}%")
    print(f"Recall@5    | {avg_orig_r5 * 100:>16.1f}% | {avg_rewr_r5 * 100:>17.1f}% | {(avg_rewr_r5 - avg_orig_r5) * 100:>+5.1f}%")
    print(f"MRR         | {avg_orig_mrr:>17.4f} | {avg_rewr_mrr:>18.4f} | {avg_rewr_mrr - avg_orig_mrr:>+6.4f}")
    print("=" * 70)

    # Output JSON report
    output_path = Path("docs/m2_3_rewrite_eval_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_data = {
        "benchmark": "M2.3 Context-aware Query Rewriting Evaluation",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "corpus_size": len(CORPUS_CHUNKS),
        "test_cases_count": len(TEST_CASES),
        "embedding_model": emb_client.model_name,
        "llm_model": settings.LLM_MODEL,
        "metrics_summary": {
            "original_query": {
                "recall@1": avg_orig_r1,
                "recall@3": avg_orig_r3,
                "recall@5": avg_orig_r5,
                "mrr": avg_orig_mrr,
            },
            "rewritten_query": {
                "recall@1": avg_rewr_r1,
                "recall@3": avg_rewr_r3,
                "recall@5": avg_rewr_r5,
                "mrr": avg_rewr_mrr,
            },
            "delta": {
                "recall@1_delta": round(avg_rewr_r1 - avg_orig_r1, 4),
                "recall@3_delta": round(avg_rewr_r3 - avg_orig_r3, 4),
                "recall@5_delta": round(avg_rewr_r5 - avg_orig_r5, 4),
                "mrr_delta": round(avg_rewr_mrr - avg_orig_mrr, 4),
            },
        },
        "details": results,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)
    print(f"\nDetailed evaluation report saved to: {output_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(run_evaluation())
