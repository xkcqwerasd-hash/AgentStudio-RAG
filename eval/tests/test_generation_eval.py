import json
from pathlib import Path
import pytest
import sqlite3

from eval.core.judge_evaluator import JudgeEvaluationResult, JudgeEvaluator
from eval.runners.run_generation_eval import (
    RUN_BENCHMARK_DB,
    SRC_BENCHMARK_DB,
    GenerationBenchmarkRunner,
    SSEStreamParser,
    clone_eval_database,
)


class TestJudgeEvaluator:
    """Unit tests for independent LLM-as-a-Judge module."""

    def test_verify_programmatic_citations_valid(self):
        chunks = ["chunk_1", "chunk_2", "chunk_3"]
        answer = "根据系统规范，默认超时为 5000ms [Ref-1]，重试间隔为 200ms [Ref-2]。"
        is_valid, details = JudgeEvaluator.verify_programmatic_citations(
            answer_text=answer,
            kept_chunks=chunks,
            extracted_citations=[{"citation_index": 1}, {"citation_index": 2}],
        )
        assert is_valid is True
        assert details["total_citation_tags"] == 2
        assert details["valid_indices"] == [1, 2]
        assert details["invalid_indices"] == []

    def test_verify_programmatic_citations_invalid_index(self):
        chunks = ["chunk_1", "chunk_2"]
        # Ref-99 is out of bounds (only 2 chunks exist)
        answer = "根据不可靠声明 [Ref-99]，超时时间为 100ms。"
        is_valid, details = JudgeEvaluator.verify_programmatic_citations(
            answer_text=answer,
            kept_chunks=chunks,
            extracted_citations=[],
        )
        assert is_valid is False
        assert details["invalid_indices"] == [99]

    @pytest.mark.asyncio
    async def test_test_mode_mock_evaluation(self):
        judge = JudgeEvaluator(test_mode=True)
        res = await judge.evaluate_sample(
            sample_id="GEN-TEST-01",
            query="测试提问",
            reference_answer="测试标准答案",
            kept_chunks=["证据分块1", "证据分块2"],
            generated_answer="系统回答 [Ref-1]",
            extracted_citations=[{"citation_index": 1}],
            answerable=True,
        )
        assert res.status == "success"
        assert res.correctness_score == 5
        assert res.faithfulness_score == 5
        assert res.citation_accuracy_score == 5
        assert res.relevancy_score == 5
        assert res.overall_score == 5.0
        assert res.rejection_detected is False


class TestDatabaseIsolation:
    """Unit tests ensuring database isolation between benchmark and run databases."""

    def test_clone_eval_database(self, tmp_path):
        src_db = tmp_path / "src_test.db"
        dst_db = tmp_path / "dst_test.db"

        conn = sqlite3.connect(src_db)
        conn.execute("CREATE TABLE test_data (id INT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test_data VALUES (1, 'initial_record')")
        conn.commit()
        conn.close()

        clone_eval_database(src_db, dst_db)

        # Verify cloned data
        dst_conn = sqlite3.connect(dst_db)
        c = dst_conn.cursor()
        c.execute("SELECT count(*) FROM test_data")
        assert c.fetchone()[0] == 1

        # Write to destination database
        dst_conn.execute("INSERT INTO test_data VALUES (2, 'new_eval_record')")
        dst_conn.commit()
        dst_conn.close()

        # Verify source database was NOT modified
        src_conn = sqlite3.connect(src_db)
        c_src = src_conn.cursor()
        c_src.execute("SELECT count(*) FROM test_data")
        assert c_src.fetchone()[0] == 1  # Still 1, perfectly isolated!
        src_conn.close()


class TestSSEStreamParser:
    """Unit tests for parsing SSE lines."""

    def test_parse_events(self):
        raw_sse = (
            "event: start\ndata: {\"conversation_id\": \"c-123\"}\n\n"
            "event: token\ndata: {\"delta\": \"Hello\"}\n\n"
            "event: token\ndata: {\"delta\": \" World\"}\n\n"
            "event: trace\ndata: {\"ttft_ms\": 120.5}\n\n"
        )
        events = SSEStreamParser.parse_events(raw_sse)
        assert len(events) == 4
        assert events[0]["event"] == "start"
        assert events[0]["data"]["conversation_id"] == "c-123"
        assert events[1]["data"]["delta"] == "Hello"
        assert events[2]["data"]["delta"] == " World"
        assert events[3]["data"]["ttft_ms"] == 120.5


class TestBenchmarkDatasetIntegrity:
    """Unit tests for benchmark_generation_v1.jsonl format and content."""

    def test_dataset_format_and_status(self):
        dataset_file = Path(__file__).resolve().parent.parent / "datasets" / "benchmark_generation_v1.jsonl"
        assert dataset_file.exists(), f"Dataset file missing: {dataset_file}"

        with open(dataset_file, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]

        assert len(lines) == 30, f"Expected 30 samples, got {len(lines)}"

        categories = set()
        for idx, line in enumerate(lines, start=1):
            s = json.loads(line)
            assert "id" in s, f"Line {idx} missing 'id'"
            assert "query" in s, f"Line {idx} missing 'query'"
            assert "reference_answer" in s, f"Line {idx} missing 'reference_answer'"
            assert "answerable" in s, f"Line {idx} missing 'answerable'"
            assert s.get("status") == "verified", f"Sample {s['id']} status is {s.get('status')}, expected 'verified'"
            categories.add(s.get("category"))

        assert categories == {"single_fact", "multi_hop", "parametric", "unanswerable"}

    def test_dataset_v1_1_format_and_revisions(self):
        dataset_file = Path(__file__).resolve().parent.parent / "datasets" / "benchmark_generation_v1_1.jsonl"
        assert dataset_file.exists(), f"Dataset file missing: {dataset_file}"

        with open(dataset_file, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]

        assert len(lines) == 30, f"Expected 30 samples, got {len(lines)}"

        categories = set()
        items = {}
        for idx, line in enumerate(lines, start=1):
            s = json.loads(line)
            assert "id" in s, f"Line {idx} missing 'id'"
            assert "query" in s, f"Line {idx} missing 'query'"
            assert "reference_answer" in s, f"Line {idx} missing 'reference_answer'"
            assert "answerable" in s, f"Line {idx} missing 'answerable'"
            assert s.get("status") == "verified", f"Sample {s['id']} status is {s.get('status')}, expected 'verified'"
            categories.add(s.get("category"))
            items[s["id"]] = s

        assert categories == {"single_fact", "multi_hop", "parametric", "unanswerable"}

        # Verify GEN-17 revision
        gen17 = items["GEN-17"]
        assert "ModelHealthStore" in gen17["query"]
        assert "QPS" in gen17["reference_answer"]
        assert "180" in gen17["reference_answer"]

        # Verify GEN-22 revision
        gen22 = items["GEN-22"]
        assert "min_idle" in gen22["query"] or "最小空闲连接数" in gen22["query"]
        assert "10" in gen22["reference_answer"]
        assert "100" in gen22["reference_answer"]
        assert "3000" in gen22["reference_answer"]

        # Verify GEN-24 revision
        gen24 = items["GEN-24"]
        assert "2" in gen24["reference_answer"]
        assert "500" in gen24["reference_answer"]
        assert "POST" in gen24["reference_answer"]


class TestTransportAndCostAccounting:
    """Unit tests for port discovery and cost calculation."""

    def test_find_free_port(self):
        from eval.runners.run_generation_eval import find_free_port
        port = find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_calculate_deepseek_cost(self):
        from eval.runners.run_generation_eval import calculate_deepseek_cost
        cost = calculate_deepseek_cost(
            rag_input_tokens=1000,
            rag_output_tokens=500,
            judge_prompt_tokens=2000,
            judge_completion_tokens=400,
            judge_cache_hit_tokens=1500,
            judge_cache_miss_tokens=500,
        )
        assert "pricing_model" in cost
        assert cost["tokens"]["total_tokens"] == 3900
        assert cost["cost_rmb"]["off_peak_total"] > 0
        assert cost["cost_rmb"]["peak_total"] == round(cost["cost_rmb"]["off_peak_total"] * 2.0, 6)
