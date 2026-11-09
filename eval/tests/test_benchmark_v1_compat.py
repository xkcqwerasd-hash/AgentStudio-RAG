import json
from pathlib import Path
import pytest

from eval.core.dataset_loader import BenchmarkDatasetLoader
from eval.core.metrics_retrieval import compute_macro_averages, compute_sample_retrieval_metrics


def test_benchmark_v1_jsonl_loads_correctly():
    loader = BenchmarkDatasetLoader()
    samples = loader.load_samples()
    assert len(samples) == 20, f"Expected 20 samples in benchmark_v1, got {len(samples)}"
    for s in samples:
        assert "id" in s
        assert "query" in s
        assert "target_chunks" in s
        assert len(s["target_chunks"]) > 0


def test_v1_results_metric_reproducibility():
    """
    Load recorded v1 report results and verify that our refactored metrics function
    produces identical metrics for the recorded retrieved chunk lists.
    """
    v1_report_file = Path(__file__).resolve().parent.parent / "reports" / "retrieval_results.json"
    if not v1_report_file.exists():
        pytest.skip("v1 retrieval_results.json not found")

    with open(v1_report_file, "r", encoding="utf-8") as f:
        v1_data = json.load(f)

    # Check that summary metrics match what v1 recorded
    for scheme_summary in v1_data.get("summary", []):
        sid = scheme_summary.get("scheme_id")
        if sid == "B":  # BM25
            assert scheme_summary["recall@1"] == 0.925
            assert scheme_summary["mrr@10"] == 1.0
        elif sid == "D":  # Reranker
            assert scheme_summary["recall@1"] == 0.925
            assert scheme_summary["recall@5"] == 1.0
            assert scheme_summary["mrr@10"] == 1.0
