import asyncio
import hashlib
from typing import List, Optional
import httpx
import jieba
import numpy as np

from app.core.config import settings
from app.core.exceptions import EmbeddingServiceError
from app.core.logging import logger
from app.core.ssrf import SSRFBlockedException, get_ssrf_safe_async_client
from app.llm.base import BaseEmbeddingClient

# Suppress jieba log output
jieba.setLogLevel(jieba.logging.INFO)

# Global cache for local BGE models to avoid repeated weight initialization
_LOCAL_BGE_MODELS: dict = {}


def validate_vector(
    vec: List[float],
    expected_dim: int,
    provider_name: str,
    model_name: str,
) -> List[float]:
    """
    Validate that a generated vector embedding meets strict quality & consistency constraints:
    1. Dimension matches expected dimension
    2. Vector does not contain NaN or Inf values
    """
    if len(vec) != expected_dim:
        raise EmbeddingServiceError(
            f"Vector dimension mismatch from provider '{provider_name}' (model '{model_name}'): "
            f"expected {expected_dim}, got {len(vec)}"
        )

    arr = np.array(vec, dtype=np.float32)
    if np.isnan(arr).any():
        raise EmbeddingServiceError(
            f"Embedding vector from provider '{provider_name}' (model '{model_name}') contains NaN values"
        )
    if np.isinf(arr).any():
        raise EmbeddingServiceError(
            f"Embedding vector from provider '{provider_name}' (model '{model_name}') contains Inf values"
        )

    return vec


class TestDoubleEmbeddingClient(BaseEmbeddingClient):
    """
    Deterministic test double embedding client.
    Uses jieba word tokenization and character n-gram hashing projected into normalized dimension space.
    Texts with similar tokens or concepts will have high cosine similarity.
    """
    __test__ = False

    def __init__(self, dimension: int = 1024):
        self._dimension = dimension
        self._provider_name = "test_double"
        self._model_name = "hash-projection-v1"

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimension

    def _text_to_vector(self, text: str) -> List[float]:
        vec = np.zeros(self._dimension, dtype=np.float32)
        cleaned = text.strip()
        if not cleaned:
            vec[0] = 1.0
            return vec.tolist()

        # Extract words using jieba for Chinese and whitespace split for English
        words = [w.strip() for w in jieba.lcut(cleaned) if w.strip()]
        tokens = list(words)

        # Character unigrams and bigrams
        chars = list(cleaned)
        tokens.extend(chars)
        for i in range(len(chars) - 1):
            tokens.append(chars[i] + chars[i + 1])

        for token in tokens:
            h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
            idx = h % self._dimension
            sign = 1.0 if ((h >> 16) & 1) == 0 else -1.0
            vec[idx] += sign

        norm = np.linalg.norm(vec)
        if norm > 1e-9:
            vec = vec / norm
        else:
            vec[0] = 1.0

        res = vec.tolist()
        return validate_vector(res, self._dimension, self._provider_name, self._model_name)

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._text_to_vector(t) for t in texts]

    async def embed_query(self, text: str) -> List[float]:
        return self._text_to_vector(text)


class LocalBGEEmbeddingClient(BaseEmbeddingClient):
    """
    Local ONNX-accelerated BGE embedding client using fastembed.
    Produces high-quality semantic embeddings locally on CPU without external API dependencies.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        dimension: Optional[int] = None,
        batch_size: Optional[int] = None,
    ):
        self._model_name = model_name or settings.EMBEDDING_MODEL_NAME
        self._dimension = dimension or settings.EMBEDDING_DIMENSIONS
        self._batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        self._provider_name = "local_bge"
        self._embedder = self._get_or_create_model(self._model_name)

    @classmethod
    def _get_or_create_model(cls, model_name: str):
        global _LOCAL_BGE_MODELS
        if model_name not in _LOCAL_BGE_MODELS:
            try:
                from fastembed import TextEmbedding
                logger.info(f"Loading local BGE embedding model '{model_name}'...")
                _LOCAL_BGE_MODELS[model_name] = TextEmbedding(model_name=model_name)
                logger.info(f"Local BGE embedding model '{model_name}' initialized successfully.")
            except Exception as exc:
                raise EmbeddingServiceError(
                    f"Failed to load local BGE model '{model_name}': {exc}"
                ) from exc
        return _LOCAL_BGE_MODELS[model_name]

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimension

    def _sync_embed(self, texts: List[str]) -> List[List[float]]:
        # fastembed expects non-empty string iterable
        # For completely empty texts, replace with single space to avoid tokenizer crash
        safe_texts = [t if t.strip() else " " for t in texts]
        raw_vecs = list(self._embedder.embed(safe_texts, batch_size=self._batch_size))
        output: List[List[float]] = []
        for v in raw_vecs:
            vec_list = v.tolist()
            output.append(
                validate_vector(vec_list, self._dimension, self._provider_name, self._model_name)
            )
        return output

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            return await asyncio.to_thread(self._sync_embed, texts)
        except EmbeddingServiceError:
            raise
        except Exception as exc:
            raise EmbeddingServiceError(
                f"Local BGE embedding failed on model '{self._model_name}': {exc}"
            ) from exc

    async def embed_query(self, text: str) -> List[float]:
        results = await self.embed_documents([text])
        if not results:
            raise EmbeddingServiceError("Local BGE returned empty embedding for query")
        return results[0]


class OpenAIEmbeddingClient(BaseEmbeddingClient):
    """
    Standard OpenAI-compatible Embedding API client.
    Supports SiliconFlow, DashScope, OpenAI, Ollama, etc.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        dimension: Optional[int] = None,
        batch_size: Optional[int] = None,
    ):
        self._api_key = api_key or settings.EMBEDDING_API_KEY
        self._base_url = (base_url or settings.EMBEDDING_BASE_URL).rstrip("/")
        self._model = model or settings.EMBEDDING_MODEL_NAME
        self._dimension = dimension or settings.EMBEDDING_DIMENSIONS
        self._batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        self._provider_name = "openai"

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimension

    # Backwards compatibility properties
    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def model(self) -> str:
        return self._model

    async def _embed_batch(self, batch_texts: List[str]) -> List[List[float]]:
        if not self._api_key:
            raise EmbeddingServiceError(
                "Embedding API key is not configured for OpenAI-compatible provider. "
                "Please set EMBEDDING_API_KEY or switch EMBEDDING_PROVIDER to 'local_bge'."
            )

        url = f"{self._base_url}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        payload = {
            "model": self._model,
            "input": batch_texts,
        }

        try:
            async with get_ssrf_safe_async_client(
                timeout=30.0,
                allow_local=(settings.DEPLOYMENT_MODE == "local"),
                trusted_hosts=settings.trusted_internal_hosts_set,
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    # Never log raw headers or full API key
                    safe_url = self._base_url
                    raise EmbeddingServiceError(
                        f"Embedding service endpoint '{safe_url}/embeddings' returned HTTP {resp.status_code}: {resp.text}"
                    )
                data = resp.json()
                items = sorted(data["data"], key=lambda x: x["index"])
                vecs = [item["embedding"] for item in items]
                for v in vecs:
                    validate_vector(v, self._dimension, self._provider_name, self._model)
                return vecs
        except SSRFBlockedException as exc:
            raise EmbeddingServiceError(
                f"Embedding request blocked by security policy (SSRF): {exc.detail}"
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise EmbeddingServiceError(
                f"Embedding service connection failed to '{self._base_url}': {exc}"
            ) from exc
        except EmbeddingServiceError:
            raise
        except Exception as exc:
            raise EmbeddingServiceError(
                f"Embedding API unexpected error on model '{self._model}': {exc}"
            ) from exc

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        all_vectors: List[List[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            batch_vectors = await self._embed_batch(batch)
            all_vectors.extend(batch_vectors)

        return all_vectors

    async def embed_query(self, text: str) -> List[float]:
        results = await self.embed_documents([text])
        if not results:
            raise EmbeddingServiceError("OpenAI embedding service returned empty embedding for query")
        return results[0]


def get_embedding_client(
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    dimension: Optional[int] = None,
) -> BaseEmbeddingClient:
    p = provider or settings.EMBEDDING_PROVIDER
    if p == "local_bge":
        return LocalBGEEmbeddingClient(model_name=model_name, dimension=dimension)
    elif p == "openai":
        return OpenAIEmbeddingClient(model=model_name, dimension=dimension)
    elif p == "test_double":
        dim = dimension or 1024
        return TestDoubleEmbeddingClient(dimension=dim)
    else:
        raise ValueError(
            f"Unknown embedding provider: '{p}'. Supported providers: 'local_bge', 'openai', 'test_double'"
        )
