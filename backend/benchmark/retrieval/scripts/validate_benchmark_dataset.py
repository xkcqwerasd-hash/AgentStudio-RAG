#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data Quality Validation Script for AgentStudio-RAG Benchmark Dataset (M6.2 Stage 2-A).
Strictly validates:
1. Corpus chunk integrity, uniqueness, and embedding presence.
2. Query distribution, uniqueness, and category balance (10 queries x 6 categories = 60).
3. Ground Truth QREL consistency: chunk existence, 3-level score adherence, duplicate checks.
4. Positive evidence completeness for all 60 queries.
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding="utf-8")

def validate_benchmark_dataset():
    root = Path(__file__).resolve().parent.parent
    corpus_file = root / "corpus" / "corpus_chunks.jsonl"
    queries_file = root / "queries" / "queries.jsonl"
    qrels_file = root / "qrels" / "qrels.jsonl"
    metadata_file = root / "metadata" / "dataset_metadata.json"

    print("=" * 80)
    print(" AgentStudio-RAG — M6.2 Stage 2-A: Benchmark Dataset Quality Validation")
    print("=" * 80)
    print(f"[*] Benchmark Root Directory: {root}")

    errors = []
    warnings = []

    # -------------------------------------------------------------------------
    # 1. Validate Corpus
    # -------------------------------------------------------------------------
    print("\n[1/4] Validating Corpus Chunks...")
    if not corpus_file.exists():
        errors.append(f"Corpus file missing: {corpus_file}")
        return False

    chunks = []
    chunk_ids = set()
    chunk_contents = set()
    with open(corpus_file, "r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except Exception as e:
                errors.append(f"Corpus line {line_idx} invalid JSON: {e}")
                continue

            cid = c.get("chunk_id")
            if not cid:
                errors.append(f"Corpus line {line_idx} missing 'chunk_id'")
                continue
            if cid in chunk_ids:
                errors.append(f"Duplicate chunk_id '{cid}' at line {line_idx}")
            chunk_ids.add(cid)

            content = c.get("content", "").strip()
            if not content:
                errors.append(f"Chunk '{cid}' has empty content")
            if content in chunk_contents:
                warnings.append(f"Chunk '{cid}' has duplicate text content with another chunk")
            chunk_contents.add(content)

            # Check embedding presence
            emb = c.get("embedding")
            if not emb or not isinstance(emb, list) or len(emb) != 512:
                errors.append(f"Chunk '{cid}' missing valid 512-d embedding vector")

            chunks.append(c)

    print(f"  -> Total Chunks verified: {len(chunks)}")
    if len(chunks) < 100 or len(chunks) > 300:
        errors.append(f"Total chunks count {len(chunks)} outside expected range [100, 300]")
    else:
        print(f"  -> Chunks count {len(chunks)} is within target range [100, 300].")

    # -------------------------------------------------------------------------
    # 2. Validate Queries
    # -------------------------------------------------------------------------
    print("\n[2/4] Validating Queries...")
    if not queries_file.exists():
        errors.append(f"Queries file missing: {queries_file}")
        return False

    queries = []
    query_ids = set()
    query_texts = set()
    category_counts = Counter()

    EXPECTED_CATEGORIES = {
        "CAT-1_技术概念解释": 10,
        "CAT-2_精确参数查询": 10,
        "CAT-3_错误码与术语查询": 10,
        "CAT-4_跨文档关联问题": 10,
        "CAT-5_同义表达与语义检索": 10,
        "CAT-6_中英文混合技术问题": 10,
    }

    with open(queries_file, "r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                q = json.loads(line)
            except Exception as e:
                errors.append(f"Queries line {line_idx} invalid JSON: {e}")
                continue

            qid = q.get("query_id")
            if not qid:
                errors.append(f"Queries line {line_idx} missing 'query_id'")
                continue
            if qid in query_ids:
                errors.append(f"Duplicate query_id '{qid}' at line {line_idx}")
            query_ids.add(qid)

            text = q.get("query", "").strip()
            if not text:
                errors.append(f"Query '{qid}' has empty query text")
            if text in query_texts:
                errors.append(f"Duplicate query text found in '{qid}'")
            query_texts.add(text)

            cat = q.get("category", "")
            if cat not in EXPECTED_CATEGORIES:
                errors.append(f"Query '{qid}' has unrecognized category '{cat}'")
            category_counts[cat] += 1
            queries.append(q)

    print(f"  -> Total Queries verified: {len(queries)}")
    if len(queries) != 60:
        errors.append(f"Total queries count {len(queries)} != 60")

    for cat, exp_cnt in EXPECTED_CATEGORIES.items():
        actual_cnt = category_counts[cat]
        print(f"  -> Category '{cat}': {actual_cnt} queries (expected: {exp_cnt})")
        if actual_cnt != exp_cnt:
            errors.append(f"Category '{cat}' count mismatch: actual {actual_cnt} != expected {exp_cnt}")

    # -------------------------------------------------------------------------
    # 3. Validate QREL Ground Truth Annotations
    # -------------------------------------------------------------------------
    print("\n[3/4] Validating Ground Truth (QRELs)...")
    if not qrels_file.exists():
        errors.append(f"QRELs file missing: {qrels_file}")
        return False

    qrels = []
    seen_qrel_pairs = set()
    qrels_by_query = {qid: [] for qid in query_ids}

    relevance_counts = Counter()

    with open(qrels_file, "r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception as e:
                errors.append(f"QREL line {line_idx} invalid JSON: {e}")
                continue

            qid = r.get("query_id")
            cid = r.get("chunk_id")
            rel = r.get("relevance")

            if qid not in query_ids:
                errors.append(f"QREL line {line_idx}: query_id '{qid}' not found in queries.jsonl")
            if cid not in chunk_ids:
                errors.append(f"QREL line {line_idx}: chunk_id '{cid}' not found in corpus_chunks.jsonl")

            if rel not in (0, 1, 2):
                errors.append(f"QREL line {line_idx}: invalid relevance score '{rel}' (must be 0, 1, or 2)")
            relevance_counts[rel] += 1

            pair = (qid, cid)
            if pair in seen_qrel_pairs:
                errors.append(f"Duplicate (query_id, chunk_id) pair '{pair}' at line {line_idx}")
            seen_qrel_pairs.add(pair)

            if qid in qrels_by_query:
                qrels_by_query[qid].append(r)
            qrels.append(r)

    print(f"  -> Total QREL Judgements verified: {len(qrels)}")
    print(f"  -> Score 2 (Core Evidence): {relevance_counts[2]}")
    print(f"  -> Score 1 (Supporting Context): {relevance_counts[1]}")
    print(f"  -> Score 0 (Hard Negative Distractor): {relevance_counts[0]}")

    # -------------------------------------------------------------------------
    # 4. Cross-Verification of Query Evidence Completeness
    # -------------------------------------------------------------------------
    print("\n[4/4] Cross-Verifying Query Evidence Completeness...")
    queries_missing_positives = []
    queries_missing_core = []
    queries_without_negatives = []

    for qid in sorted(query_ids):
        judgements = qrels_by_query.get(qid, [])
        positives = [j for j in judgements if j.get("relevance", 0) >= 1]
        cores = [j for j in judgements if j.get("relevance", 0) == 2]
        negatives = [j for j in judgements if j.get("relevance", 0) == 0]

        if not positives:
            queries_missing_positives.append(qid)
        if not cores:
            queries_missing_core.append(qid)
        if not negatives:
            queries_without_negatives.append(qid)

    if queries_missing_positives:
        errors.append(f"Queries with ZERO positive ground truth: {queries_missing_positives}")
    else:
        print("  -> 100% of queries have at least one valid positive chunk (rel >= 1).")

    if queries_missing_core:
        errors.append(f"Queries missing score=2 core answer chunk: {queries_missing_core}")
    else:
        print("  -> 100% of queries have at least one score=2 core answer chunk.")

    print(f"  -> Queries with explicit hard negative distractors: {len(query_ids) - len(queries_without_negatives)}/{len(query_ids)}")

    # -------------------------------------------------------------------------
    # Validation Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print(" Validation Results Summary")
    print("=" * 80)
    print(f"Total Errors: {len(errors)}")
    print(f"Total Warnings: {len(warnings)}")

    if warnings:
        print("\nWarnings:")
        for w in warnings[:5]:
            print(f"  [WARN] {w}")

    if errors:
        print("\nErrors Found:")
        for e in errors:
            print(f"  [FAIL] {e}")
        print("\n[!] DATASET VALIDATION FAILED! Please fix errors before Stage 2-B.")
        return False

    print("\n[SUCCESS] ALL DATASET QUALITY CHECKS PASSED!")
    print(f"Corpus: {len(chunks)} chunks | Queries: {len(queries)} | QRELs: {len(qrels)}")
    print("The benchmark dataset is 100% ready for Stage 2-B ablation benchmarking.")
    return True

if __name__ == "__main__":
    success = validate_benchmark_dataset()
    sys.exit(0 if success else 1)
