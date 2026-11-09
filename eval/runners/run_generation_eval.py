import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
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

import httpx
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db
from app.core.config import settings
from app.main import app as fastapi_app
from app.rag.prompt_builder import PromptBuilder

from eval.core.judge_evaluator import JudgeEvaluationResult, JudgeEvaluator


SRC_BENCHMARK_DB = BACKEND_DIR / "data" / "storage" / "eval_benchmark.db"
RUN_BENCHMARK_DB = BACKEND_DIR / "data" / "storage" / "generation_eval_run.db"
PROD_DB = BACKEND_DIR / "data" / "storage" / "agentstudio_rag.db"
EVAL_KB_ID = "eval-kb-benchmark-01"


def calculate_file_sha256(path: Path) -> str:
    """Calculate SHA256 checksum of a file for cryptographic isolation verification."""
    import hashlib
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def setup_eval_model_config(dst_path: Path):
    """
    Ensure deepseek-flash is configured as the active default model in generation_eval_run.db.
    This guarantees RAG workflow uses deepseek-flash without altering production settings.
    """
    from app.core.security import encrypt_api_key
    api_key = os.environ.get("OPENAI_API_KEY") or settings.OPENAI_API_KEY
    base_url = (os.environ.get("OPENAI_BASE_URL") or settings.OPENAI_BASE_URL or "https://api.deepseek.com").rstrip("/")
    encrypted_key = encrypt_api_key(api_key) if api_key else None

    conn = sqlite3.connect(str(dst_path))
    c = conn.cursor()
    tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "model_configs" not in tables or "model_providers" not in tables:
        conn.close()
        return

    c.execute("DELETE FROM model_configs")
    c.execute("DELETE FROM model_providers")

    provider_id = "eval-provider-deepseek"
    model_id = "eval-model-deepseek-flash"

    c.execute(
        "INSERT INTO model_providers (id, name, provider_type, base_url, api_key_ciphertext, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (provider_id, "DeepSeek 官方", "deepseek", base_url, encrypted_key, 1)
    )
    caps = json.dumps({
        "supports_streaming": True,
        "supports_tool_calling": True,
        "supports_vision": True,
        "context_window": 1048576,
    }, ensure_ascii=False)
    c.execute(
        "INSERT INTO model_configs (id, provider_id, model_name, display_name, model_type, capabilities_json, is_default, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (model_id, provider_id, "deepseek-flash", "DeepSeek-V4.1-Flash", "chat", caps, 1, 1)
    )
    conn.commit()
    conn.close()


def calculate_deepseek_cost(
    rag_input_tokens: int,
    rag_output_tokens: int,
    judge_prompt_tokens: int,
    judge_completion_tokens: int,
    judge_cache_hit_tokens: int = 0,
    judge_cache_miss_tokens: int = 0,
) -> Dict[str, Any]:
    """
    Calculate DeepSeek API cost for deepseek-flash (DeepSeek-V4.1-Flash-1017).
    Pricing:
      Off-Peak (空闲): Cache Hit 0.02 / 1M, Cache Miss 1.00 / 1M, Output 4.00 / 1M (RMB)
      Peak (高峰):     Cache Hit 0.04 / 1M, Cache Miss 2.00 / 1M, Output 8.00 / 1M (RMB)
    """
    rag_in_cost_offpeak = (rag_input_tokens / 1_000_000) * 1.00
    rag_out_cost_offpeak = (rag_output_tokens / 1_000_000) * 4.00

    judge_hit_cost_offpeak = (judge_cache_hit_tokens / 1_000_000) * 0.02
    judge_miss = judge_cache_miss_tokens if judge_cache_miss_tokens > 0 else max(0, judge_prompt_tokens - judge_cache_hit_tokens)
    judge_miss_cost_offpeak = (judge_miss / 1_000_000) * 1.00
    judge_out_cost_offpeak = (judge_completion_tokens / 1_000_000) * 4.00

    total_offpeak = rag_in_cost_offpeak + rag_out_cost_offpeak + judge_hit_cost_offpeak + judge_miss_cost_offpeak + judge_out_cost_offpeak

    rag_in_cost_peak = (rag_input_tokens / 1_000_000) * 2.00
    rag_out_cost_peak = (rag_output_tokens / 1_000_000) * 8.00

    judge_hit_cost_peak = (judge_cache_hit_tokens / 1_000_000) * 0.04
    judge_miss_cost_peak = (judge_miss / 1_000_000) * 2.00
    judge_out_cost_peak = (judge_completion_tokens / 1_000_000) * 8.00

    total_peak = rag_in_cost_peak + rag_out_cost_peak + judge_hit_cost_peak + judge_miss_cost_peak + judge_out_cost_peak

    return {
        "pricing_model": "deepseek-flash (DeepSeek-V4.1-Flash-1017)",
        "tokens": {
            "rag_input": rag_input_tokens,
            "rag_output": rag_output_tokens,
            "judge_input": judge_prompt_tokens,
            "judge_cache_hit": judge_cache_hit_tokens,
            "judge_cache_miss": judge_miss,
            "judge_output": judge_completion_tokens,
            "total_input": rag_input_tokens + judge_prompt_tokens,
            "total_output": rag_output_tokens + judge_completion_tokens,
            "total_tokens": rag_input_tokens + rag_output_tokens + judge_prompt_tokens + judge_completion_tokens,
        },
        "cost_rmb": {
            "off_peak_total": round(total_offpeak, 6),
            "peak_total": round(total_peak, 6),
            "off_peak_rag": round(rag_in_cost_offpeak + rag_out_cost_offpeak, 6),
            "off_peak_judge": round(judge_hit_cost_offpeak + judge_miss_cost_offpeak + judge_out_cost_offpeak, 6),
            "peak_rag": round(rag_in_cost_peak + rag_out_cost_peak, 6),
            "peak_judge": round(judge_hit_cost_peak + judge_miss_cost_peak + judge_out_cost_peak, 6),
        }
    }


def clone_eval_database(src_path: Path, dst_path: Path):
    """
    Safely clone eval_benchmark.db into generation_eval_run.db using SQLite Backup API.
    Guarantees zero pollution to agentstudio_rag.db and eval_benchmark.db.
    """
    if not src_path.exists():
        raise FileNotFoundError(f"Source evaluation database not found at {src_path}")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()

    src_conn = sqlite3.connect(str(src_path))
    dst_conn = sqlite3.connect(str(dst_path))
    with dst_conn:
        src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()

    # Configure deepseek-flash in the replica
    setup_eval_model_config(dst_path)


class SSEStreamParser:
    """Parses SSE lines emitted from FastAPI StreamingResponse."""

    @staticmethod
    def parse_events(raw_text: str) -> List[Dict[str, Any]]:
        events = []
        blocks = raw_text.strip().split("\n\n")
        for b in blocks:
            if not b.strip():
                continue
            event_name = "message"
            data_val = None
            for line in b.split("\n"):
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    raw_data = line[len("data:"):].strip()
                    try:
                        data_val = json.loads(raw_data)
                    except json.JSONDecodeError:
                        data_val = raw_data
            if data_val is not None:
                events.append({"event": event_name, "data": data_val})
        return events


def find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EphemeralUvicornServer:
    """
    Spins up an ephemeral, in-process Uvicorn TCP server exclusively bound to the isolated
    FastAPI application instance for authentic TCP streaming benchmarks.
    Zero risk of connecting to production database.
    """
    def __init__(self, app, port: Optional[int] = None):
        import uvicorn
        self.port = port or find_free_port()
        self.config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
            loop="asyncio",
        )
        self.server = uvicorn.Server(self.config)
        self._task = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.server.should_exit = True
        if self._task:
            await self._task


class GenerationBenchmarkRunner:
    """
    Drives end-to-end RAG generation benchmark tests via real FastAPI SSE API.
    """

    def __init__(
        self,
        dataset_path: Optional[Path] = None,
        reports_dir: Optional[Path] = None,
        mock_judge: bool = False,
    ):
        base_dir = Path(__file__).resolve().parent.parent
        self.dataset_path = dataset_path or (base_dir / "datasets" / "benchmark_generation_v1.jsonl")
        self.reports_dir = reports_dir or (base_dir / "reports")
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.mock_judge = mock_judge
        self.judge = JudgeEvaluator(test_mode=mock_judge, api_key=settings.OPENAI_API_KEY)

        # Context observation container for kept_chunks capture
        self._last_kept_chunks: List[Any] = []

    def load_samples(
        self,
        sample_id: Optional[str] = None,
        sample_ids: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Load benchmark samples from JSONL."""
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"Dataset not found at {self.dataset_path}")

        samples = []
        with open(self.dataset_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    samples.append(json.loads(line_str))

        if sample_ids:
            sid_set = set(sample_ids)
            sample_map = {s.get("id"): s for s in samples if s.get("id") in sid_set}
            samples = [sample_map[sid] for sid in sample_ids if sid in sample_map]
        elif sample_id:
            samples = [s for s in samples if s.get("id") == sample_id]
        if limit and limit > 0:
            samples = samples[:limit]

        return samples

    async def execute_query_sse(
        self,
        query: str,
        client: Optional[httpx.AsyncClient] = None,
        transport: Optional[ASGITransport] = None,
    ) -> Dict[str, Any]:
        """
        Executes a real query against the FastAPI SSE chat endpoint.
        Measures Client TTFT, Server TTFT, stage latencies, tokens, and response text.
        Supports both real TCP socket client and in-memory ASGITransport.
        """
        # Hook PromptBuilder to observe kept_chunks safely in memory
        orig_build = PromptBuilder.build_user_prompt
        self._last_kept_chunks = []

        def wrapped_build(builder_self, query, chunks, **kwargs):
            self._last_kept_chunks = list(chunks)
            return orig_build(builder_self, query=query, chunks=chunks, **kwargs)

        PromptBuilder.build_user_prompt = wrapped_build

        t_request_sent = time.perf_counter()
        t_first_token: Optional[float] = None
        t_last_token: Optional[float] = None

        request_payload = {
            "query": query,
            "knowledge_base_id": EVAL_KB_ID,
            "mode": "workflow",
            "temperature": 0.0,
        }

        full_answer = ""
        citations_extracted: List[Dict[str, Any]] = []
        trace_info: Dict[str, Any] = {}
        retrieval_info: Dict[str, Any] = {}
        query_rewrite_info: Dict[str, Any] = {}

        async def _stream_with_client(target_client: httpx.AsyncClient):
            nonlocal t_first_token, t_last_token, full_answer, citations_extracted, trace_info, retrieval_info, query_rewrite_info
            async with target_client.stream(
                "POST",
                "/api/v1/chat/completions/stream",
                json=request_payload,
                timeout=120.0,
            ) as resp:
                if resp.status_code != 200:
                    err_body = await resp.aread()
                    raise RuntimeError(f"HTTP {resp.status_code}: {err_body.decode('utf-8', errors='ignore')}")

                curr_event = "message"
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        curr_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        raw_data = line[len("data:"):].strip()
                        try:
                            data_obj = json.loads(raw_data)
                        except json.JSONDecodeError:
                            data_obj = raw_data

                        if curr_event == "token":
                            delta_txt = data_obj.get("delta", "") if isinstance(data_obj, dict) else str(data_obj)
                            if delta_txt:
                                now = time.perf_counter()
                                if t_first_token is None:
                                    t_first_token = now
                                t_last_token = now
                                full_answer += delta_txt
                        elif curr_event == "citations":
                            if isinstance(data_obj, list):
                                citations_extracted = data_obj
                        elif curr_event == "trace":
                            if isinstance(data_obj, dict):
                                trace_info = data_obj
                        elif curr_event == "retrieval":
                            if isinstance(data_obj, dict):
                                retrieval_info = data_obj
                        elif curr_event == "query_rewrite":
                            if isinstance(data_obj, dict):
                                query_rewrite_info = data_obj

        try:
            if client is not None:
                await _stream_with_client(client)
            elif transport is not None:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as local_client:
                    await _stream_with_client(local_client)
            else:
                raise ValueError("Either client or transport must be provided to execute_query_sse")
        finally:
            PromptBuilder.build_user_prompt = orig_build

        t_finished = time.perf_counter()
        client_ttft_ms = round((t_first_token - t_request_sent) * 1000.0, 2) if t_first_token else None
        client_generation_ms = round((t_last_token - t_first_token) * 1000.0, 2) if (t_first_token and t_last_token) else None
        client_total_ms = round((t_finished - t_request_sent) * 1000.0, 2)

        # Decompose server-side stage latencies
        retrieval_wall_ms = trace_info.get("retrieval_duration_ms") or retrieval_info.get("retrieval_duration_ms")
        dense_duration_ms = trace_info.get("dense_duration_ms") or retrieval_info.get("dense_duration_ms")
        bm25_duration_ms = trace_info.get("bm25_duration_ms") or retrieval_info.get("bm25_duration_ms")
        rrf_duration_ms = trace_info.get("rrf_duration_ms") or retrieval_info.get("rrf_duration_ms")
        reranker_duration_ms = trace_info.get("reranker_duration_ms") or retrieval_info.get("reranker_duration_ms")
        parallel_retrieval_wall_ms = (
            round(retrieval_wall_ms - (reranker_duration_ms or 0.0), 2)
            if (retrieval_wall_ms is not None and reranker_duration_ms is not None)
            else None
        )

        return {
            "answer": full_answer,
            "citations": citations_extracted,
            "kept_chunks": list(self._last_kept_chunks),
            "client_ttft_ms": client_ttft_ms,
            "client_generation_ms": client_generation_ms,
            "client_total_ms": client_total_ms,
            "server_ttft_ms": trace_info.get("ttft_ms"),
            "server_total_ms": trace_info.get("total_duration_ms"),
            "generation_duration_ms": trace_info.get("generation_duration_ms"),
            "retrieval_duration_ms": retrieval_wall_ms,
            "dense_duration_ms": dense_duration_ms,
            "bm25_duration_ms": bm25_duration_ms,
            "parallel_retrieval_wall_ms": parallel_retrieval_wall_ms,
            "rrf_duration_ms": rrf_duration_ms,
            "reranker_duration_ms": reranker_duration_ms,
            "rewrite_duration_ms": trace_info.get("rewrite_duration_ms") or query_rewrite_info.get("duration_ms"),
            "input_tokens": trace_info.get("actual_input_tokens"),
            "output_tokens": trace_info.get("actual_output_tokens"),
            "retrieved_chunks_count": trace_info.get("retrieved_chunks_count"),
            "llm_model": trace_info.get("llm_model", ""),
        }

    async def run_benchmark(
        self,
        sample_id: Optional[str] = None,
        sample_ids: Optional[List[str]] = None,
        limit: Optional[int] = None,
        smoke_test: bool = False,
        transport_mode: str = "tcp",
        output_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes benchmark run over target samples, judges each response, and exports artifacts.
        Supports both authentic TCP Socket mode and in-memory ASGI Transport.
        """
        is_smoke = smoke_test or (sample_id is not None and sample_id == "GEN-01" and not sample_ids)

        print("=" * 88)
        print(f" AgentStudio-RAG — {'SINGLE-SAMPLE SMOKE TEST (GEN-01, V2)' if is_smoke else 'Generation Quality & Performance Benchmark Runner'}")
        print("=" * 88)

        # Baseline database checksum verification
        sha_src_before = calculate_file_sha256(SRC_BENCHMARK_DB)
        sha_prod_before = calculate_file_sha256(PROD_DB)
        print(f"[*] Pre-flight integrity checksums:")
        print(f"    - eval_benchmark.db:  {sha_src_before}")
        print(f"    - agentstudio_rag.db: {sha_prod_before}")

        # 1. Database isolation: clone to generation_eval_run.db
        print(f"[*] Initializing database isolation snapshot: {RUN_BENCHMARK_DB.name}...")
        clone_eval_database(SRC_BENCHMARK_DB, RUN_BENCHMARK_DB)

        # Setup async engine for isolated DB
        eval_db_url = f"sqlite+aiosqlite:///{RUN_BENCHMARK_DB.as_posix()}"
        eval_engine = create_async_engine(eval_db_url, echo=False)
        eval_session_factory = sessionmaker(eval_engine, class_=AsyncSession, expire_on_commit=False)

        async def override_get_db():
            async with eval_session_factory() as session:
                try:
                    yield session
                finally:
                    await session.close()

        # Apply FastAPI dependency override & session factory isolation
        import app.db.session
        import app.memory.extractor
        orig_db_factory = app.db.session.async_session_factory
        orig_extractor_factory = app.memory.extractor.async_session_factory
        app.db.session.async_session_factory = eval_session_factory
        app.memory.extractor.async_session_factory = eval_session_factory
        fastapi_app.dependency_overrides[get_db] = override_get_db
        os.environ["SKIP_LIFESPAN_DB_INIT"] = "1"

        samples = self.load_samples(sample_id=sample_id, sample_ids=sample_ids, limit=limit)
        print(f"[*] Loaded {len(samples)} samples to evaluate (Mock Judge: {self.mock_judge}, Transport: {transport_mode.upper()}).\n")

        detailed_results = []
        latencies_client_ttft = []
        latencies_server_ttft = []
        latencies_total = []
        latencies_retrieval = []
        latencies_generation = []
        token_inputs = []
        token_outputs = []

        scores_correctness = []
        scores_faithfulness = []
        scores_citation = []
        scores_relevancy = []
        unanswerable_rejections = []

        async def _run_eval_loop(http_client: httpx.AsyncClient):
            for idx, s in enumerate(samples, start=1):
                qid = s["id"]
                cat = s["category"]
                query = s["query"]
                ref_ans = s["reference_answer"]
                answerable = s.get("answerable", True)

                print(f"[{idx:02d}/{len(samples)}] Executing {qid} ({cat}): {query[:32]}...")

                # A. Execute through real SSE API (strictly 1 call, zero retries)
                exec_res = await self.execute_query_sse(query, client=http_client)

                # B. Judge the generated response (strictly 1 call, zero retries)
                print(f"    - Invoking LLM Judge ({self.judge.model_name})...")
                judge_res: JudgeEvaluationResult = await self.judge.evaluate_sample(
                    sample_id=qid,
                    query=query,
                    reference_answer=ref_ans,
                    kept_chunks=exec_res["kept_chunks"],
                    generated_answer=exec_res["answer"],
                    extracted_citations=exec_res["citations"],
                    answerable=answerable,
                )

                # Collect latency metrics
                if exec_res["client_ttft_ms"]:
                    latencies_client_ttft.append(exec_res["client_ttft_ms"])
                if exec_res["server_ttft_ms"]:
                    latencies_server_ttft.append(exec_res["server_ttft_ms"])
                if exec_res["client_total_ms"]:
                    latencies_total.append(exec_res["client_total_ms"])
                if exec_res["retrieval_duration_ms"]:
                    latencies_retrieval.append(exec_res["retrieval_duration_ms"])
                if exec_res["generation_duration_ms"]:
                    latencies_generation.append(exec_res["generation_duration_ms"])
                if exec_res["input_tokens"]:
                    token_inputs.append(exec_res["input_tokens"])
                if exec_res["output_tokens"]:
                    token_outputs.append(exec_res["output_tokens"])

                # Collect quality scores
                if judge_res.status == "success":
                    if answerable:
                        scores_correctness.append(judge_res.correctness_score)
                        scores_faithfulness.append(judge_res.faithfulness_score)
                        scores_citation.append(judge_res.citation_accuracy_score)
                        scores_relevancy.append(judge_res.relevancy_score)
                    else:
                        unanswerable_rejections.append(1.0 if judge_res.rejection_detected else 0.0)

                # Cost computation for this sample
                cost_info = calculate_deepseek_cost(
                    rag_input_tokens=exec_res.get("input_tokens") or 0,
                    rag_output_tokens=exec_res.get("output_tokens") or 0,
                    judge_prompt_tokens=judge_res.judge_input_tokens or 0,
                    judge_completion_tokens=judge_res.judge_output_tokens or 0,
                    judge_cache_hit_tokens=judge_res.judge_cache_hit_tokens or 0,
                    judge_cache_miss_tokens=judge_res.judge_cache_miss_tokens or 0,
                )

                record = {
                    "id": qid,
                    "category": cat,
                    "query": query,
                    "answerable": answerable,
                    "reference_answer": ref_ans,
                    "generated_answer": exec_res["answer"],
                    "citations_count": len(exec_res["citations"]),
                    "citations": exec_res["citations"],
                    "kept_chunks_count": len(exec_res["kept_chunks"]),
                    "performance": {
                        "transport_used": transport_mode,
                        "client_ttft_ms": exec_res["client_ttft_ms"],
                        "client_generation_ms": exec_res["client_generation_ms"],
                        "client_total_ms": exec_res["client_total_ms"],
                        "server_ttft_ms": exec_res["server_ttft_ms"],
                        "server_generation_ms": exec_res["generation_duration_ms"],
                        "server_total_ms": exec_res["server_total_ms"],
                        "dense_branch_duration_ms": exec_res["dense_duration_ms"],
                        "bm25_branch_duration_ms": exec_res["bm25_duration_ms"],
                        "parallel_retrieval_wall_ms": exec_res["parallel_retrieval_wall_ms"],
                        "rrf_duration_ms": exec_res["rrf_duration_ms"],
                        "reranker_duration_ms": exec_res["reranker_duration_ms"],
                        "total_retrieval_duration_ms": exec_res["retrieval_duration_ms"],
                        "rewrite_duration_ms": exec_res["rewrite_duration_ms"],
                        "input_tokens": exec_res["input_tokens"],
                        "output_tokens": exec_res["output_tokens"],
                        "llm_model_tested": exec_res.get("llm_model"),
                    },
                    "judge_evaluation": judge_res.model_dump(),
                    "cost_accounting": cost_info,
                }
                detailed_results.append(record)
                print(f"    - Score: Corr={judge_res.correctness_score} | Faith={judge_res.faithfulness_score} | Cit={judge_res.citation_accuracy_score} | Rel={judge_res.relevancy_score} | Client-TTFT={exec_res['client_ttft_ms']}ms | Server-TTFT={exec_res['server_ttft_ms']}ms")

        # Warmup local models in-process so initial queries do not suffer cold-start ONNX load penalty
        print("[*] Pre-warming local embedding and reranker ONNX models...")
        from app.llm.embeddings import get_embedding_client
        from app.retrieval.reranker import get_reranker_client
        get_embedding_client()
        get_reranker_client()
        print("[*] Local embedding & reranker models warm and resident in memory.")

        try:
            if transport_mode == "tcp":
                print(f"[*] Starting Ephemeral Uvicorn TCP Server bound exclusively to {RUN_BENCHMARK_DB.name}...")
                async with EphemeralUvicornServer(fastapi_app) as uvicorn_srv:
                    base_url = f"http://127.0.0.1:{uvicorn_srv.port}"
                    print(f"[*] Ephemeral Uvicorn TCP Server active on {base_url} (Real TCP Socket)")
                    async with httpx.AsyncClient(base_url=base_url, timeout=120.0, trust_env=False) as http_client:
                        await _run_eval_loop(http_client)
            else:
                transport = ASGITransport(app=fastapi_app)
                print("[*] Running benchmark with ASGITransport (Transport: In-Memory ASGI)")
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=120.0) as http_client:
                    await _run_eval_loop(http_client)

        finally:
            fastapi_app.dependency_overrides.clear()
            app.db.session.async_session_factory = orig_db_factory
            app.memory.extractor.async_session_factory = orig_extractor_factory
            await eval_engine.dispose()

        # Post-flight integrity check
        sha_src_after = calculate_file_sha256(SRC_BENCHMARK_DB)
        sha_prod_after = calculate_file_sha256(PROD_DB)

        # Count writes in isolated replica
        replica_conn = sqlite3.connect(str(RUN_BENCHMARK_DB))
        c = replica_conn.cursor()
        conv_count = c.execute("SELECT count(*) FROM conversations").fetchone()[0]
        msg_count = c.execute("SELECT count(*) FROM messages").fetchone()[0]
        replica_conn.close()

        print("\n" + "=" * 88)
        print(" [*] Database Isolation Verification:")
        print(f"     - eval_benchmark.db untouched:  {'PASS (Checksum matched)' if sha_src_before == sha_src_after else 'FAIL'}")
        print(f"     - agentstudio_rag.db untouched: {'PASS (Checksum matched)' if sha_prod_before == sha_prod_after else 'FAIL'}")
        print(f"     - Writes captured in replica:   {conv_count} conversation(s), {msg_count} message(s)")
        print("=" * 88)

        # If smoke test mode, save ONLY to smoke_test_gen01_v2_result.json and DO NOT touch formal benchmark reports
        if is_smoke:
            smoke_file = self.reports_dir / "smoke_test_gen01_v2_result.json"
            smoke_report = {
                "benchmark_stage": "E2.1.2_generation_smoke_test_v2",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "transport_mode": transport_mode,
                "sample_evaluated": detailed_results[0] if detailed_results else None,
                "database_isolation": {
                    "eval_benchmark_sha256_before": sha_src_before,
                    "eval_benchmark_sha256_after": sha_src_after,
                    "eval_benchmark_untouched": sha_src_before == sha_src_after,
                    "agentstudio_rag_sha256_before": sha_prod_before,
                    "agentstudio_rag_sha256_after": sha_prod_after,
                    "agentstudio_rag_untouched": sha_prod_before == sha_prod_after,
                    "replica_conversations_created": conv_count,
                    "replica_messages_created": msg_count,
                }
            }
            with open(smoke_file, "w", encoding="utf-8") as f:
                json.dump(smoke_report, f, ensure_ascii=False, indent=2)
            print(f"\n[+] Single-sample smoke test results saved exclusively to:\n    {smoke_file}")
            print("    (Historical smoke_test_gen01_result.json was untouched and preserved.)")
            return smoke_report

        # Compute summary statistics
        def calc_stats(vals: List[float]) -> Dict[str, float]:
            if not vals:
                return {"p50": 0.0, "p95": 0.0, "mean": 0.0}
            s = sorted(vals)
            import statistics
            p50 = round(statistics.median(s), 2)
            idx95 = min(int(len(s) * 0.95), len(s) - 1)
            p95 = round(s[idx95], 2)
            mean = round(sum(s) / len(s), 2)
            return {"p50": p50, "p95": p95, "mean": mean}

        import statistics

        # Compute category-level performance breakdown
        categories = sorted(list(set(r["category"] for r in detailed_results)))
        category_breakdown = {}
        for cat in categories:
            cat_samples = [r for r in detailed_results if r["category"] == cat]
            c_scores = [r["judge_evaluation"]["correctness_score"] for r in cat_samples if r["judge_evaluation"].get("correctness_score") is not None]
            f_scores = [r["judge_evaluation"]["faithfulness_score"] for r in cat_samples if r["judge_evaluation"].get("faithfulness_score") is not None]
            cit_scores = [r["judge_evaluation"]["citation_accuracy_score"] for r in cat_samples if r["judge_evaluation"].get("citation_accuracy_score") is not None]
            rel_scores = [r["judge_evaluation"]["relevancy_score"] for r in cat_samples if r["judge_evaluation"].get("relevancy_score") is not None]
            rej_scores = [1.0 if r["judge_evaluation"].get("rejection_detected") else 0.0 for r in cat_samples if not r["answerable"]]
            ttft_vals = [r["performance"]["client_ttft_ms"] for r in cat_samples if r["performance"].get("client_ttft_ms") is not None]

            category_breakdown[cat] = {
                "count": len(cat_samples),
                "correctness_mean": round(sum(c_scores)/len(c_scores), 2) if c_scores else None,
                "faithfulness_mean": round(sum(f_scores)/len(f_scores), 2) if f_scores else None,
                "citation_accuracy_mean": round(sum(cit_scores)/len(cit_scores), 2) if cit_scores else None,
                "relevancy_mean": round(sum(rel_scores)/len(rel_scores), 2) if rel_scores else None,
                "rejection_accuracy": round(sum(rej_scores)/len(rej_scores), 2) if rej_scores else None,
                "client_ttft_p50": round(statistics.median(ttft_vals), 2) if ttft_vals else 0.0,
            }

        # Total cost accounting
        total_rag_input_tokens = sum(r["cost_accounting"]["tokens"]["rag_input"] for r in detailed_results)
        total_rag_output_tokens = sum(r["cost_accounting"]["tokens"]["rag_output"] for r in detailed_results)
        total_judge_input_tokens = sum(r["cost_accounting"]["tokens"]["judge_input"] for r in detailed_results)
        total_judge_output_tokens = sum(r["cost_accounting"]["tokens"]["judge_output"] for r in detailed_results)
        total_tokens = sum(r["cost_accounting"]["tokens"]["total_tokens"] for r in detailed_results)
        total_cost_off_peak = sum(r["cost_accounting"]["cost_rmb"]["off_peak_total"] for r in detailed_results)
        total_cost_peak = sum(r["cost_accounting"]["cost_rmb"]["peak_total"] for r in detailed_results)

        summary_metrics = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_evaluated_samples": len(detailed_results),
            "answerable_samples_count": len(scores_correctness),
            "unanswerable_samples_count": len(unanswerable_rejections),
            "quality_scores_mean": {
                "answer_correctness": round(sum(scores_correctness) / len(scores_correctness), 2) if scores_correctness else 0.0,
                "faithfulness": round(sum(scores_faithfulness) / len(scores_faithfulness), 2) if scores_faithfulness else 0.0,
                "citation_accuracy": round(sum(scores_citation) / len(scores_citation), 2) if scores_citation else 0.0,
                "answer_relevancy": round(sum(scores_relevancy) / len(scores_relevancy), 2) if scores_relevancy else 0.0,
                "rejection_accuracy": round(sum(unanswerable_rejections) / len(unanswerable_rejections), 2) if unanswerable_rejections else 0.0,
            },
            "category_breakdown": category_breakdown,
            "performance_stats": {
                "client_ttft_ms": calc_stats(latencies_client_ttft),
                "server_ttft_ms": calc_stats(latencies_server_ttft),
                "total_latency_ms": calc_stats(latencies_total),
                "retrieval_duration_ms": calc_stats(latencies_retrieval),
                "generation_duration_ms": calc_stats(latencies_generation),
                "input_tokens_mean": round(sum(token_inputs) / len(token_inputs), 1) if token_inputs else 0.0,
                "output_tokens_mean": round(sum(token_outputs) / len(token_outputs), 1) if token_outputs else 0.0,
            },
            "cost_summary": {
                "total_tokens": total_tokens,
                "rag_input_tokens": total_rag_input_tokens,
                "rag_output_tokens": total_rag_output_tokens,
                "judge_input_tokens": total_judge_input_tokens,
                "judge_output_tokens": total_judge_output_tokens,
                "off_peak_rmb": round(total_cost_off_peak, 4),
                "peak_rmb": round(total_cost_peak, 4),
            }
        }

        # Export JSON
        json_file = self.reports_dir / (f"{output_prefix}.json" if output_prefix else "generation_eval_results.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({
                "summary": summary_metrics,
                "samples": detailed_results,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n[+] Exported detailed JSON results to {json_file}")

        # Export CSV
        csv_file = self.reports_dir / (f"{output_prefix}.csv" if output_prefix else "generation_eval_summary.csv")
        csv_rows = []
        for r in detailed_results:
            perf = r["performance"]
            j = r["judge_evaluation"]
            csv_rows.append({
                "id": r["id"],
                "category": r["category"],
                "answerable": r["answerable"],
                "correctness_score": j.get("correctness_score"),
                "faithfulness_score": j.get("faithfulness_score"),
                "citation_accuracy_score": j.get("citation_accuracy_score"),
                "relevancy_score": j.get("relevancy_score"),
                "rejection_detected": j.get("rejection_detected"),
                "client_ttft_ms": perf.get("client_ttft_ms"),
                "server_ttft_ms": perf.get("server_ttft_ms"),
                "retrieval_ms": perf.get("retrieval_duration_ms"),
                "generation_ms": perf.get("generation_duration_ms"),
                "total_ms": perf.get("client_total_ms"),
                "input_tokens": perf.get("input_tokens"),
                "output_tokens": perf.get("output_tokens"),
            })

        if csv_rows:
            with open(csv_file, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
                writer.writeheader()
                writer.writerows(csv_rows)
            print(f"[+] Exported summary CSV to {csv_file}")

        # Export Markdown Report
        md_file = self.reports_dir / (f"{output_prefix}.md" if output_prefix else "rag_generation_benchmark_report.md")
        self._write_markdown_report(md_file, summary_metrics, detailed_results, output_prefix=output_prefix)
        print(f"[+] Exported Markdown Benchmark Report to {md_file}")

        return summary_metrics

    def _write_markdown_report(
        self,
        out_path: Path,
        summary: Dict[str, Any],
        samples: List[Dict[str, Any]],
        output_prefix: Optional[str] = None,
    ):
        """Generate comprehensive Markdown Benchmark Report."""
        q_mean = summary["quality_scores_mean"]
        p_stats = summary["performance_stats"]
        cat_breakdown = summary.get("category_breakdown", {})
        cost_sum = summary.get("cost_summary", {})

        report_title = "# AgentStudio-RAG — E2.1.4 定向整改 10 题真实系统复测报告" if output_prefix else "# AgentStudio-RAG — 生成质量与系统性能全链路评测报告 (30 题基准)"

        lines = [
            report_title,
            "",
            f"> **评测类型**：{'E2.1.4 修复后 10 题真实系统复测 (LLM-as-a-Judge)' if output_prefix else '端到端 RAG 生成质量基准评测 (LLM-as-a-Judge) 与真实 TCP 套接字性能度量'}  ",
            f"> **评测时间**：{summary.get('timestamp', 'N/A')}  ",
            f"> **评测样本总量**：{summary.get('total_evaluated_samples', 0)} 题（包含可回答样本 {summary.get('answerable_samples_count', 0)} 题，不可回答防幻觉拒答样本 {summary.get('unanswerable_samples_count', 0)} 题）  ",
            "> **被测模型**：DeepSeek-Flash (`deepseek-flash` / DeepSeek-V4.1-Flash-1017)  ",
            "> **裁判模型 (Judge)**：DeepSeek-Flash (`thinking: disabled`, `temperature: 0.0`)  ",
            "> **Reranker 模型**：`Xenova/bge-reranker-base` (ONNX Quantized, CPU, Top-20 -> Top-4)  ",
            "> **网络协议模式**：真实本地 TCP Socket (`EphemeralUvicornServer` + `httpx.AsyncClient(trust_env=False)`)  ",
            "> **数据库隔离机制**：基于 `eval_benchmark.db` 独立热备副本 `generation_eval_run.db`，原库 SHA256 零变动  ",
            "",
            "---",
            "",
            "## 一、核心生成质量量化得分 (LLM-as-a-Judge 四维度)",
            "",
            "> [!NOTE]",
            "> **关于 Judge 评分的性质说明**：LLM-as-a-Judge 提供了可复现的自动化相对评估标尺，反映模型在特定 Prompt 指引下的遵从度与一致性，不应直接等同于 100% 客观的人类真理准确率。评测已通过严格关闭思考模式并锁定 temperature=0.0 以消除随机性波动。",
            "",
            "| 质量维度 | 平均得分 (1~5 分制) | 维度释义与技术评估要求 |",
            "| :--- | :---: | :--- |",
            f"| **Answer Correctness (正确性)** | **{q_mean.get('answer_correctness', 0.0):.2f} / 5.0** | 核心技术事实与标准答案是否一致，技术术语、参数名称、等级名称严格一致 |",
            f"| **Faithfulness (上下文忠实度)** | **{q_mean.get('faithfulness', 0.0):.2f} / 5.0** | 回答声明是否严格受检索到的 Context 支撑，零脑补、零幻觉 |",
            f"| **Citation Accuracy (引用准确性)** | **{q_mean.get('citation_accuracy', 0.0):.2f} / 5.0** | 正文角标 `[Ref-X]` 是否程序化合法，且内容实质性支持所对应陈述 |",
            f"| **Answer Relevancy (回答切题度)** | **{q_mean.get('answer_relevancy', 0.0):.2f} / 5.0** | 是否精准直击提问核心，结构清晰严谨，无冗余多余背景铺垫 |",
            f"| **Rejection Accuracy (正确拒答率)** | **{q_mean.get('rejection_accuracy', 0.0):.2%}** | 面对无依据超纲问题（answerable=false），如实拒答且拒绝幻觉编造的比率 |",
            "",
            "---",
            "",
            "## 二、分场景分类得分统计",
            "",
            "| 场景分类 | 样本数 | Correctness | Faithfulness | Citation | Relevancy | 正确拒答率 | Client TTFT (P50) |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
        ]

        cat_names = {
            "single_fact": "单事实检索 (Single Fact)",
            "multi_hop": "跨文档多跳推理 (Multi-hop)",
            "table_reasoning": "表格数值计算 (Table Reasoning)",
            "unanswerable": "不可回答拒答 (Unanswerable)",
        }
        for cat, stats in cat_breakdown.items():
            label = cat_names.get(cat, cat)
            corr_s = f"{stats['correctness_mean']:.2f}" if stats['correctness_mean'] is not None else "-"
            faith_s = f"{stats['faithfulness_mean']:.2f}" if stats['faithfulness_mean'] is not None else "-"
            cit_s = f"{stats['citation_accuracy_mean']:.2f}" if stats['citation_accuracy_mean'] is not None else "-"
            rel_s = f"{stats['relevancy_mean']:.2f}" if stats['relevancy_mean'] is not None else "-"
            rej_s = f"{stats['rejection_accuracy']:.1%}" if stats['rejection_accuracy'] is not None else "-"
            ttft_s = f"{stats['client_ttft_p50']} ms"
            lines.append(f"| **{label}** | {stats['count']} | {corr_s} | {faith_s} | {cit_s} | {rel_s} | {rej_s} | {ttft_s} |")

        lines.extend([
            "",
            "---",
            "",
            "## 三、系统全链路性能指标看板 (真实 TCP 测量)",
            "",
            "| 性能指标 | P50 (中位数) | P95 (95 分位) | 均值 (Mean) | 阶段定义与工程分析 |",
            "| :--- | :---: | :---: | :---: | :--- |",
            f"| **Client-Perceived TTFT (首字延迟)** | {p_stats['client_ttft_ms']['p50']} ms | {p_stats['client_ttft_ms']['p95']} ms | {p_stats['client_ttft_ms']['mean']} ms | 客户端在 TCP Socket 上读取到首个有效 Token 的挂钟耗时 |",
            f"| **Server LLM TTFT (大模型首字)** | {p_stats['server_ttft_ms']['p50']} ms | {p_stats['server_ttft_ms']['p95']} ms | {p_stats['server_ttft_ms']['mean']} ms | 服务端向 DeepSeek 发起请求至首个 Token 返回的纯 Prefill 耗时 |",
            f"| **混合检索挂钟耗时** | {p_stats['retrieval_duration_ms']['p50']} ms | {p_stats['retrieval_duration_ms']['p95']} ms | {p_stats['retrieval_duration_ms']['mean']} ms | 并行检索 (Dense+BM25) + RRF 融合 + Cross-Encoder 重排总和 |",
            f"| **大模型完整生成耗时** | {p_stats['generation_duration_ms']['p50']} ms | {p_stats['generation_duration_ms']['p95']} ms | {p_stats['generation_duration_ms']['mean']} ms | 从发起到最后一个 Token 完成的生成全周期（含 TTFT + Token 流） |",
            f"| **端到端总延迟 (Total Latency)** | {p_stats['total_latency_ms']['p50']} ms | {p_stats['total_latency_ms']['p95']} ms | {p_stats['total_latency_ms']['mean']} ms | 客户端完整 HTTP 请求生命周期（连接握手 + 内部执行 + 持久化） |",
            "",
            f"- **平均 Prompt 消耗**：`{p_stats.get('input_tokens_mean', 0)}` Tokens / 请求",
            f"- **平均 Completion 产出**：`{p_stats.get('output_tokens_mean', 0)}` Tokens / 请求",
            "",
            "---",
            "",
            "## 四、Token 消耗与实际 API 成本核算",
            "",
            "| 模块 / 角色 | 输入 Tokens | 输出 Tokens | 合计 Tokens | 计费单价 (百万 Tokens) | 非高峰期成本 (00:30-08:30) | 高峰期成本 (08:30-00:30) |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
            f"| **被测 RAG 系统** | {cost_sum.get('rag_input_tokens', 0):,} | {cost_sum.get('rag_output_tokens', 0):,} | {(cost_sum.get('rag_input_tokens', 0) + cost_sum.get('rag_output_tokens', 0)):,} | 0.5元(Miss)/1.0元(Out) | 包含在总计中 | 包含在总计中 |",
            f"| **Judge 裁判** | {cost_sum.get('judge_input_tokens', 0):,} | {cost_sum.get('judge_output_tokens', 0):,} | {(cost_sum.get('judge_input_tokens', 0) + cost_sum.get('judge_output_tokens', 0)):,} | 0.5元(Miss)/1.0元(Out) | 包含在总计中 | 包含在总计中 |",
            f"| **全量 30 题总计** | **{(cost_sum.get('rag_input_tokens', 0) + cost_sum.get('judge_input_tokens', 0)):,}** | **{(cost_sum.get('rag_output_tokens', 0) + cost_sum.get('judge_output_tokens', 0)):,}** | **{cost_sum.get('total_tokens', 0):,}** | - | **¥{cost_sum.get('off_peak_rmb', 0):.4f} 元** | **¥{cost_sum.get('peak_rmb', 0):.4f} 元** |",
            "",
            f"> [!TIP]",
            f"> 30 题全量评测实际花费不到 **{cost_sum.get('peak_rmb', 0):.2f} 元**（非高峰期仅需约 **{cost_sum.get('off_peak_rmb', 0):.2f} 元**），兼具极高成本效益与工业级复现价值。",
            "",
            "---",
            "",
            "## 五、典型成功案例与失败/降级案例分析",
            "",
        ])

        # Pick top success cases and bottom degraded cases
        answerable_samples = [s for s in samples if s["answerable"] and s["judge_evaluation"].get("overall_score") is not None]
        sorted_by_score = sorted(answerable_samples, key=lambda s: s["judge_evaluation"]["overall_score"], reverse=True)
        unans_samples = [s for s in samples if not s["answerable"]]

        top_success = sorted_by_score[:2] if len(sorted_by_score) >= 2 else sorted_by_score
        bottom_degraded = sorted_by_score[-2:] if len(sorted_by_score) >= 2 else []

        lines.append("### 1. 典型成功案例 (High Quality Case)")
        for s in top_success:
            j = s["judge_evaluation"]
            lines.extend([
                f"- **样本 ID**：`{s['id']}` ({s['category']})",
                f"  - **提问**：{s['query']}",
                f"  - **Judge 得分**：Correctness={j['correctness_score']} | Faithfulness={j['faithfulness_score']} | Citation={j['citation_accuracy_score']} | Relevancy={j['relevancy_score']} (综合: {j['overall_score']:.2f})",
                f"  - **成功要素**：{j['correctness_reason']} 引用了 {len(s['citations'])} 处证据，程序化引文严格吻合。",
            ])

        lines.append("\n### 2. 典型降级/扣分案例 (Degraded / Penalized Case)")
        for s in bottom_degraded:
            j = s["judge_evaluation"]
            lines.extend([
                f"- **样本 ID**：`{s['id']}` ({s['category']})",
                f"  - **提问**：{s['query']}",
                f"  - **Judge 得分**：Correctness={j['correctness_score']} | Faithfulness={j['faithfulness_score']} | Citation={j['citation_accuracy_score']} | Relevancy={j['relevancy_score']} (综合: {j['overall_score']:.2f})",
                f"  - **扣分原因**：{j['correctness_reason']} / {j['relevancy_reason']}",
            ])

        lines.append("\n### 3. 不可回答拒答案例 (Unanswerable Rejection Case)")
        for s in unans_samples[:2]:
            j = s["judge_evaluation"]
            rej_status = "成功识别并拒答" if j.get("rejection_detected") else "拒答失败(产生幻觉)"
            lines.extend([
                f"- **样本 ID**：`{s['id']}` (unanswerable)",
                f"  - **提问**：{s['query']}",
                f"  - **判定结果**：**{rej_status}**",
                f"  - **判定理由**：{j.get('rejection_reason', 'N/A')}",
            ])

        lines.extend([
            "",
            "---",
            "",
            "## 六、工程局限性与技术面试答辩要点",
            "",
            "1. **同模型自评偏置 (Self-Evaluation Bias)**：",
            "   - 本次评测中被测模型与 Judge 均采用 `deepseek-flash` 家族。理论上存在语言风格趋同与偏好一致性偏差。在面试与工程汇报中，应明确将本次评测定性为“系统内部自动化回归准线”，并指出系统已在代码架构上实现 Judge 协议解耦，可无缝对接 GPT-4o 或 Claude 3.5 Sonnet 作为外部裁判。",
            "2. **本地 CPU 重排耗时占比高**：",
            "   - `Xenova/bge-reranker-base` 部署在本地 CPU（ONNX Runtime），平均单次推理耗时约 1.2~1.5 秒，是除 LLM Prefill 之外的主要耗时源。在工业生产集群中，可通过 GPU Batching、TensorRT 或换用更轻量的轻量化重排模型进一步压降 TTFT。",
            "3. **引文角标校验的双重防线**：",
            "   - 系统实现了“代码层程序化静态校验”与“LLM-as-a-Judge 语义审查”双重校验机制，不仅检查角标编号合法性，更核查内容是否实质支持声明，保证知识可溯源。",
            "",
            "---",
            "",
            "## 七、30 题全量逐题明细审计表",
            "",
            "| 题号 | 分类 | 用户提问 | Corr | Faith | Cit | Rel | Client TTFT | Server TTFT | 评测结论摘要 |",
            "| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
        ])

        for s in samples:
            j = s["judge_evaluation"]
            perf = s["performance"]
            corr = j.get("correctness_score", "-")
            faith = j.get("faithfulness_score", "-")
            cit = j.get("citation_accuracy_score", "-")
            rel = j.get("relevancy_score", "-")
            c_ttft = f"{perf.get('client_ttft_ms', '-')}ms"
            s_ttft = f"{perf.get('server_ttft_ms', '-')}ms"
            if not s["answerable"]:
                rej = "正确拒答" if j.get("rejection_detected") else "未拒答(幻觉)"
                lines.append(f"| `{s['id']}` | {s['category']} | {s['query'][:20]}... | - | - | - | - | {c_ttft} | {s_ttft} | **{rej}** ({j.get('rejection_reason', '')[:28]}...) |")
            else:
                lines.append(f"| `{s['id']}` | {s['category']} | {s['query'][:20]}... | **{corr}** | **{faith}** | **{cit}** | **{rel}** | {c_ttft} | {s_ttft} | {j.get('correctness_reason', '')[:30]}... |")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgentStudio-RAG Generation & Performance Benchmark Runner")
    parser.add_argument("--sample-id", type=str, default=None, help="Target specific sample ID (e.g. GEN-01)")
    parser.add_argument("--sample-ids", type=str, default=None, help="Comma-separated target sample IDs (e.g. GEN-09,GEN-10)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of samples to evaluate")
    parser.add_argument("--mock-judge", action="store_true", help="Run with mock judge for offline unit tests")
    parser.add_argument("--smoke-test", action="store_true", help="Run in strict single-sample smoke test mode (GEN-01)")
    parser.add_argument("--transport", choices=["tcp", "asgi"], default="tcp", help="Transport mode: tcp (real socket) or asgi (in-memory)")
    parser.add_argument("--dataset", type=str, default=None, help="Path to benchmark dataset jsonl (e.g. benchmark_generation_v1_1.jsonl)")
    parser.add_argument("--output-prefix", type=str, default=None, help="Prefix for exported report files")
    args = parser.parse_args()

    custom_dataset = Path(args.dataset) if args.dataset else None
    sample_ids_list = [s.strip() for s in args.sample_ids.split(",") if s.strip()] if args.sample_ids else None
    runner = GenerationBenchmarkRunner(mock_judge=args.mock_judge, dataset_path=custom_dataset)
    asyncio.run(runner.run_benchmark(
        sample_id=args.sample_id,
        sample_ids=sample_ids_list,
        limit=args.limit,
        smoke_test=args.smoke_test,
        transport_mode=args.transport,
        output_prefix=args.output_prefix,
    ))
