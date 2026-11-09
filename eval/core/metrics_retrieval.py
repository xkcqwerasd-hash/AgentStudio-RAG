import math
from typing import Any, Dict, List, Optional, Set, Tuple


def deduplicate_chunk_ids(chunk_ids: List[str]) -> List[str]:
    """Preserve order while removing duplicate chunk IDs."""
    seen: Set[str] = set()
    deduped: List[str] = []
    for cid in chunk_ids:
        if cid not in seen:
            seen.add(cid)
            deduped.append(cid)
    return deduped


def calculate_recall_at_k(
    retrieved_chunk_ids: List[str],
    golden_chunk_ids: Set[str],
    k: int,
) -> float:
    """
    Recall@K = (Number of relevant chunks retrieved in top-K) / (Total relevant golden chunks).
    Returns 0.0 if golden_chunk_ids is empty.
    """
    if not golden_chunk_ids or k <= 0:
        return 0.0

    deduped = deduplicate_chunk_ids(retrieved_chunk_ids)[:k]
    hits = sum(1 for cid in deduped if cid in golden_chunk_ids)
    return round(hits / float(len(golden_chunk_ids)), 4)


def calculate_precision_at_k(
    retrieved_chunk_ids: List[str],
    golden_chunk_ids: Set[str],
    k: int,
) -> float:
    """
    Precision@K = (Number of relevant chunks in top-K) / K.
    Preserves original standard Information Retrieval definition with fixed denominator K.
    """
    if k <= 0 or not golden_chunk_ids:
        return 0.0

    deduped = deduplicate_chunk_ids(retrieved_chunk_ids)[:k]
    hits = sum(1 for cid in deduped if cid in golden_chunk_ids)
    return round(hits / float(k), 4)


def calculate_r_precision(
    retrieved_chunk_ids: List[str],
    golden_chunk_ids: Set[str],
) -> float:
    """
    R-Precision: Precision evaluated at rank R, where R is the number of relevant golden chunks (|G|).
    Solves the fixed-denominator issue of Precision@K when |G| < K.
    Returns 0.0 if golden_chunk_ids is empty.
    """
    r = len(golden_chunk_ids)
    if r <= 0 or not retrieved_chunk_ids:
        return 0.0

    deduped = deduplicate_chunk_ids(retrieved_chunk_ids)[:r]
    hits = sum(1 for cid in deduped if cid in golden_chunk_ids)
    return round(hits / float(r), 4)


def calculate_hit_rate_at_k(
    retrieved_chunk_ids: List[str],
    golden_chunk_ids: Set[str],
    k: int,
) -> float:
    """
    HitRate@K: Binary indicator (1.0 or 0.0) indicating whether AT LEAST ONE
    relevant chunk is present in the top-K retrieved list.
    Returns 0.0 if golden_chunk_ids is empty or k <= 0.
    """
    if not golden_chunk_ids or k <= 0 or not retrieved_chunk_ids:
        return 0.0

    deduped = deduplicate_chunk_ids(retrieved_chunk_ids)[:k]
    for cid in deduped:
        if cid in golden_chunk_ids:
            return 1.0
    return 0.0


def calculate_candidate_recall(
    candidate_chunk_ids: List[str],
    golden_chunk_ids: Set[str],
    n: Optional[int] = None,
) -> float:
    """
    Candidate Recall@N: Recall of golden chunks captured in the candidate pool
    before the reranking stage, verifying first-stage retrieval recall.
    """
    pool_k = n if (n is not None and n > 0) else len(candidate_chunk_ids)
    return calculate_recall_at_k(candidate_chunk_ids, golden_chunk_ids, k=pool_k)


def calculate_mrr_at_k(
    retrieved_chunk_ids: List[str],
    golden_chunk_ids: Set[str],
    k: int = 10,
) -> float:
    """
    MRR@K = 1 / rank of the first relevant chunk retrieved in top-K.
    Returns 0.0 if no relevant chunk appears in top-K or golden_chunk_ids is empty.
    """
    if not golden_chunk_ids or k <= 0:
        return 0.0

    deduped = deduplicate_chunk_ids(retrieved_chunk_ids)[:k]
    for rank, cid in enumerate(deduped, start=1):
        if cid in golden_chunk_ids:
            return round(1.0 / float(rank), 4)
    return 0.0


def calculate_ndcg_at_k(
    retrieved_chunk_ids: List[str],
    relevance_map: Dict[str, float],
    k: int = 10,
) -> float:
    """
    Normalized Discounted Cumulative Gain at rank K with graded relevance:
    DCG@K = sum_{i=1}^K (2^{rel_i} - 1) / log2(i + 1)
    IDCG@K = sum_{i=1}^{min(K, |golden|)} (2^{ideal_rel_i} - 1) / log2(i + 1)
    nDCG@K = DCG@K / IDCG@K
    """
    if not relevance_map or k <= 0:
        return 0.0

    deduped = deduplicate_chunk_ids(retrieved_chunk_ids)[:k]

    # Calculate DCG@K
    dcg = 0.0
    for idx, cid in enumerate(deduped, start=1):
        rel = float(relevance_map.get(cid, 0.0))
        if rel > 0.0:
            dcg += (math.pow(2.0, rel) - 1.0) / math.log2(idx + 1.0)

    # Calculate IDCG@K from sorted ideal relevance scores
    ideal_scores = sorted(
        [float(r) for r in relevance_map.values() if r > 0.0],
        reverse=True,
    )[:k]

    idcg = 0.0
    for idx, rel in enumerate(ideal_scores, start=1):
        idcg += (math.pow(2.0, rel) - 1.0) / math.log2(idx + 1.0)

    if idcg <= 1e-9:
        return 0.0

    return round(dcg / idcg, 4)


def compute_sample_retrieval_metrics(
    retrieved_chunk_ids: List[str],
    relevance_map: Dict[str, float],
    candidate_chunk_ids: Optional[List[str]] = None,
    candidate_n: Optional[int] = None,
) -> Dict[str, float]:
    """
    Compute comprehensive retrieval metrics for a single query sample:
    - Retains legacy v1 metrics: Recall@1, Recall@3, Recall@5, Recall@10, Precision@5, MRR@10, nDCG@10.
    - Adds v2 metrics: R-Precision, HitRate@1, HitRate@3, HitRate@5, HitRate@10, Candidate Recall@N.
    """
    golden_ids = {cid for cid, score in relevance_map.items() if score > 0.0}

    r1 = calculate_recall_at_k(retrieved_chunk_ids, golden_ids, k=1)
    r3 = calculate_recall_at_k(retrieved_chunk_ids, golden_ids, k=3)
    r5 = calculate_recall_at_k(retrieved_chunk_ids, golden_ids, k=5)
    r10 = calculate_recall_at_k(retrieved_chunk_ids, golden_ids, k=10)
    p5 = calculate_precision_at_k(retrieved_chunk_ids, golden_ids, k=5)
    r_prec = calculate_r_precision(retrieved_chunk_ids, golden_ids)

    hr1 = calculate_hit_rate_at_k(retrieved_chunk_ids, golden_ids, k=1)
    hr3 = calculate_hit_rate_at_k(retrieved_chunk_ids, golden_ids, k=3)
    hr5 = calculate_hit_rate_at_k(retrieved_chunk_ids, golden_ids, k=5)
    hr10 = calculate_hit_rate_at_k(retrieved_chunk_ids, golden_ids, k=10)

    mrr10 = calculate_mrr_at_k(retrieved_chunk_ids, golden_ids, k=10)
    ndcg10 = calculate_ndcg_at_k(retrieved_chunk_ids, relevance_map, k=10)

    metrics = {
        "recall@1": r1,
        "recall@3": r3,
        "recall@5": r5,
        "recall@10": r10,
        "precision@5": p5,
        "r_precision": r_prec,
        "hit_rate@1": hr1,
        "hit_rate@3": hr3,
        "hit_rate@5": hr5,
        "hit_rate@10": hr10,
        "mrr@10": mrr10,
        "ndcg@10": ndcg10,
    }

    if candidate_chunk_ids is not None:
        cand_recall = calculate_candidate_recall(candidate_chunk_ids, golden_ids, n=candidate_n)
        metrics["candidate_recall"] = cand_recall

    return metrics


def compute_macro_averages(
    sample_metrics: List[Dict[str, float]],
    keys: Optional[List[str]] = None,
) -> Dict[str, float]:
    """Compute Macro Average across all query sample metrics."""
    if not sample_metrics:
        default_keys = [
            "recall@1", "recall@3", "recall@5", "recall@10",
            "precision@5", "r_precision",
            "hit_rate@1", "hit_rate@3", "hit_rate@5", "hit_rate@10",
            "mrr@10", "ndcg@10",
        ]
        return {k: 0.0 for k in default_keys}

    if keys is None:
        # Collect all metric keys present across all samples preserving standard ordering
        ordered_standard = [
            "recall@1", "recall@3", "recall@5", "recall@10",
            "precision@5", "r_precision",
            "hit_rate@1", "hit_rate@3", "hit_rate@5", "hit_rate@10",
            "mrr@10", "ndcg@10", "candidate_recall",
        ]
        present_keys = set()
        for m in sample_metrics:
            present_keys.update(m.keys())
        keys = [k for k in ordered_standard if k in present_keys]
        # Append any other non-standard keys
        for k in sorted(present_keys):
            if k not in keys:
                keys.append(k)

    n = float(len(sample_metrics))
    averages: Dict[str, float] = {}
    for k in keys:
        vals = [m.get(k, 0.0) for m in sample_metrics]
        averages[k] = round(sum(vals) / n, 4)
    return averages
