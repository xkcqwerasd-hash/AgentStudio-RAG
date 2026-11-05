from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import asyncio
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.core.config import settings
from app.core.logging import logger
from app.retrieval.vector_store import ScoredChunk


def compute_stable_sigmoid(logit: float) -> float:
    """
    Numerically stable Sigmoid mapping f(x) = 1 / (1 + exp(-x)) -> [0.0, 1.0].
    Clamps exponent input defensively to [-50.0, 50.0] to prevent OverflowError
    when logit < -709, without mutating the caller's raw logit.
    For logit >= 50.0: returns 1.0.
    For logit <= -50.0: returns 0.0.
    Finite floats in [0.0, 1.0] are guaranteed for all inputs from -inf to +inf.
    Note: Sigmoid squashing maps real logits to a monotonic [0, 1] range and
    does NOT represent statistically calibrated probability.
    """
    if math.isnan(logit):
        return 0.0
    if logit >= 50.0:
        return 1.0
    if logit <= -50.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-logit))


@dataclass
class RerankResult:
    chunks: List[ScoredChunk]
    duration_ms: float
    candidates_in: int
    candidates_out: int
    model_name: str
    degraded: bool = False
    degradation_reason: Optional[str] = None


class BaseRerankerClient(ABC):
    @property
    @abstractmethod
    def model_name(self) -> str:
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: List[ScoredChunk],
        top_k: Optional[int] = None,
    ) -> List[ScoredChunk]:
        pass


class LocalONNXReranker(BaseRerankerClient):
    """
    High-performance, CPU-optimized Cross-Encoder Reranker using INT8 Quantized ONNX Runtime
    and HuggingFace Tokenizers.
    Model: Xenova/bge-reranker-base (278M parameters, INT8 quantized to 279 MB).
    """

    def __init__(
        self,
        model_name: str = "Xenova/bge-reranker-base",
        batch_size: int = 16,
        intra_threads: int = 4,
    ):
        self._model_name = model_name
        self.batch_size = batch_size
        self.intra_threads = intra_threads
        self._lock = asyncio.Lock()
        self._session = None
        self._tokenizer = None
        self._init_model()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str:
        return "local_onnx"

    def _init_model(self):
        from huggingface_hub import hf_hub_download
        import onnxruntime as ort
        from tokenizers import Tokenizer

        logger.info(f"Loading local Cross-Encoder ONNX model: {self._model_name}...")
        t0 = time.perf_counter()

        # Download or resolve cached tokenizer and quantized onnx model
        tok_path = hf_hub_download(repo_id=self._model_name, filename="tokenizer.json")
        try:
            onnx_path = hf_hub_download(repo_id=self._model_name, filename="onnx/model_quantized.onnx")
        except Exception:
            onnx_path = hf_hub_download(repo_id=self._model_name, filename="onnx/model.onnx")

        tokenizer = Tokenizer.from_file(tok_path)
        tokenizer.enable_padding(pad_id=0, pad_token="<pad>")
        tokenizer.enable_truncation(max_length=512)
        self._tokenizer = tokenizer

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = self.intra_threads
        sess_opts.inter_op_num_threads = 1
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            onnx_path,
            sess_opts,
            providers=["CPUExecutionProvider"],
        )
        dt = (time.perf_counter() - t0) * 1000
        logger.info(f"Cross-Encoder ONNX model loaded successfully in {dt:.2f} ms.")

    def _inference_batch(self, pairs: List[Tuple[str, str]]) -> List[float]:
        """Synchronous batch inference on CPU."""
        encoded = self._tokenizer.encode_batch(pairs)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        input_names = [inp.name for inp in self._session.get_inputs()]
        inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in input_names:
            inputs["token_type_ids"] = np.array([e.type_ids for e in encoded], dtype=np.int64)

        outputs = self._session.run(None, inputs)
        logits = outputs[0].flatten().tolist()
        return logits

    async def rerank(
        self,
        query: str,
        candidates: List[ScoredChunk],
        top_k: Optional[int] = None,
    ) -> List[ScoredChunk]:
        if not candidates or not query.strip():
            return candidates

        pairs = [(query, c.content) for c in candidates]
        all_logits: List[float] = []

        # Thread-safe batch execution
        async with self._lock:
            for i in range(0, len(pairs), self.batch_size):
                batch_pairs = pairs[i : i + self.batch_size]
                logits = await asyncio.to_thread(self._inference_batch, batch_pairs)
                all_logits.extend(logits)

        # Build reranked ScoredChunk list with normalized sigmoid relevance scores
        scored_candidates: List[ScoredChunk] = []
        for orig, logit in zip(candidates, all_logits):
            # Monotonic Sigmoid score in [0.0, 1.0] (note: not statistically calibrated probability)
            sig_score = compute_stable_sigmoid(logit)
            normalized_score = round(float(sig_score), 6)
            raw = round(float(logit), 6)

            scored = ScoredChunk(
                chunk_id=orig.chunk_id,
                document_id=orig.document_id,
                document_name=orig.document_name,
                page_number=orig.page_number,
                chunk_index=orig.chunk_index,
                content=orig.content,
                similarity_score=normalized_score,
                dense_rank=orig.dense_rank,
                bm25_rank=orig.bm25_rank,
                dense_score=orig.dense_score,
                bm25_score=orig.bm25_score,
                fusion_score=orig.fusion_score,
                reranker_score=normalized_score,
                reranker_raw_score=raw,
            )
            scored_candidates.append(scored)

        # Sort descending primarily by raw logit score (reranker_raw_score) to avoid saturation ties,
        # followed by fusion_score and chunk_id as deterministic tie-breakers
        scored_candidates.sort(
            key=lambda x: (
                -(x.reranker_raw_score if x.reranker_raw_score is not None else -1e9),
                -(x.fusion_score if x.fusion_score is not None else -1e9),
                x.chunk_id,
            )
        )

        # Assign reranker_rank (1-based)
        for idx, item in enumerate(scored_candidates, start=1):
            item.reranker_rank = idx

        if top_k is not None:
            return scored_candidates[:top_k]
        return scored_candidates


class TestDoubleReranker(BaseRerankerClient):
    """
    Deterministic offline test double for Cross-Encoder Reranker.
    Computes exact lexical and technical token overlap + RRF prior without neural weights.
    Latency: ~0.1 ms.
    """
    __test__ = False

    def __init__(self, model_name: str = "test_double_reranker"):
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str:
        return "test_double"

    async def rerank(
        self,
        query: str,
        candidates: List[ScoredChunk],
        top_k: Optional[int] = None,
    ) -> List[ScoredChunk]:
        if not candidates or not query.strip():
            return candidates

        q_lower = query.lower()
        q_tokens = set(q_lower.split())

        scored_candidates: List[ScoredChunk] = []
        for orig in candidates:
            c_lower = orig.content.lower()
            # Technical keywords exact matches
            exact_hits = sum(1 for kw in ["5000", "超时", "jwt", "redis", "half_open", "open", "closed", "min_idle", "modelhealthstore"] if kw in q_lower and kw in c_lower)
            # Token overlap
            c_tokens = set(c_lower.split())
            overlap = len(q_tokens & c_tokens)
            # Prior from fusion score
            fusion_prior = (orig.fusion_score or 0.0) * 10.0

            raw_logit = float(exact_hits * 3.0 + overlap * 1.5 + fusion_prior)
            normalized_score = round(compute_stable_sigmoid(raw_logit), 6)

            scored = ScoredChunk(
                chunk_id=orig.chunk_id,
                document_id=orig.document_id,
                document_name=orig.document_name,
                page_number=orig.page_number,
                chunk_index=orig.chunk_index,
                content=orig.content,
                similarity_score=normalized_score,
                dense_rank=orig.dense_rank,
                bm25_rank=orig.bm25_rank,
                dense_score=orig.dense_score,
                bm25_score=orig.bm25_score,
                fusion_score=orig.fusion_score,
                reranker_score=normalized_score,
                reranker_raw_score=round(raw_logit, 4),
            )
            scored_candidates.append(scored)

        # Sort descending primarily by raw logit score (reranker_raw_score) to avoid saturation ties
        scored_candidates.sort(
            key=lambda x: (
                -(x.reranker_raw_score if x.reranker_raw_score is not None else -1e9),
                -(x.fusion_score if x.fusion_score is not None else -1e9),
                x.chunk_id,
            )
        )

        for idx, item in enumerate(scored_candidates, start=1):
            item.reranker_rank = idx

        if top_k is not None:
            return scored_candidates[:top_k]
        return scored_candidates


# Cached client instances
_reranker_instances: Dict[str, BaseRerankerClient] = {}


def get_reranker_client(provider: Optional[str] = None) -> BaseRerankerClient:
    """
    Factory function returning a singleton Cross-Encoder client.
    Guarantees the model is loaded once at startup and reused.
    """
    prov = provider or settings.RERANKER_PROVIDER
    if prov in _reranker_instances:
        return _reranker_instances[prov]

    if prov == "test_double":
        client = TestDoubleReranker(model_name=settings.RERANKER_MODEL_NAME)
    elif prov == "local_onnx":
        client = LocalONNXReranker(
            model_name=settings.RERANKER_MODEL_NAME,
            batch_size=settings.RERANKER_BATCH_SIZE,
        )
    else:
        logger.warning(f"Unknown Reranker provider '{prov}', defaulting to TestDoubleReranker.")
        client = TestDoubleReranker()

    _reranker_instances[prov] = client
    return client


class CrossEncoderReranker:
    """
    High-level orchestrator for Cross-Encoder Reranking:
    - Handles timeouts via asyncio.wait_for
    - Fallback degradation on error/timeout back to RRF ranking
    - Emits execution metrics and degradation status
    """

    def __init__(self, client: Optional[BaseRerankerClient] = None):
        self.client = client or get_reranker_client()

    async def rerank(
        self,
        query: str,
        candidates: List[ScoredChunk],
        top_k: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> RerankResult:
        start_time = time.perf_counter()
        k_val = top_k if top_k is not None else settings.RERANKER_TOP_K
        t_limit = timeout if timeout is not None else settings.RERANKER_TIMEOUT

        if not candidates or not query.strip():
            return RerankResult(
                chunks=[],
                duration_ms=0.0,
                candidates_in=0,
                candidates_out=0,
                model_name=self.client.model_name,
                degraded=False,
            )

        if not settings.RERANKER_ENABLED:
            logger.info("Cross-Encoder Reranker disabled in settings, passing RRF candidates directly.")
            dt = (time.perf_counter() - start_time) * 1000
            return RerankResult(
                chunks=candidates[:k_val],
                duration_ms=round(dt, 2),
                candidates_in=len(candidates),
                candidates_out=len(candidates[:k_val]),
                model_name="disabled",
                degraded=False,
            )

        try:
            reranked = await asyncio.wait_for(
                self.client.rerank(query=query, candidates=candidates, top_k=k_val),
                timeout=t_limit,
            )
            duration_ms = (time.perf_counter() - start_time) * 1000
            return RerankResult(
                chunks=reranked,
                duration_ms=round(duration_ms, 2),
                candidates_in=len(candidates),
                candidates_out=len(reranked),
                model_name=self.client.model_name,
                degraded=False,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.warning(f"Cross-Encoder reranking timed out after {t_limit}s, falling back to RRF ranking.")
            # Fallback: slice original candidates
            fallback_chunks = candidates[:k_val]
            return RerankResult(
                chunks=fallback_chunks,
                duration_ms=round(duration_ms, 2),
                candidates_in=len(candidates),
                candidates_out=len(fallback_chunks),
                model_name=self.client.model_name,
                degraded=True,
                degradation_reason="timeout",
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"Cross-Encoder reranking encountered error: {exc}, falling back to RRF ranking.")
            fallback_chunks = candidates[:k_val]
            return RerankResult(
                chunks=fallback_chunks,
                duration_ms=round(duration_ms, 2),
                candidates_in=len(candidates),
                candidates_out=len(fallback_chunks),
                model_name=self.client.model_name,
                degraded=True,
                degradation_reason=str(exc),
            )
