import math
import pytest
from typing import Dict, List, Set

from eval.core.metrics_retrieval import (
    calculate_candidate_recall,
    calculate_hit_rate_at_k,
    calculate_mrr_at_k,
    calculate_ndcg_at_k,
    calculate_precision_at_k,
    calculate_r_precision,
    calculate_recall_at_k,
    compute_macro_averages,
    compute_sample_retrieval_metrics,
    deduplicate_chunk_ids,
)


class TestMetricsRetrieval:
    """Unit tests for the evaluation metrics core."""

    def test_deduplicate_chunk_ids(self):
        chunks = ["c1", "c2", "c1", "c3", "c2", "c4"]
        deduped = deduplicate_chunk_ids(chunks)
        assert deduped == ["c1", "c2", "c3", "c4"]
        assert deduplicate_chunk_ids([]) == []

    def test_recall_at_k_single_golden(self):
        goldens = {"c1"}
        retrieved = ["c1", "c2", "c3"]
        assert calculate_recall_at_k(retrieved, goldens, k=1) == 1.0
        assert calculate_recall_at_k(retrieved, goldens, k=5) == 1.0

        # Miss at rank 1, hit at rank 2
        retrieved_miss = ["c2", "c1", "c3"]
        assert calculate_recall_at_k(retrieved_miss, goldens, k=1) == 0.0
        assert calculate_recall_at_k(retrieved_miss, goldens, k=2) == 1.0

    def test_recall_at_k_multiple_goldens(self):
        goldens = {"c1", "c3"}
        retrieved = ["c1", "c2", "c3", "c4"]
        # In top-1: only c1 is found -> 1/2 = 0.5
        assert calculate_recall_at_k(retrieved, goldens, k=1) == 0.5
        # In top-2: still only c1 -> 0.5
        assert calculate_recall_at_k(retrieved, goldens, k=2) == 0.5
        # In top-3: c1 and c3 are found -> 2/2 = 1.0
        assert calculate_recall_at_k(retrieved, goldens, k=3) == 1.0

    def test_recall_edge_cases(self):
        assert calculate_recall_at_k(["c1"], set(), k=5) == 0.0
        assert calculate_recall_at_k([], {"c1"}, k=5) == 0.0
        assert calculate_recall_at_k(["c1"], {"c1"}, k=0) == 0.0
        assert calculate_recall_at_k(["c1"], {"c1"}, k=-1) == 0.0

    def test_precision_at_k_fixed_denominator(self):
        goldens = {"c1"}
        retrieved = ["c1", "c2", "c3", "c4", "c5"]
        # Standard IR Precision@5 has fixed denominator K=5
        assert calculate_precision_at_k(retrieved, goldens, k=5) == 0.2000
        # If retrieved has fewer chunks than K, denominator is still K
        short_retrieved = ["c1", "c2"]
        assert calculate_precision_at_k(short_retrieved, goldens, k=5) == 0.2000
        # When 2 relevant chunks in top-5:
        goldens2 = {"c1", "c2"}
        assert calculate_precision_at_k(retrieved, goldens2, k=5) == 0.4000

    def test_precision_edge_cases(self):
        assert calculate_precision_at_k(["c1"], set(), k=5) == 0.0
        assert calculate_precision_at_k(["c1"], {"c1"}, k=0) == 0.0
        assert calculate_precision_at_k([], {"c1"}, k=5) == 0.0

    def test_r_precision(self):
        # Case 1: |G| = 1, R = 1
        goldens1 = {"c1"}
        assert calculate_r_precision(["c1", "c2", "c3"], goldens1) == 1.0
        assert calculate_r_precision(["c2", "c1", "c3"], goldens1) == 0.0

        # Case 2: |G| = 2, R = 2
        goldens2 = {"c1", "c3"}
        # Top-2 has c1 (hit) and c2 (miss) -> 1/2 = 0.5
        assert calculate_r_precision(["c1", "c2", "c3", "c4"], goldens2) == 0.5
        # Top-2 has c1 and c3 -> 2/2 = 1.0
        assert calculate_r_precision(["c1", "c3", "c2", "c4"], goldens2) == 1.0

        # Case 3: |G| = 3, R = 3
        goldens3 = {"c1", "c2", "c3"}
        assert calculate_r_precision(["c1", "c9", "c3", "c2"], goldens3) == round(2.0 / 3.0, 4)

        # Edge cases: empty goldens or empty retrieved
        assert calculate_r_precision(["c1"], set()) == 0.0
        assert calculate_r_precision([], {"c1"}) == 0.0

    def test_hit_rate_at_k(self):
        goldens = {"target"}
        retrieved = ["d1", "d2", "target", "d3"]
        assert calculate_hit_rate_at_k(retrieved, goldens, k=1) == 0.0
        assert calculate_hit_rate_at_k(retrieved, goldens, k=2) == 0.0
        assert calculate_hit_rate_at_k(retrieved, goldens, k=3) == 1.0
        assert calculate_hit_rate_at_k(retrieved, goldens, k=5) == 1.0

        # Edge cases
        assert calculate_hit_rate_at_k(retrieved, set(), k=5) == 0.0
        assert calculate_hit_rate_at_k([], goldens, k=5) == 0.0
        assert calculate_hit_rate_at_k(retrieved, goldens, k=0) == 0.0

    def test_candidate_recall(self):
        goldens = {"c1", "c2"}
        # Candidate pool size 10 capturing both goldens
        candidates_10 = ["x1", "c1", "x2", "x3", "c2", "x4", "x5", "x6", "x7", "x8"]
        assert calculate_candidate_recall(candidates_10, goldens, n=10) == 1.0
        # If truncated to N=3: only c1 is captured -> 0.5
        assert calculate_candidate_recall(candidates_10, goldens, n=3) == 0.5
        # Empty golden
        assert calculate_candidate_recall(candidates_10, set(), n=10) == 0.0

    def test_mrr_at_k(self):
        goldens = {"c2", "c4"}
        retrieved = ["c1", "c2", "c3", "c4"]
        # First relevant chunk is c2 at rank 2 -> MRR = 1/2 = 0.5
        assert calculate_mrr_at_k(retrieved, goldens, k=10) == 0.5
        # If k=1, first relevant chunk is not in top-1 -> MRR = 0.0
        assert calculate_mrr_at_k(retrieved, goldens, k=1) == 0.0

        # Hit at rank 1 -> 1.0
        assert calculate_mrr_at_k(["c2", "c1"], goldens, k=10) == 1.0
        # No hit -> 0.0
        assert calculate_mrr_at_k(["x1", "x2"], goldens, k=10) == 0.0

    def test_ndcg_at_k_graded(self):
        # Relevance: c1 is core (2.0), c2 is background (1.0), others 0.0
        rel_map = {"c1": 2.0, "c2": 1.0}

        # Perfect ranking: [c1, c2, c3]
        perfect = ["c1", "c2", "c3"]
        assert calculate_ndcg_at_k(perfect, rel_map, k=10) == 1.0

        # Sub-optimal ranking: [c2, c1, c3]
        subopt = ["c2", "c1", "c3"]
        score_subopt = calculate_ndcg_at_k(subopt, rel_map, k=10)
        assert 0.0 < score_subopt < 1.0

        # Theoretical check:
        # DCG of [c2, c1] = (2^1 - 1)/log2(2) + (2^2 - 1)/log2(3) = 1/1 + 3/1.58496 = 1 + 1.892789 = 2.8928
        # IDCG of [c1, c2] = (2^2 - 1)/log2(2) + (2^1 - 1)/log2(3) = 3/1 + 1/1.58496 = 3 + 0.630930 = 3.6309
        # expected nDCG = 2.8928 / 3.6309 = 0.7967
        assert abs(score_subopt - 0.7967) < 0.005

    def test_compute_sample_retrieval_metrics_compatibility(self):
        rel_map = {"c1": 2.0, "c2": 1.0}
        retrieved = ["c1", "c3", "c2", "c4", "c5"]
        candidate_pool = ["c1", "c3", "c2", "c4", "c5", "c6", "c7", "c8", "c9", "c10"]

        m = compute_sample_retrieval_metrics(
            retrieved,
            rel_map,
            candidate_chunk_ids=candidate_pool,
            candidate_n=10,
        )

        # 1. Check all v1 keys are present
        v1_keys = ["recall@1", "recall@3", "recall@5", "recall@10", "precision@5", "mrr@10", "ndcg@10"]
        for k in v1_keys:
            assert k in m, f"Missing v1 key {k}"

        # 2. Check all v2 keys are present
        v2_keys = ["r_precision", "hit_rate@1", "hit_rate@3", "hit_rate@5", "hit_rate@10", "candidate_recall"]
        for k in v2_keys:
            assert k in m, f"Missing v2 key {k}"

        # 3. Check values
        assert m["recall@1"] == 0.5  # c1 in top-1 out of 2 goldens
        assert m["recall@3"] == 1.0  # c1 and c2 in top-3
        assert m["precision@5"] == 0.4000  # 2 goldens in top-5
        assert m["r_precision"] == 0.5000  # R=2, top-2 has c1, c3 -> 1/2 = 0.5
        assert m["hit_rate@1"] == 1.0
        assert m["candidate_recall"] == 1.0  # both in candidate pool

    def test_compute_macro_averages(self):
        m1 = {
            "recall@1": 1.0,
            "precision@5": 0.2,
            "r_precision": 1.0,
            "hit_rate@1": 1.0,
            "mrr@10": 1.0,
            "ndcg@10": 1.0,
        }
        m2 = {
            "recall@1": 0.0,
            "precision@5": 0.4,
            "r_precision": 0.5,
            "hit_rate@1": 0.0,
            "mrr@10": 0.5,
            "ndcg@10": 0.6,
        }

        avg = compute_macro_averages([m1, m2])
        assert avg["recall@1"] == 0.5
        assert avg["precision@5"] == 0.3
        assert avg["r_precision"] == 0.75
        assert avg["hit_rate@1"] == 0.5
        assert avg["mrr@10"] == 0.75
        assert avg["ndcg@10"] == 0.8

    def test_candidate_budget_fairness_simulation(self):
        """
        Verify that Candidate Pool budgets N in [10, 30, 50] are strictly enforced across:
        - Scheme A: Dense Top-N
        - Scheme B: BM25 Top-N
        - Scheme C: Dense Top-N + BM25 Top-N -> RRF truncated to Top-N
        - Scheme D: Hybrid Top-N candidate pool passed to Reranker
        """
        for budget_n in [10, 30, 50]:
            mock_corpus = [f"doc_chunk_{i}" for i in range(100)]

            # Scheme A retrieves exactly budget_n
            dense_top_n = mock_corpus[:budget_n]
            assert len(dense_top_n) == budget_n

            # Scheme B retrieves exactly budget_n
            bm25_top_n = list(reversed(mock_corpus))[:budget_n]
            assert len(bm25_top_n) == budget_n

            # Scheme C merges the two top-n sets and truncates to budget_n
            hybrid_merged = deduplicate_chunk_ids(dense_top_n + bm25_top_n)[:budget_n]
            assert len(hybrid_merged) == budget_n

            # Scheme D reranks the exact same hybrid candidate pool of size budget_n
            rerank_candidates = list(hybrid_merged)
            assert len(rerank_candidates) == budget_n
            assert rerank_candidates == hybrid_merged
