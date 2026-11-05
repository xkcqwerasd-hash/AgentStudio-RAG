import time
from typing import Any, Dict, List, Optional
import httpx

from app.core.logging import logger
from app.schemas.model_provider import (
    ModelTestResponse,
    OllamaModelItem,
    OllamaProbeResponse,
    VLLMProbeResponse,
)


async def test_model_connectivity(
    base_url: str,
    api_key: Optional[str],
    model_name: str,
    timeout: float = 15.0,
) -> ModelTestResponse:
    """
    Test real connectivity and basic inference capability for an OpenAI-compatible endpoint.
    Sends a minimal completion request to verify base_url, api_key, and model existence.
    """
    clean_base = base_url.strip().rstrip("/")
    # If base_url already ends with /v1 or not, we append /chat/completions appropriately
    if not clean_base.endswith("/chat/completions"):
        url = f"{clean_base}/chat/completions"
    else:
        url = clean_base

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    payload = {
        "model": model_name.strip(),
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
        "temperature": 0.1,
    }

    start_t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            latency_ms = round((time.perf_counter() - start_t) * 1000, 2)

            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                snippet = ""
                if choices:
                    snippet = choices[0].get("message", {}).get("content", "")
                return ModelTestResponse(
                    success=True,
                    latency_ms=latency_ms,
                    reply_snippet=snippet.strip() or "连接成功 (空回复)",
                )
            elif resp.status_code == 401:
                return ModelTestResponse(
                    success=False,
                    latency_ms=latency_ms,
                    error="鉴权失败 (HTTP 401)：API Key 无效或未授权，请检查填写的密钥。",
                )
            elif resp.status_code == 404:
                return ModelTestResponse(
                    success=False,
                    latency_ms=latency_ms,
                    error=f"未找到资源 (HTTP 404)：模型 '{model_name}' 不存在或 Base URL 路径不匹配。",
                )
            elif resp.status_code == 429:
                return ModelTestResponse(
                    success=False,
                    latency_ms=latency_ms,
                    error="请求过于频繁或额度已用尽 (HTTP 429)：请检查 API 服务商额度与并发限制。",
                )
            else:
                err_text = resp.text[:200]
                return ModelTestResponse(
                    success=False,
                    latency_ms=latency_ms,
                    error=f"服务返回 HTTP {resp.status_code}: {err_text}",
                )
    except httpx.ConnectError as exc:
        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        return ModelTestResponse(
            success=False,
            latency_ms=latency_ms,
            error=f"无法连接到服务 ({clean_base})：请确认本地/远程服务已启动且网络通畅。",
        )
    except httpx.TimeoutException:
        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        return ModelTestResponse(
            success=False,
            latency_ms=latency_ms,
            error=f"连接超时 (超过 {timeout}s)：服务响应过慢或网络未连通。",
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        return ModelTestResponse(
            success=False,
            latency_ms=latency_ms,
            error=f"连接异常: {str(exc)}",
        )


async def probe_ollama_models(
    base_url: str = "http://localhost:11434",
    timeout: float = 10.0,
) -> OllamaProbeResponse:
    """
    Probe downloaded model tags from an active Ollama instance via /api/tags.
    """
    clean_base = base_url.strip().rstrip("/")
    url = f"{clean_base}/api/tags"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return OllamaProbeResponse(
                    success=False,
                    error=f"Ollama 服务返回 HTTP {resp.status_code}: {resp.text[:150]}",
                )
            data = resp.json()
            raw_models = data.get("models", [])
            items: List[OllamaModelItem] = []
            for m in raw_models:
                details = m.get("details", {})
                items.append(
                    OllamaModelItem(
                        name=m.get("name", ""),
                        size=m.get("size"),
                        modified_at=m.get("modified_at"),
                        family=details.get("family"),
                        parameter_size=details.get("parameter_size"),
                        quantization_level=details.get("quantization_level"),
                    )
                )
            return OllamaProbeResponse(success=True, models=items)
    except httpx.ConnectError:
        return OllamaProbeResponse(
            success=False,
            error=f"无法连接到 Ollama 服务 ({clean_base})，请确认 Ollama 进程已在本地运行。",
        )
    except httpx.TimeoutException:
        return OllamaProbeResponse(
            success=False,
            error=f"探测 Ollama 服务超时 ({timeout}s)，请检查服务状态。",
        )
    except Exception as exc:
        return OllamaProbeResponse(success=False, error=f"探测异常: {str(exc)}")


async def probe_vllm_models(
    base_url: str = "http://localhost:8000/v1",
    api_key: Optional[str] = None,
    timeout: float = 10.0,
) -> VLLMProbeResponse:
    """
    Probe available models on a vLLM or OpenAI-compatible server via /v1/models.
    """
    clean_base = base_url.strip().rstrip("/")
    if not clean_base.endswith("/models"):
        if clean_base.endswith("/v1"):
            url = f"{clean_base}/models"
        else:
            url = f"{clean_base}/v1/models"
    else:
        url = clean_base

    headers: Dict[str, str] = {}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                # Try fallback without /v1
                fallback_url = f"{clean_base.rstrip('/v1')}/models"
                resp = await client.get(fallback_url, headers=headers)
                if resp.status_code != 200:
                    return VLLMProbeResponse(
                        success=False,
                        error=f"vLLM 服务返回 HTTP {resp.status_code}: {resp.text[:150]}",
                    )
            data = resp.json()
            raw_models = data.get("data", [])
            model_names = [m.get("id") for m in raw_models if m.get("id")]
            return VLLMProbeResponse(success=True, models=model_names)
    except httpx.ConnectError:
        return VLLMProbeResponse(
            success=False,
            error=f"无法连接到 vLLM 服务 ({clean_base})，请确认服务已启动且网络通畅。",
        )
    except httpx.TimeoutException:
        return VLLMProbeResponse(
            success=False,
            error=f"探测 vLLM 服务超时 ({timeout}s)。",
        )
    except Exception as exc:
        return VLLMProbeResponse(success=False, error=f"探测异常: {str(exc)}")
