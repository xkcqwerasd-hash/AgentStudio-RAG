#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentStudio-RAG — M6.2 Stage 2-B: One-off Retrieval Ablation Benchmark Runner.

Strict Execution Boundaries:
- Local CPU only, NO external LLM API calls (0 token cost).
- Independent in-memory evaluation, ZERO pollution to production database (agentstudio_rag.db).
- Uses production classes: TechnicalBM25Tokenizer, BM25KBIndex, rrf_fuse, CrossEncoderReranker, LocalBGEEmbeddingClient.
- 3 Warmup queries + 3 Repeated measurement rounds per query.
- Evaluates 4 Schemes:
  1. Dense Only (VectorRetriever / Cosine)
  2. BM25 Only (BM25Retriever / Lucene)
  3. Hybrid RRF (Dense + BM25 reciprocal rank fusion, k=60)
  4. Hybrid RRF + Cross-Encoder Reranker (ONNX INT8 bge-reranker-base)
- Metrics: Recall@1/3/5, StrictRecall@1/3/5, MRR, nDCG@5, Latency (mean, P50, P95, breakdown).
- Outputs: m6_2_results.json, m6_2_summary.csv, m6_2_query_details.csv, SVG visualization charts.
"""

import asyncio
import csv
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Adjust python sys.path so backend/app can be imported directly
BACKEND_DIR = Path(__file__).resolve().parents[3]  # backend
PROJECT_ROOT = Path(__file__).resolve().parents[4]  # repo root
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.llm.embeddings import get_embedding_client
from app.retrieval.bm25_retriever import BM25KBIndex, TechnicalBM25Tokenizer
from app.retrieval.reranker import CrossEncoderReranker
from app.retrieval.rrf_fusion import rrf_fuse
from app.retrieval.vector_store import ScoredChunk

# Set stdout encoding for Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class InMemoryVectorRetriever:
    """
    In-memory vector retriever implementing the exact cosine similarity search
    from app.retrieval.vector_store.VectorRetriever without touching SQLite.
    """

    def __init__(self, chunks: List[Dict[str, Any]]):
        self.chunks = chunks
        self.chunk_ids = [c["chunk_id"] for c in chunks]
        self.embeddings = np.array([c["embedding"] for c in chunks], dtype=np.float32)
        self.norms = np.linalg.norm(self.embeddings, axis=1)

    def search(
        self,
        query_vector: List[float],
        top_k: int = 20,
        threshold: float = 0.0,
    ) -> List[ScoredChunk]:
        q_vec = np.array(query_vector, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm < 1e-9:
            return []

        dot_products = np.dot(self.embeddings, q_vec)
        cos_sims = dot_products / (self.norms * q_norm)

        scored_items: List[ScoredChunk] = []
        for idx, cos_sim in enumerate(cos_sims):
            if cos_sim >= threshold:
                c = self.chunks[idx]
                scored_items.append(
                    ScoredChunk(
                        chunk_id=c["chunk_id"],
                        document_id=c["document_id"],
                        document_name=c.get("source", c.get("document_name", "")),
                        page_number=c.get("page_number", 1),
                        chunk_index=c.get("chunk_index", 0),
                        content=c["content"],
                        similarity_score=round(float(cos_sim), 4),
                    )
                )

        scored_items.sort(key=lambda x: x.similarity_score, reverse=True)
        return scored_items[:top_k]


def compute_metrics(
    retrieved_chunks: List[ScoredChunk],
    qrel_dict: Dict[str, int],
    cutoffs: Tuple[int, ...] = (1, 3, 5),
) -> Dict[str, float]:
    """
    Computes standard IR metrics against ground truth qrels:
    - Recall@1, Recall@3, Recall@5 (Relevance >= 1)
    - StrictRecall@1, StrictRecall@3, StrictRecall@5 (Relevance == 2)
    - MRR (cutoff 5)
    - nDCG@5 (Graded relevance 2^rel - 1)
    """
    # Ground truth sets
    pos_targets = {cid for cid, rel in qrel_dict.items() if rel >= 1}
    core_targets = {cid for cid, rel in qrel_dict.items() if rel == 2}

    num_pos = len(pos_targets)
    num_core = len(core_targets)

    metrics: Dict[str, float] = {}

    # Recall at cutoffs
    for k in cutoffs:
        top_k_chunks = retrieved_chunks[:k]
        top_k_cids = {c.chunk_id for c in top_k_chunks}

        hits = len(top_k_cids & pos_targets)
        recall = hits / num_pos if num_pos > 0 else 0.0
        metrics[f"recall@{k}"] = round(recall, 4)

        strict_hits = len(top_k_cids & core_targets)
        strict_recall = strict_hits / num_core if num_core > 0 else 0.0
        metrics[f"strict_recall@{k}"] = round(strict_recall, 4)

    # MRR (Mean Reciprocal Rank within top-5)
    rr = 0.0
    for idx, c in enumerate(retrieved_chunks[:5], start=1):
        if c.chunk_id in pos_targets:
            rr = 1.0 / idx
            break
    metrics["mrr"] = round(rr, 4)

    # nDCG@5
    dcg = 0.0
    for idx, c in enumerate(retrieved_chunks[:5], start=1):
        rel = qrel_dict.get(c.chunk_id, 0)
        gain = (2.0 ** rel) - 1.0
        discount = math.log2(idx + 1)
        dcg += gain / discount

    # Ideal DCG@5
    ideal_scores = sorted(
        [rel for rel in qrel_dict.values() if rel >= 1],
        reverse=True,
    )[:5]
    idcg = 0.0
    for idx, rel in enumerate(ideal_scores, start=1):
        gain = (2.0 ** rel) - 1.0
        discount = math.log2(idx + 1)
        idcg += gain / discount

    ndcg = dcg / idcg if idcg > 1e-9 else 0.0
    metrics["ndcg@5"] = round(ndcg, 4)

    return metrics


def generate_svg_metrics_chart(summary: Dict[str, Dict[str, float]], filepath: str) -> None:
    """
    Generates a clean standalone SVG grouped bar chart comparing metrics across schemes.
    """
    schemes = ["dense_only", "bm25_only", "hybrid_rrf", "hybrid_reranker"]
    scheme_labels = ["Dense Only", "BM25 Only", "Hybrid RRF", "Hybrid + Reranker"]
    colors = ["#3b82f6", "#10b981", "#8b5cf6", "#f59e0b"]
    metric_keys = ["recall@1", "recall@3", "recall@5", "mrr", "ndcg@5"]
    metric_labels = ["Recall@1", "Recall@3", "Recall@5", "MRR", "nDCG@5"]

    svg_w = 900
    svg_h = 450
    margin_l = 80
    margin_r = 40
    margin_t = 60
    margin_b = 60

    plot_w = svg_w - margin_l - margin_r
    plot_h = svg_h - margin_t - margin_b

    num_groups = len(metric_keys)
    group_width = plot_w / num_groups
    bar_width = group_width / (len(schemes) + 1.2)

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {svg_w} {svg_h}" width="{svg_w}" height="{svg_h}" style="background:#ffffff; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">',
        '  <style>',
        '    .title { font-size:18px; font-weight:700; fill:#1e293b; }',
        '    .subtitle { font-size:12px; fill:#64748b; }',
        '    .grid { stroke:#e2e8f0; stroke-width:1; stroke-dasharray:4 4; }',
        '    .axis { stroke:#94a3b8; stroke-width:1.5; }',
        '    .tick-label { font-size:12px; fill:#475569; text-anchor:middle; font-weight:500; }',
        '    .y-label { font-size:11px; fill:#64748b; text-anchor:end; }',
        '    .val-label { font-size:10px; font-weight:600; text-anchor:middle; }',
        '    .legend-text { font-size:12px; fill:#334155; font-weight:500; }',
        '  </style>',
        f'  <text x="{margin_l}" y="32" class="title">AgentStudio-RAG — M6.2 Retrieval Ablation Benchmark</text>',
        f'  <text x="{margin_l}" y="48" class="subtitle">Comparison of IR Metrics: Recall@1/3/5, MRR, nDCG@5 across 60 Queries</text>',
    ]

    # Legend
    legend_x = svg_w - 480
    for idx, (label, color) in enumerate(zip(scheme_labels, colors)):
        lx = legend_x + idx * 115
        svg_lines.append(f'  <rect x="{lx}" y="30" width="12" height="12" rx="3" fill="{color}"/>')
        svg_lines.append(f'  <text x="{lx + 16}" y="40" class="legend-text">{label}</text>')

    # Y-axis grid and ticks (0.0 to 1.0)
    for y_val in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
        y_pos = margin_t + plot_h - (y_val * plot_h)
        svg_lines.append(f'  <line x1="{margin_l}" y1="{y_pos}" x2="{svg_w - margin_r}" y2="{y_pos}" class="grid"/>')
        svg_lines.append(f'  <text x="{margin_l - 8}" y="{y_pos + 4}" class="y-label">{y_val:.1f}</text>')

    # X-axis line
    svg_lines.append(f'  <line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{svg_w - margin_r}" y2="{margin_t + plot_h}" class="axis"/>')

    # Bars
    for g_idx, m_key in enumerate(metric_keys):
        g_center_x = margin_l + g_idx * group_width + group_width / 2
        g_start_x = g_center_x - (len(schemes) * bar_width) / 2

        # Metric group label
        svg_lines.append(f'  <text x="{g_center_x}" y="{margin_t + plot_h + 24}" class="tick-label">{metric_labels[g_idx]}</text>')

        for s_idx, (s_key, color) in enumerate(zip(schemes, colors)):
            val = summary.get(s_key, {}).get(m_key, 0.0)
            bar_h = val * plot_h
            bar_x = g_start_x + s_idx * bar_width
            bar_y = margin_t + plot_h - bar_h

            svg_lines.append(f'  <rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{bar_width - 4:.1f}" height="{bar_h:.1f}" rx="2" fill="{color}"/>')
            if bar_h > 15:
                svg_lines.append(f'  <text x="{bar_x + (bar_width - 4)/2:.1f}" y="{bar_y - 4:.1f}" class="val-label" fill="{color}">{val:.2f}</text>')

    svg_lines.append('</svg>')

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(svg_lines))


def generate_svg_latency_chart(summary: Dict[str, Dict[str, float]], filepath: str) -> None:
    """
    Generates a clean standalone SVG bar chart comparing Mean, P50, and P95 Latencies (ms).
    """
    schemes = ["dense_only", "bm25_only", "hybrid_rrf", "hybrid_reranker"]
    scheme_labels = ["Dense Only", "BM25 Only", "Hybrid RRF", "Hybrid + Reranker"]
    stat_keys = ["mean_ms", "p50_ms", "p95_ms"]
    stat_labels = ["Mean", "P50 (Median)", "P95"]
    colors = ["#0284c7", "#10b981", "#ef4444"]

    svg_w = 900
    svg_h = 450
    margin_l = 80
    margin_r = 40
    margin_t = 60
    margin_b = 60

    plot_w = svg_w - margin_l - margin_r
    plot_h = svg_h - margin_t - margin_b

    # Find max latency to scale Y axis
    max_lat = 0.0
    for s_key in schemes:
        for st in stat_keys:
            val = summary.get(s_key, {}).get(f"lat_{st}", 0.0)
            if val > max_lat:
                max_lat = val

    max_y = math.ceil(max_lat * 1.15 / 50.0) * 50.0 if max_lat > 0 else 100.0

    num_groups = len(schemes)
    group_width = plot_w / num_groups
    bar_width = group_width / (len(stat_keys) + 1.2)

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {svg_w} {svg_h}" width="{svg_w}" height="{svg_h}" style="background:#ffffff; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">',
        '  <style>',
        '    .title { font-size:18px; font-weight:700; fill:#1e293b; }',
        '    .subtitle { font-size:12px; fill:#64748b; }',
        '    .grid { stroke:#e2e8f0; stroke-width:1; stroke-dasharray:4 4; }',
        '    .axis { stroke:#94a3b8; stroke-width:1.5; }',
        '    .tick-label { font-size:13px; fill:#1e293b; text-anchor:middle; font-weight:600; }',
        '    .y-label { font-size:11px; fill:#64748b; text-anchor:end; }',
        '    .val-label { font-size:10px; font-weight:600; text-anchor:middle; }',
        '    .legend-text { font-size:12px; fill:#334155; font-weight:500; }',
        '  </style>',
        f'  <text x="{margin_l}" y="32" class="title">AgentStudio-RAG — Retrieval Latency Benchmark</text>',
        f'  <text x="{margin_l}" y="48" class="subtitle">Comparison of Retrieval Latencies (Mean, P50, P95 in ms) across 60 Queries</text>',
    ]

    # Legend
    legend_x = svg_w - 380
    for idx, (label, color) in enumerate(zip(stat_labels, colors)):
        lx = legend_x + idx * 115
        svg_lines.append(f'  <rect x="{lx}" y="30" width="12" height="12" rx="3" fill="{color}"/>')
        svg_lines.append(f'  <text x="{lx + 16}" y="40" class="legend-text">{label}</text>')

    # Y-axis ticks
    num_ticks = 5
    for i in range(num_ticks + 1):
        y_val = (max_y / num_ticks) * i
        y_pos = margin_t + plot_h - (y_val / max_y * plot_h)
        svg_lines.append(f'  <line x1="{margin_l}" y1="{y_pos}" x2="{svg_w - margin_r}" y2="{y_pos}" class="grid"/>')
        svg_lines.append(f'  <text x="{margin_l - 8}" y="{y_pos + 4}" class="y-label">{int(y_val)} ms</text>')

    # X-axis line
    svg_lines.append(f'  <line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{svg_w - margin_r}" y2="{margin_t + plot_h}" class="axis"/>')

    # Bars
    for g_idx, s_key in enumerate(schemes):
        g_center_x = margin_l + g_idx * group_width + group_width / 2
        g_start_x = g_center_x - (len(stat_keys) * bar_width) / 2

        svg_lines.append(f'  <text x="{g_center_x}" y="{margin_t + plot_h + 24}" class="tick-label">{scheme_labels[g_idx]}</text>')

        for st_idx, (st, color) in enumerate(zip(stat_keys, colors)):
            val = summary.get(s_key, {}).get(f"lat_{st}", 0.0)
            bar_h = (val / max_y) * plot_h
            bar_x = g_start_x + st_idx * bar_width
            bar_y = margin_t + plot_h - bar_h

            svg_lines.append(f'  <rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{bar_width - 4:.1f}" height="{bar_h:.1f}" rx="2" fill="{color}"/>')
            if bar_h > 15:
                svg_lines.append(f'  <text x="{bar_x + (bar_width - 4)/2:.1f}" y="{bar_y - 4:.1f}" class="val-label" fill="{color}">{val:.1f}ms</text>')

    svg_lines.append('</svg>')

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(svg_lines))


async def main():
    root_dir = PROJECT_ROOT
    corpus_file = root_dir / "backend" / "benchmark" / "retrieval" / "corpus" / "corpus_chunks.jsonl"
    queries_file = root_dir / "backend" / "benchmark" / "retrieval" / "queries" / "queries.jsonl"
    qrels_file = root_dir / "backend" / "benchmark" / "retrieval" / "qrels" / "qrels.jsonl"

    results_dir = root_dir / "backend" / "benchmark" / "retrieval" / "results"
    data_dir = root_dir / "data" / "benchmark"
    results_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print(" AgentStudio-RAG — M6.2 Stage 2-B: One-off Retrieval Ablation Benchmark")
    print("=" * 80)

    # 1. Load Corpus
    print("[1/5] Loading Corpus Chunks into Memory...")
    chunks: List[Dict[str, Any]] = []
    with open(corpus_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                chunks.append(json.loads(line))
    print(f"  -> Total Chunks loaded: {len(chunks)}")

    # 2. Build In-Memory Retrievers
    print("[2/5] Building In-Memory Retrievers...")
    vector_retriever = InMemoryVectorRetriever(chunks)
    print("  -> InMemoryVectorRetriever initialized (512-dim cosine).")

    bm25_index = BM25KBIndex(knowledge_base_id="benchmark_kb")
    bm25_index.build_from_chunks([
        {
            "chunk_id": c["chunk_id"],
            "document_id": c["document_id"],
            "document_name": c.get("source", ""),
            "page_number": c.get("page_number", 1),
            "chunk_index": c.get("chunk_index", 0),
            "content": c["content"],
        }
        for c in chunks
    ])
    print(f"  -> BM25KBIndex built ({len(bm25_index.df)} unique terms in inverted index).")

    embed_client = get_embedding_client()
    print(f"  -> Local Embedding Client ready: {embed_client.model_name}")

    reranker = CrossEncoderReranker()
    print(f"  -> CrossEncoderReranker ready: {reranker.client.model_name} (provider: {reranker.client.provider_name})")

    # 3. Load Queries and QRELs
    print("[3/5] Loading Queries and Ground Truth Annotations...")
    queries: List[Dict[str, Any]] = []
    with open(queries_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                queries.append(json.loads(line))
    print(f"  -> Total Queries: {len(queries)}")

    qrel_map: Dict[str, Dict[str, int]] = {}
    with open(qrels_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                qid = item["query_id"]
                cid = item["chunk_id"]
                rel = item["relevance"]
                if qid not in qrel_map:
                    qrel_map[qid] = {}
                qrel_map[qid][cid] = rel
    print(f"  -> Total QREL mappings loaded for {len(qrel_map)} queries.")

    # 4. Warmup Phase (3 Queries)
    print("\n[4/5] Executing Warmup Phase (3 queries to eliminate cold start)...")
    warmup_queries = [queries[0]["query"], queries[10]["query"], queries[20]["query"]]
    for w_idx, w_q in enumerate(warmup_queries, start=1):
        q_vec = await embed_client.embed_query(w_q)
        w_dense = vector_retriever.search(q_vec, top_k=20)
        w_bm25 = bm25_index.search(w_q, top_k=20)
        w_rrf = rrf_fuse(w_dense, w_bm25, k=60, top_k=20)
        await reranker.rerank(w_q, w_rrf, top_k=5)
        print(f"  -> Warmup {w_idx}/3 finished.")
    print("  -> Warmup complete! CPU threads, caches, and ONNX session stabilized.\n")

    # 5. Formal Benchmark Execution
    print("[5/5] Executing 4-Scheme Benchmark on 60 Queries (3 rounds each)...")
    schemes = ["dense_only", "bm25_only", "hybrid_rrf", "hybrid_reranker"]

    # Store full results
    query_results: List[Dict[str, Any]] = []
    rounds = 3

    # Collectors for global summary
    scheme_metrics_collector: Dict[str, List[Dict[str, float]]] = {s: [] for s in schemes}
    scheme_latencies_collector: Dict[str, List[float]] = {s: [] for s in schemes}
    stage_latencies_collector: Dict[str, List[float]] = {
        "dense_embed": [],
        "dense_scan": [],
        "bm25_search": [],
        "rrf_fuse": [],
        "reranker_infer": [],
    }

    # Category breakdown collectors
    categories = sorted(list({q["category"] for q in queries}))
    cat_metrics_collector: Dict[str, Dict[str, List[Dict[str, float]]]] = {
        cat: {s: [] for s in schemes} for cat in categories
    }

    t_benchmark_start = time.perf_counter()

    for q_idx, q_item in enumerate(queries, start=1):
        qid = q_item["query_id"]
        q_text = q_item["query"]
        q_cat = q_item["category"]
        qrels = qrel_map.get(qid, {})

        # Storage for this query across rounds
        q_record: Dict[str, Any] = {
            "query_id": qid,
            "category": q_cat,
            "query": q_text,
            "schemes": {},
        }

        # Scheme execution data
        # For evaluation metrics, results are deterministic, so we evaluate once on round 1 candidates
        scheme_latencies: Dict[str, List[float]] = {s: [] for s in schemes}
        scheme_candidates: Dict[str, List[ScoredChunk]] = {}

        for round_idx in range(1, rounds + 1):
            # 1. Dense Only
            t0 = time.perf_counter()
            q_vec = await embed_client.embed_query(q_text)
            t1 = time.perf_counter()
            dense_top20 = vector_retriever.search(q_vec, top_k=20)
            t2 = time.perf_counter()

            dense_lat = (t2 - t0) * 1000.0
            scheme_latencies["dense_only"].append(dense_lat)
            if round_idx == 1:
                scheme_candidates["dense_only"] = dense_top20
                stage_latencies_collector["dense_embed"].append((t1 - t0) * 1000.0)
                stage_latencies_collector["dense_scan"].append((t2 - t1) * 1000.0)

            # 2. BM25 Only
            t3 = time.perf_counter()
            bm25_top20 = bm25_index.search(q_text, top_k=20)
            t4 = time.perf_counter()

            bm25_lat = (t4 - t3) * 1000.0
            scheme_latencies["bm25_only"].append(bm25_lat)
            if round_idx == 1:
                scheme_candidates["bm25_only"] = bm25_top20
                stage_latencies_collector["bm25_search"].append(bm25_lat)

            # 3. Hybrid RRF
            t5 = time.perf_counter()
            rrf_top20 = rrf_fuse(dense_top20, bm25_top20, k=60, top_k=20)
            t6 = time.perf_counter()

            rrf_fuse_lat = (t6 - t5) * 1000.0
            # Total RRF pipeline latency = dense + bm25 + fusion
            total_rrf_lat = dense_lat + bm25_lat + rrf_fuse_lat
            scheme_latencies["hybrid_rrf"].append(total_rrf_lat)
            if round_idx == 1:
                scheme_candidates["hybrid_rrf"] = rrf_top20
                stage_latencies_collector["rrf_fuse"].append(rrf_fuse_lat)

            # 4. Hybrid RRF + Cross-Encoder Reranker
            t7 = time.perf_counter()
            rerank_result = await reranker.rerank(query=q_text, candidates=rrf_top20, top_k=5)
            t8 = time.perf_counter()

            rerank_infer_lat = (t8 - t7) * 1000.0
            # Total Reranker pipeline latency = RRF pipeline + rerank inference
            total_rerank_pipeline_lat = total_rrf_lat + rerank_infer_lat
            scheme_latencies["hybrid_reranker"].append(total_rerank_pipeline_lat)
            if round_idx == 1:
                scheme_candidates["hybrid_reranker"] = rerank_result.chunks
                stage_latencies_collector["reranker_infer"].append(rerank_infer_lat)

        # Process each scheme for this query
        for s in schemes:
            lats = scheme_latencies[s]
            mean_lat = round(float(np.mean(lats)), 2)
            p50_lat = round(float(np.percentile(lats, 50)), 2)
            p95_lat = round(float(np.percentile(lats, 95)), 2)

            scheme_latencies_collector[s].extend(lats)

            # Metrics
            cands = scheme_candidates[s]
            metrics = compute_metrics(cands, qrels, cutoffs=(1, 3, 5))
            scheme_metrics_collector[s].append(metrics)
            cat_metrics_collector[q_cat][s].append(metrics)

            # Candidate details
            annotated_cands = []
            for rank_idx, c in enumerate(cands[:5], start=1):
                cid = c.chunk_id
                rel = qrels.get(cid, None)
                is_ann = cid in qrels
                if rel == 2:
                    ann_type = "core_evidence"
                elif rel == 1:
                    ann_type = "supporting_context"
                elif rel == 0:
                    ann_type = "confirmed_negative"
                else:
                    ann_type = "unannotated_candidate"

                # Candidate score according to scheme
                if s == "dense_only":
                    sc = c.similarity_score
                elif s == "bm25_only":
                    sc = c.similarity_score
                elif s == "hybrid_rrf":
                    sc = round(c.fusion_score or 0.0, 5)
                else:
                    sc = round(c.reranker_raw_score or c.similarity_score, 4)

                annotated_cands.append({
                    "rank": rank_idx,
                    "chunk_id": cid,
                    "document_id": c.document_id,
                    "score": sc,
                    "ground_truth_relevance": rel,
                    "is_annotated": is_ann,
                    "annotation_type": ann_type,
                    "content_snippet": c.content[:80] + "..." if len(c.content) > 80 else c.content,
                })

            q_record["schemes"][s] = {
                "metrics": metrics,
                "latency_ms": {
                    "mean": mean_lat,
                    "p50": p50_lat,
                    "p95": p95_lat,
                    "rounds": [round(l, 2) for l in lats],
                },
                "top5_candidates": annotated_cands,
            }

        query_results.append(q_record)
        if q_idx % 10 == 0 or q_idx == len(queries):
            print(f"  -> Evaluated {q_idx}/60 queries...")

    total_bench_duration = time.perf_counter() - t_benchmark_start
    print(f"\n[OK] Benchmark execution completed in {total_bench_duration:.2f} seconds!")

    # 6. Compute Global Aggregated Summaries
    print("\nComputing Aggregated Summaries...")
    summary: Dict[str, Dict[str, float]] = {}

    for s in schemes:
        all_metrics = scheme_metrics_collector[s]
        all_lats = scheme_latencies_collector[s]

        rec1 = float(np.mean([m["recall@1"] for m in all_metrics]))
        rec3 = float(np.mean([m["recall@3"] for m in all_metrics]))
        rec5 = float(np.mean([m["recall@5"] for m in all_metrics]))
        s_rec1 = float(np.mean([m["strict_recall@1"] for m in all_metrics]))
        s_rec3 = float(np.mean([m["strict_recall@3"] for m in all_metrics]))
        s_rec5 = float(np.mean([m["strict_recall@5"] for m in all_metrics]))
        mrr = float(np.mean([m["mrr"] for m in all_metrics]))
        ndcg = float(np.mean([m["ndcg@5"] for m in all_metrics]))

        lat_mean = float(np.mean(all_lats))
        lat_p50 = float(np.percentile(all_lats, 50))
        lat_p95 = float(np.percentile(all_lats, 95))

        summary[s] = {
            "recall@1": round(rec1, 4),
            "recall@3": round(rec3, 4),
            "recall@5": round(rec5, 4),
            "strict_recall@1": round(s_rec1, 4),
            "strict_recall@3": round(s_rec3, 4),
            "strict_recall@5": round(s_rec5, 4),
            "mrr": round(mrr, 4),
            "ndcg@5": round(ndcg, 4),
            "lat_mean_ms": round(lat_mean, 2),
            "lat_p50_ms": round(lat_p50, 2),
            "lat_p95_ms": round(lat_p95, 2),
        }

    # Stage latency breakdown summary
    stage_breakdown = {
        k: {
            "mean_ms": round(float(np.mean(v)), 2),
            "p50_ms": round(float(np.percentile(v, 50)), 2),
            "p95_ms": round(float(np.percentile(v, 95)), 2),
        }
        for k, v in stage_latencies_collector.items()
    }

    # Category summaries
    category_summaries: Dict[str, Dict[str, Dict[str, float]]] = {}
    for cat in categories:
        category_summaries[cat] = {}
        for s in schemes:
            m_list = cat_metrics_collector[cat][s]
            category_summaries[cat][s] = {
                "recall@1": round(float(np.mean([m["recall@1"] for m in m_list])), 4),
                "recall@3": round(float(np.mean([m["recall@3"] for m in m_list])), 4),
                "recall@5": round(float(np.mean([m["recall@5"] for m in m_list])), 4),
                "strict_recall@1": round(float(np.mean([m["strict_recall@1"] for m in m_list])), 4),
                "strict_recall@3": round(float(np.mean([m["strict_recall@3"] for m in m_list])), 4),
                "strict_recall@5": round(float(np.mean([m["strict_recall@5"] for m in m_list])), 4),
                "mrr": round(float(np.mean([m["mrr"] for m in m_list])), 4),
                "ndcg@5": round(float(np.mean([m["ndcg@5"] for m in m_list])), 4),
            }

    # 7. Print Console Summary Table
    print("\n" + "=" * 90)
    print(f"{'Scheme':<22} | {'Rec@1':<6} | {'Rec@3':<6} | {'Rec@5':<6} | {'MRR':<6} | {'nDCG@5':<6} | {'Mean(ms)':<8} | {'P95(ms)':<8}")
    print("-" * 90)
    scheme_display_names = {
        "dense_only": "1. Dense Only",
        "bm25_only": "2. BM25 Only",
        "hybrid_rrf": "3. Hybrid RRF",
        "hybrid_reranker": "4. Hybrid + Reranker",
    }
    for s in schemes:
        row = summary[s]
        print(
            f"{scheme_display_names[s]:<22} | "
            f"{row['recall@1']:<6.4f} | "
            f"{row['recall@3']:<6.4f} | "
            f"{row['recall@5']:<6.4f} | "
            f"{row['mrr']:<6.4f} | "
            f"{row['ndcg@5']:<6.4f} | "
            f"{row['lat_mean_ms']:<8.2f} | "
            f"{row['lat_p95_ms']:<8.2f}"
        )
    print("=" * 90)

    # 8. Export JSON & CSV Artifacts
    full_export = {
        "metadata": {
            "execution_date": "2026-11-04",
            "num_chunks": len(chunks),
            "num_queries": len(queries),
            "num_qrels": len(qrel_map),
            "measurement_rounds": rounds,
            "warmup_queries": 3,
            "embedding_model": embed_client.model_name,
            "reranker_model": reranker.client.model_name,
            "relevance_rules": {
                "score_2": "Core Evidence (高相关/核心答案切片)",
                "score_1": "Supporting Context (次相关/支撑上下文切片)",
                "score_0": "Confirmed Adversarial Negative (已确认硬负例/干扰项)",
                "unannotated": "Unannotated Candidate (未标注候选切片，收益按0计算，非已确认负例)",
            },
        },
        "summary": summary,
        "stage_latency_breakdown": stage_breakdown,
        "category_summaries": category_summaries,
        "queries": query_results,
    }

    # Save to both backend/benchmark/retrieval/results and data/benchmark
    export_targets = [results_dir, data_dir]
    for target_dir in export_targets:
        # JSON
        json_path = target_dir / "m6_2_results.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(full_export, f, ensure_ascii=False, indent=2)
        print(f"[Exported] {json_path}")

        # Summary CSV
        summary_csv_path = target_dir / "m6_2_summary.csv"
        with open(summary_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "category", "scheme", "recall@1", "recall@3", "recall@5",
                "strict_recall@1", "strict_recall@3", "strict_recall@5",
                "mrr", "ndcg@5", "lat_mean_ms", "lat_p50_ms", "lat_p95_ms"
            ])
            # Overall
            for s in schemes:
                r = summary[s]
                writer.writerow([
                    "OVERALL_ALL_60_QUERIES", s,
                    r["recall@1"], r["recall@3"], r["recall@5"],
                    r["strict_recall@1"], r["strict_recall@3"], r["strict_recall@5"],
                    r["mrr"], r["ndcg@5"],
                    r["lat_mean_ms"], r["lat_p50_ms"], r["lat_p95_ms"]
                ])
            # Category breakdown
            for cat in categories:
                for s in schemes:
                    cr = category_summaries[cat][s]
                    writer.writerow([
                        cat, s,
                        cr["recall@1"], cr["recall@3"], cr["recall@5"],
                        cr["strict_recall@1"], cr["strict_recall@3"], cr["strict_recall@5"],
                        cr["mrr"], cr["ndcg@5"],
                        "N/A", "N/A", "N/A"
                    ])
        print(f"[Exported] {summary_csv_path}")

        # Query Details CSV
        query_csv_path = target_dir / "m6_2_query_details.csv"
        with open(query_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "query_id", "category", "query", "scheme",
                "recall@1", "recall@3", "recall@5",
                "strict_recall@1", "strict_recall@3", "strict_recall@5",
                "mrr", "ndcg@5", "mean_lat_ms",
                "top1_chunk_id", "top1_rel", "top1_type",
                "top2_chunk_id", "top2_rel", "top2_type",
                "top3_chunk_id", "top3_rel", "top3_type",
                "top4_chunk_id", "top4_rel", "top4_type",
                "top5_chunk_id", "top5_rel", "top5_type",
            ])
            for q_res in query_results:
                qid = q_res["query_id"]
                cat = q_res["category"]
                qtext = q_res["query"]
                for s in schemes:
                    s_data = q_res["schemes"][s]
                    m = s_data["metrics"]
                    lat = s_data["latency_ms"]["mean"]
                    cands = s_data["top5_candidates"]
                    row = [
                        qid, cat, qtext, s,
                        m["recall@1"], m["recall@3"], m["recall@5"],
                        m["strict_recall@1"], m["strict_recall@3"], m["strict_recall@5"],
                        m["mrr"], m["ndcg@5"], lat
                    ]
                    for idx in range(5):
                        if idx < len(cands):
                            c_info = cands[idx]
                            row.extend([c_info["chunk_id"], str(c_info["ground_truth_relevance"]), c_info["annotation_type"]])
                        else:
                            row.extend(["NONE", "NONE", "NONE"])
                    writer.writerow(row)
        print(f"[Exported] {query_csv_path}")

        # SVG Charts
        svg_metrics_path = target_dir / "m6_2_metrics_comparison.svg"
        generate_svg_metrics_chart(summary, str(svg_metrics_path))
        print(f"[Exported] {svg_metrics_path}")

        svg_lat_path = target_dir / "m6_2_latency_comparison.svg"
        generate_svg_latency_chart(summary, str(svg_lat_path))
        print(f"[Exported] {svg_lat_path}")

    print("\n[SUCCESS] Stage 2-B benchmark runner completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
