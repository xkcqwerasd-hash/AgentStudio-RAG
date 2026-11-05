import json
from typing import Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AgentStudioException
from app.core.logging import logger
from app.core.security import decrypt_api_key, encrypt_api_key
from app.llm.base import BaseLLMClient
from app.llm.openai_client import OpenAILLMClient, get_llm_client
from app.models.model_provider import ModelConfig, ModelProvider


class ModelNotFoundError(AgentStudioException):
    def __init__(self, model_id: str):
        super().__init__(
            status_code=404,
            detail=f"指定的模型配置不存在或已被删除: {model_id}",
        )


class ModelDisabledError(AgentStudioException):
    def __init__(self, message: str):
        super().__init__(
            status_code=400,
            detail=message,
        )


class ToolCallingNotSupportedError(AgentStudioException):
    def __init__(self, display_name: str, model_name: str):
        super().__init__(
            status_code=400,
            detail=(
                f"当前选择的模型 [{display_name} ({model_name})] 不支持原生工具调用 (Tool Calling)。"
                f"深度分析模式需要模型支持自主决策与工具调用能力，请切换到支持 Tool Calling 的模型"
                f"（如 DeepSeek-V3/R1、GPT-4o、Qwen-2.5 等）或使用 [⚡ 快速问答] 模式。"
            ),
        )


async def seed_default_models_if_empty(db: AsyncSession) -> Optional[ModelConfig]:
    """
    Seed default Provider and Model into database if empty, preserving backward compatibility.
    """
    stmt = select(ModelProvider).limit(1)
    res = await db.execute(stmt)
    existing_provider = res.scalar_one_or_none()
    if existing_provider:
        return None

    logger.info("Initializing default ModelProvider and ModelConfig from settings...")
    is_deepseek = "deepseek" in (settings.OPENAI_BASE_URL or "").lower()
    provider_name = "DeepSeek 官方" if is_deepseek else "默认 OpenAI 兼容服务"
    provider_type = "deepseek" if is_deepseek else "openai_compatible"

    encrypted_key = encrypt_api_key(settings.OPENAI_API_KEY) if settings.OPENAI_API_KEY else None

    provider = ModelProvider(
        name=provider_name,
        provider_type=provider_type,
        base_url=settings.OPENAI_BASE_URL or "https://api.deepseek.com",
        api_key_ciphertext=encrypted_key,
        enabled=True,
    )
    db.add(provider)
    await db.flush()

    model_name = settings.LLM_MODEL or "deepseek-chat"
    display_name = "DeepSeek-V3 (内置默认)" if is_deepseek else f"{model_name} (默认)"

    default_caps = {
        "supports_streaming": True,
        "supports_tool_calling": True,
        "supports_vision": False,
        "context_window": settings.MODEL_CONTEXT_WINDOW or 32768,
    }

    model_config = ModelConfig(
        provider_id=provider.id,
        model_name=model_name,
        display_name=display_name,
        model_type="chat",
        capabilities_json=json.dumps(default_caps, ensure_ascii=False),
        is_default=True,
        enabled=True,
    )
    db.add(model_config)
    await db.commit()
    await db.refresh(model_config)
    logger.info(f"Default model configured: {display_name} (ID: {model_config.id})")
    return model_config


async def resolve_llm_client(
    db: AsyncSession,
    model_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Tuple[BaseLLMClient, ModelConfig]:
    """
    Request-level Model Resolution Mechanism (请求级模型解析机制):
    1. If model_id is provided, resolve specific enabled ModelConfig from DB.
    2. Check multi-tenant authorization: private providers (owner_id is not None) can only be accessed by owner.
    3. If model_id is None, resolve system default ModelConfig.
    4. Construct and return an isolated, request-scoped LLM Client.
    5. Ensures multi-user / concurrent requests never contaminate each other's credentials or parameters.
    """
    model_cfg: Optional[ModelConfig] = None

    if model_id:
        stmt = (
            select(ModelConfig)
            .where(ModelConfig.id == model_id)
        )
        res = await db.execute(stmt)
        model_cfg = res.scalar_one_or_none()
        if not model_cfg:
            raise ModelNotFoundError(model_id)
        if not model_cfg.enabled:
            raise ModelDisabledError(f"指定的模型配置 [{model_cfg.display_name}] 已被停用，请选择其他可用模型。")

        # Multi-tenant authorization check: private provider can only be accessed by its owner
        provider = await db.get(ModelProvider, model_cfg.provider_id)
        if provider and provider.owner_id is not None:
            if user_id is not None and provider.owner_id != user_id:
                # Disallow unauthorized cross-tenant model usage
                raise ModelNotFoundError(model_id)
    else:
        # Resolve default model
        stmt = (
            select(ModelConfig)
            .where(ModelConfig.is_default == True, ModelConfig.enabled == True)
            .limit(1)
        )
        res = await db.execute(stmt)
        model_cfg = res.scalar_one_or_none()

        if not model_cfg:
            # Fall back to any enabled model
            fallback_stmt = select(ModelConfig).where(ModelConfig.enabled == True).limit(1)
            fallback_res = await db.execute(fallback_stmt)
            model_cfg = fallback_res.scalar_one_or_none()

        if not model_cfg:
            # Seed default from environment settings
            model_cfg = await seed_default_models_if_empty(db)

    # If still no model in DB (e.g. in test doubles without committing), create mock virtual ModelConfig
    if not model_cfg:
        virtual_provider = ModelProvider(
            id="virtual-default-provider",
            name="Virtual Default",
            provider_type="openai_compatible",
            base_url=settings.OPENAI_BASE_URL,
            api_key_ciphertext=None,
            enabled=True,
        )
        model_cfg = ModelConfig(
            id="virtual-default-model",
            provider_id=virtual_provider.id,
            model_name=settings.LLM_MODEL,
            display_name=f"{settings.LLM_MODEL} (环境变量)",
            model_type="chat",
            capabilities_json=json.dumps({
                "supports_streaming": True,
                "supports_tool_calling": True,
                "supports_vision": False,
                "context_window": settings.MODEL_CONTEXT_WINDOW,
            }),
            is_default=True,
            enabled=True,
        )
        model_cfg.provider = virtual_provider

    provider = model_cfg.provider
    if provider and not provider.enabled:
        raise ModelDisabledError(f"模型所属服务商 [{provider.name}] 已被停用，请先启用该服务商。")

    # In CI/test environment using test_double, return the test double singleton
    if settings.LLM_PROVIDER == "test_double":
        td_client = get_llm_client("test_double")
        if hasattr(td_client, "model"):
            td_client.model = model_cfg.model_name
        return td_client, model_cfg

    # Resolve decrypted API key
    plain_api_key = None
    if provider and provider.api_key_ciphertext:
        try:
            plain_api_key = decrypt_api_key(provider.api_key_ciphertext)
        except Exception as exc:
            logger.error(f"Failed to decrypt API key for provider {provider.name}: {exc}")
            raise ModelDisabledError(f"服务商 [{provider.name}] 的 API Key 解密失败，请重新配置密钥。") from exc
    elif not provider or not provider.api_key_ciphertext:
        # For Ollama / local services without key, or fallback to settings
        if provider and provider.provider_type in ("ollama", "vllm"):
            plain_api_key = ""
        else:
            plain_api_key = settings.OPENAI_API_KEY

    base_url = (provider.base_url if provider else settings.OPENAI_BASE_URL)

    # Create request-isolated LLM client
    client = OpenAILLMClient(
        api_key=plain_api_key,
        base_url=base_url,
        model=model_cfg.model_name,
    )
    return client, model_cfg


def validate_tool_calling_capability(model_config: ModelConfig) -> None:
    """
    Validate whether the model supports native Tool Calling.
    Raises ToolCallingNotSupportedError if unsupported, preventing silent failures.
    """
    caps = model_config.capabilities
    if not caps.get("supports_tool_calling", True):
        raise ToolCallingNotSupportedError(
            display_name=model_config.display_name,
            model_name=model_config.model_name,
        )
