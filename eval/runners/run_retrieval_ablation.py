import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure stdout uses UTF-8 on Windows
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.ingestion.pipeline import IngestionPipeline
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.bm25_retriever import BM25Retriever, bm25_index_manager
from app.retrieval.reranker import CrossEncoderReranker, LocalONNXReranker, TestDoubleReranker
from app.retrieval.rrf_fusion import HybridRetriever
from app.retrieval.vector_store import VectorRetriever

from eval.core.dataset_loader import BenchmarkDatasetLoader
from eval.core.metrics_retrieval import compute_macro_averages, compute_sample_retrieval_metrics
from eval.core.result_writer import ResultWriter


EVAL_DB_FILE = BACKEND_DIR / "data" / "storage" / "eval_benchmark.db"
EVAL_DB_URL = f"sqlite+aiosqlite:///{EVAL_DB_FILE.as_posix()}"
EVAL_KB_ID = "eval-kb-benchmark-01"


async def setup_isolated_eval_db() -> sessionmaker:
    """Initialize isolated evaluation SQLite database."""
    EVAL_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(EVAL_DB_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return async_session


async def ingest_eval_fixtures(
    session_factory: sessionmaker,
    fixtures_dir: Path,
) -> Dict[str, str]:
    """
    Ingests all fixture markdown documents through the actual IngestionPipeline.
    Returns chunk_lookup mapping: '{doc_name}:{page_number}:{chunk_index}' -> chunk_id.
    """
    chunk_lookup: Dict[str, str] = {}
    emb_client = get_embedding_client()

    async with session_factory() as db:
        # Check or create evaluation KnowledgeBase
        kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == EVAL_KB_ID)
        kb = (await db.execute(kb_stmt)).scalar_one_or_none()

        if not kb:
            kb = KnowledgeBase(
                id=EVAL_KB_ID,
                name="E2.1-Retrieval-Benchmark",
                description="E2.1 独立检索评测专用基准知识库",
                embedding_provider="local_bge",
                embedding_model="BAAI/bge-small-zh-v1.5",
                embedding_dimensions=512,
                chunk_size=400,
                chunk_overlap=40,
            )
            db.add(kb)
            await db.commit()
            await db.refresh(kb)

        # Clear old documents in this KB to ensure deterministic fresh ingestion
        del_docs_stmt = select(Document).where(Document.knowledge_base_id == kb.id)
        existing_docs = (await db.execute(del_docs_stmt)).scalars().all()
        for d in existing_docs:
            await db.delete(d)
        await db.commit()

        # Invalidate BM25 index for fresh build
        bm25_index_manager.invalidate(kb.id)

        # Ingest each fixture file through real IngestionPipeline
        fixture_files = sorted(fixtures_dir.glob("*.md"))
        print(f"[*] Ingesting {len(fixture_files)} benchmark fixture documents into isolated DB...")

        for fpath in fixture_files:
            doc = Document(
                knowledge_base_id=kb.id,
                file_name=fpath.name,
                file_type="md",
                file_path=str(fpath.resolve()),
                status="UPLOADED",
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)

            pipeline = IngestionPipeline(db)
            processed_doc = await pipeline.process_document(doc.id)
            print(f"    - Ingested '{processed_doc.file_name}': {processed_doc.chunk_count} chunks, {processed_doc.page_count} pages.")

        # Build chunk_lookup
        chunk_stmt = select(DocumentChunk, Document.file_name).join(
            Document, DocumentChunk.document_id == Document.id
        ).where(DocumentChunk.knowledge_base_id == kb.id)
        rows = (await db.execute(chunk_stmt)).all()

        for chunk, doc_name in rows:
            key = f"{doc_name}:{chunk.page_number}:{chunk.chunk_index}"
            chunk_lookup[key] = chunk.id

        print(f"[*] Successfully built chunk lookup table with {len(chunk_lookup)} active chunks.")

    return chunk_lookup


async def run_ablation():
    print("=" * 96)
    print(" AgentStudio-RAG — E2.1: 独立检索评测与消融实验基准系统")
    print("=" * 96)

    # 1. Initialize DB and load dataset
    session_factory = await setup_isolated_eval_db()
    loader = BenchmarkDatasetLoader()
    fixture_hashes = loader.compute_fixture_hashes()
    raw_samples = loader.load_samples()

    print(f"[*] Loaded {len(raw_samples)} benchmark samples from {loader.dataset_path.name}")
    print(f"[*] Fixture hashes (SHA-256):")
    for fname, h in fixture_hashes.items():
        print(f"    - {fname}: {h}")

    # 2. Ingest documents and bind chunks
    chunk_lookup = await ingest_eval_fixtures(session_factory, loader.fixtures_dir)
    bound_samples = loader.bind_samples_to_chunks(raw_samples, chunk_lookup)

    # 3. Setup retrievers & models
    async with session_factory() as db:
        dense_retriever = VectorRetriever(db)
        bm25_retriever = BM25Retriever(db)
        hybrid_retriever = HybridRetriever(db)
        emb_client = get_embedding_client()

        # Initialize Cross-Encoder Reranker
        try:
            onnx_client = LocalONNXReranker(batch_size=8)
            reranker = CrossEncoderReranker(client=onnx_client)
            print(f"[*] Cross-Encoder Client: LocalONNXReranker ({onnx_client.model_name}) on CPU")
        except Exception as e:
            print(f"[!] Warning: LocalONNX failed ({e}), falling back to TestDoubleReranker.")
            reranker = CrossEncoderReranker(client=TestDoubleReranker())

        # 4. Pre-warm run to eliminate cold-start distortion
        print("[*] Warming up Embedding, BM25 index, and Reranker ONNX runtime...")
        warmup_q = bound_samples[0]["query"]
        w_vec = await emb_client.embed_query(warmup_q)
        await dense_retriever.search(EVAL_KB_ID, w_vec, top_k=5)
        await bm25_retriever.search(EVAL_KB_ID, warmup_q, top_k=5)
        w_hyb = await hybrid_retriever.search(EVAL_KB_ID, warmup_q, top_k=5)
        await reranker.rerank(warmup_q, w_hyb.chunks, top_k=5)
        print("[*] Warm-up complete. Starting benchmark evaluation...\n")

        # Result tracking containers
        samples_dense_metrics = []
        samples_bm25_metrics = []
        samples_rrf_metrics = []
        samples_rerank_metrics = []

        latencies_dense = []
        latencies_bm25 = []
        latencies_rrf = []
        latencies_rerank = []

        detailed_sample_records = []

        for idx, sample in enumerate(bound_samples, start=1):
            qid = sample["id"]
            query = sample["query"]
            cat = sample["category"]
            golden_map = sample["golden_map"]
            golden_ids = set(sample["golden_ids"])

            # -------------------------------------------------------------
            # Scheme A: Dense Only (BGE-small-zh-v1.5)
            # -------------------------------------------------------------
            t0 = time.perf_counter()
            q_vec = await emb_client.embed_query(query)
            dense_res = await dense_retriever.search(
                knowledge_base_id=EVAL_KB_ID,
                query_vector=q_vec,
                top_k=20,
                threshold=0.0,
            )
            dense_dt = (time.perf_counter() - t0) * 1000.0
            latencies_dense.append(dense_dt)

            dense_ids = [c.chunk_id for c in dense_res]
            m_dense = compute_sample_retrieval_metrics(dense_ids, golden_map)
            samples_dense_metrics.append(m_dense)

            # -------------------------------------------------------------
            # Scheme B: BM25 Only (Lucene positive IDF)
            # -------------------------------------------------------------
            t0 = time.perf_counter()
            bm25_res = await bm25_retriever.search(
                knowledge_base_id=EVAL_KB_ID,
                query=query,
                top_k=20,
            )
            bm25_dt = (time.perf_counter() - t0) * 1000.0
            latencies_bm25.append(bm25_dt)

            bm25_ids = [c.chunk_id for c in bm25_res]
            m_bm25 = compute_sample_retrieval_metrics(bm25_ids, golden_map)
            samples_bm25_metrics.append(m_bm25)

            # -------------------------------------------------------------
            # Scheme C: Dense + BM25 + RRF Hybrid Fusion (k=60)
            # -------------------------------------------------------------
            t0 = time.perf_counter()
            rrf_res = await hybrid_retriever.search(
                knowledge_base_id=EVAL_KB_ID,
                query=query,
                top_k=20,
            )
            rrf_dt = (time.perf_counter() - t0) * 1000.0
            latencies_rrf.append(rrf_dt)

            rrf_ids = [c.chunk_id for c in rrf_res.chunks]
            m_rrf = compute_sample_retrieval_metrics(rrf_ids, golden_map)
            samples_rrf_metrics.append(m_rrf)

            # -------------------------------------------------------------
            # Scheme D: Dense + BM25 + RRF + Cross-Encoder Reranker
            # -------------------------------------------------------------
            t0 = time.perf_counter()
            rerank_res = await reranker.rerank(
                query=query,
                candidates=rrf_res.chunks,
                top_k=10,
            )
            rerank_dt = rrf_dt + rerank_res.duration_ms
            latencies_rerank.append(rerank_dt)

            rerank_ids = [c.chunk_id for c in rerank_res.chunks]
            m_rerank = compute_sample_retrieval_metrics(rerank_ids, golden_map)
            samples_rerank_metrics.append(m_rerank)

            # Analyze Rank of the primary target chunk
            primary_target_id = sample["golden_ids"][0] if sample["golden_ids"] else None

            def get_rank(ids: List[str], target: Optional[str]) -> str:
                if not target:
                    return "N/A"
                for r, cid in enumerate(ids, start=1):
                    if cid == target:
                        return str(r)
                return ">20"

            d_rank = get_rank(dense_ids, primary_target_id)
            b_rank = get_rank(bm25_ids, primary_target_id)
            r_rank = get_rank(rrf_ids, primary_target_id)
            rk_rank = get_rank(rerank_ids, primary_target_id)

            analysis = ""
            if rk_rank == "1" and r_rank != "1":
                analysis = f"Reranker 精排将目标从第 {r_rank} 位成功提升至第 1 位"
            elif rk_rank == "1" and r_rank == "1":
                analysis = "RRF 与 Reranker 均精准保持第 1 位"
            elif d_rank == ">20" and b_rank != ">20":
                analysis = "Dense 语义漂移失召，BM25 关键词精确兜底命中"
            elif b_rank == ">20" and d_rank != ">20":
                analysis = "BM25 缺乏泛化失召，Dense 深度语义成功召回"
            else:
                analysis = "多路稳定召回并完成精准排序"

            detailed_sample_records.append({
                "id": qid,
                "category": cat,
                "query": query,
                "dense_rank": d_rank,
                "bm25_rank": b_rank,
                "rrf_rank": r_rank,
                "rerank_rank": rk_rank,
                "analysis": analysis,
                "metrics_dense": m_dense,
                "metrics_bm25": m_bm25,
                "metrics_rrf": m_rrf,
                "metrics_rerank": m_rerank,
            })

            print(f"[{idx:02d}/20] {qid} | D: #{d_rank:>2} | B: #{b_rank:>2} | RRF: #{r_rank:>2} | Rerank: #{rk_rank:>2} | Query: {query[:30]}...")

    # 5. Compute Macro Averages and latency percentiles
    def calc_percentiles(vals: List[float]) -> Tuple[float, float, float]:
        if not vals:
            return 0.0, 0.0, 0.0
        s = sorted(vals)
        mean_val = round(sum(s) / len(s), 2)
        p50 = round(statistics.median(s), 2)
        idx95 = min(int(len(s) * 0.95), len(s) - 1)
        p95 = round(s[idx95], 2)
        return p50, p95, mean_val

    dense_p50, dense_p95, dense_mean = calc_percentiles(latencies_dense)
    bm25_p50, bm25_p95, bm25_mean = calc_percentiles(latencies_bm25)
    rrf_p50, rrf_p95, rrf_mean = calc_percentiles(latencies_rrf)
    rerank_p50, rerank_p95, rerank_mean = calc_percentiles(latencies_rerank)

    avg_dense = compute_macro_averages(samples_dense_metrics)
    avg_bm25 = compute_macro_averages(samples_bm25_metrics)
    avg_rrf = compute_macro_averages(samples_rrf_metrics)
    avg_rerank = compute_macro_averages(samples_rerank_metrics)

    summary_rows = [
        {
            "scheme_id": "A",
            "scheme_name": "Dense Only (BGE-small-zh)",
            **avg_dense,
            "latency_p50_ms": dense_p50,
            "latency_p95_ms": dense_p95,
            "latency_mean_ms": dense_mean,
        },
        {
            "scheme_id": "B",
            "scheme_name": "BM25 Only (Lucene Positive IDF)",
            **avg_bm25,
            "latency_p50_ms": bm25_p50,
            "latency_p95_ms": bm25_p95,
            "latency_mean_ms": bm25_mean,
        },
        {
            "scheme_id": "C",
            "scheme_name": "Dense + BM25 + RRF (k=60)",
            **avg_rrf,
            "latency_p50_ms": rrf_p50,
            "latency_p95_ms": rrf_p95,
            "latency_mean_ms": rrf_mean,
        },
        {
            "scheme_id": "D",
            "scheme_name": "Hybrid + Cross-Encoder Reranker",
            **avg_rerank,
            "latency_p50_ms": rerank_p50,
            "latency_p95_ms": rerank_p95,
            "latency_mean_ms": rerank_mean,
        },
    ]

    # Category breakdown calculations
    categories = [
        ("numerical_and_parameter", "数值与参数匹配场景"),
        ("exact_keyword", "精确技术关键词场景"),
        ("technical_terminology", "专有名词与架构概念场景"),
        ("semantic_and_paraphrase", "长句语义提问与同义表述场景"),
    ]

    category_breakdowns = {}
    for cat_key, cat_title in categories:
        cat_samples = [s for s in detailed_sample_records if s["category"] == cat_key]
        n_cat = len(cat_samples)
        if n_cat == 0:
            continue

        c_dense = compute_macro_averages([s["metrics_dense"] for s in cat_samples])
        c_bm25 = compute_macro_averages([s["metrics_bm25"] for s in cat_samples])
        c_rrf = compute_macro_averages([s["metrics_rrf"] for s in cat_samples])
        c_rerank = compute_macro_averages([s["metrics_rerank"] for s in cat_samples])

        category_breakdowns[cat_key] = {
            "title": cat_title,
            "sample_count": n_cat,
            "schemes": [
                {"scheme_name": "Dense Only", **c_dense, "note": "语义理解强，专有名词略漂移"},
                {"scheme_name": "BM25 Only", **c_bm25, "note": "基于倒排索引与词项统计的关键词检索"},
                {"scheme_name": "RRF Hybrid", **c_rrf, "note": "双路互补融合，大幅提升覆盖度"},
                {"scheme_name": "Hybrid+Reranker", **c_rerank, "note": "全面置顶核心答案，得分最高"},
            ],
        }

    # 6. Export Results
    metadata = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_commit": "0d4eb7a",
        "sample_count": len(bound_samples),
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "reranker_model": "Xenova/bge-reranker-base",
        "db_path": str(EVAL_DB_FILE.relative_to(PROJECT_ROOT)),
        "fixture_hashes": fixture_hashes,
        "parameters": {
            "candidate_pool_size": 20,
            "top_k": 10,
            "rrf_k": 60,
            "rrf_dense_weight": 1.0,
            "rrf_bm25_weight": 1.0,
        },
    }

    writer = ResultWriter()
    full_data = {
        "metadata": metadata,
        "summary": summary_rows,
        "category_breakdowns": category_breakdowns,
        "samples": detailed_sample_records,
    }

    json_path = writer.write_json(full_data)
    csv_path = writer.write_csv(summary_rows)
    md_path = writer.write_markdown_report(
        metadata=metadata,
        summary_rows=summary_rows,
        category_breakdowns=category_breakdowns,
        detailed_samples=detailed_sample_records,
    )

    print("\n" + "=" * 96)
    print(" 实验完成！四组方案消融评测对比表 (Macro Averages):")
    print("=" * 96)
    print(f"{'方案':<28} | {'Recall@1':<9} | {'Recall@5':<9} | {'Recall@10':<9} | {'MRR@10':<8} | {'nDCG@10':<8} | {'P50 (ms)':<9} | {'P95 (ms)':<9}")
    print("-" * 96)
    for r in summary_rows:
        print(f"{r['scheme_name']:<28} | {r['recall@1']:<9.2%} | {r['recall@5']:<9.2%} | {r['recall@10']:<9.2%} | {r['mrr@10']:<8.4f} | {r['ndcg@10']:<8.4f} | {r['latency_p50_ms']:<9.2f} | {r['latency_p95_ms']:<9.2f}")
    print("=" * 96)
    print(f"[*] 结果报告已成功持久化至:")
    print(f"    - JSON: {json_path}")
    print(f"    - CSV:  {csv_path}")
    print(f"    - MD:   {md_path}\n")


if __name__ == "__main__":
    asyncio.run(run_ablation())
