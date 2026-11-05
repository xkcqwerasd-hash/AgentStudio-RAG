import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db
from app.core.auth import get_current_user
from app.core.exceptions import AgentStudioException
from app.core.logging import logger
from app.core.security import decrypt_api_key, encrypt_api_key, mask_api_key
from app.llm.factory import seed_default_models_if_empty
from app.llm.manager import probe_ollama_models, probe_vllm_models, test_model_connectivity
from app.models.model_provider import ModelConfig, ModelProvider
from app.models.user import User
from app.schemas.model_provider import (
    ActiveModelItem,
    ModelCapabilities,
    ModelConfigCreate,
    ModelConfigResponse,
    ModelConfigUpdate,
    ModelProviderCreate,
    ModelProviderResponse,
    ModelProviderUpdate,
    ModelTestRequest,
    ModelTestResponse,
    OllamaProbeRequest,
    OllamaProbeResponse,
    VLLMProbeRequest,
    VLLMProbeResponse,
)

router = APIRouter(prefix="/models", tags=["Model Management"])


def _to_provider_response(provider: ModelProvider) -> ModelProviderResponse:
    """Helper to convert ModelProvider ORM object to ModelProviderResponse with masked API key."""
    decrypted_key = None
    if provider.api_key_ciphertext:
        try:
            decrypted_key = decrypt_api_key(provider.api_key_ciphertext)
        except Exception:
            pass

    models_res: List[ModelConfigResponse] = []
    for m in (provider.models or []):
        caps_dict = m.capabilities
        models_res.append(
            ModelConfigResponse(
                id=m.id,
                provider_id=m.provider_id,
                model_name=m.model_name,
                display_name=m.display_name,
                model_type=m.model_type,
                capabilities=ModelCapabilities(**caps_dict),
                is_default=m.is_default,
                enabled=m.enabled,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
        )

    return ModelProviderResponse(
        id=provider.id,
        name=provider.name,
        provider_type=provider.provider_type,
        base_url=provider.base_url,
        has_api_key=bool(provider.api_key_ciphertext),
        masked_api_key=mask_api_key(decrypted_key) if decrypted_key else None,
        enabled=provider.enabled,
        owner_id=provider.owner_id,
        models=models_res,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


@router.get("/providers", response_model=List[ModelProviderResponse])
async def list_providers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all model providers and their configured models accessible to the user."""
    # Ensure default is seeded if database has no providers yet
    await seed_default_models_if_empty(db)

    if current_user.role == "admin":
        stmt = select(ModelProvider).order_by(ModelProvider.created_at.asc())
    else:
        stmt = (
            select(ModelProvider)
            .where((ModelProvider.owner_id.is_(None)) | (ModelProvider.owner_id == current_user.id))
            .order_by(ModelProvider.created_at.asc())
        )
    res = await db.execute(stmt)
    providers = res.scalars().all()
    return [_to_provider_response(p) for p in providers]


@router.post("/providers", response_model=ModelProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ModelProviderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new model provider and optional initial models."""
    if payload.is_public and current_user.role == "admin":
        target_owner_id = None
    else:
        target_owner_id = current_user.id

    encrypted_key = encrypt_api_key(payload.api_key) if payload.api_key else None
    provider = ModelProvider(
        name=payload.name.strip(),
        provider_type=payload.provider_type.strip(),
        base_url=payload.base_url.strip().rstrip("/"),
        api_key_ciphertext=encrypted_key,
        enabled=payload.enabled,
        owner_id=target_owner_id,
    )
    db.add(provider)
    await db.flush()

    if payload.models:
        for m in payload.models:
            if m.is_default:
                # Clear other defaults
                await db.execute(update(ModelConfig).values(is_default=False))

            caps = m.capabilities.model_dump() if m.capabilities else {
                "supports_streaming": True,
                "supports_tool_calling": True,
                "supports_vision": False,
                "context_window": 32768,
            }
            cfg = ModelConfig(
                provider_id=provider.id,
                model_name=m.model_name.strip(),
                display_name=m.display_name.strip(),
                model_type=m.model_type.strip(),
                capabilities_json=json.dumps(caps, ensure_ascii=False),
                is_default=m.is_default,
                enabled=m.enabled,
            )
            db.add(cfg)

    await db.commit()
    await db.refresh(provider)
    return _to_provider_response(provider)


@router.get("/providers/{provider_id}", response_model=ModelProviderResponse)
async def get_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get detail of a model provider."""
    stmt = select(ModelProvider).where(ModelProvider.id == provider_id)
    res = await db.execute(stmt)
    provider = res.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="服务商不存在")
    if provider.owner_id is not None and provider.owner_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=404, detail="服务商不存在")
    return _to_provider_response(provider)


@router.put("/providers/{provider_id}", response_model=ModelProviderResponse)
async def update_provider(
    provider_id: str,
    payload: ModelProviderUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a model provider's configuration or credentials."""
    stmt = select(ModelProvider).where(ModelProvider.id == provider_id)
    res = await db.execute(stmt)
    provider = res.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="服务商不存在")

    if provider.owner_id is None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以修改系统全局模型服务商")
    elif provider.owner_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=404, detail="服务商不存在")

    if payload.name is not None:
        provider.name = payload.name.strip()
    if payload.provider_type is not None:
        provider.provider_type = payload.provider_type.strip()
    if payload.base_url is not None:
        provider.base_url = payload.base_url.strip().rstrip("/")
    if payload.enabled is not None:
        provider.enabled = payload.enabled

    # API Key update semantics:
    # None -> keep untouched
    # "" -> clear key
    # non-empty string -> encrypt & update
    if payload.api_key is not None:
        if payload.api_key.strip():
            provider.api_key_ciphertext = encrypt_api_key(payload.api_key)
        else:
            provider.api_key_ciphertext = None

    await db.commit()
    await db.refresh(provider)
    return _to_provider_response(provider)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a model provider and its associated models."""
    stmt = select(ModelProvider).where(ModelProvider.id == provider_id)
    res = await db.execute(stmt)
    provider = res.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="服务商不存在")

    if provider.owner_id is None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以删除系统全局模型服务商")
    elif provider.owner_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=404, detail="服务商不存在")

    await db.delete(provider)
    await db.commit()


@router.post("/providers/{provider_id}/models", response_model=ModelConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_model_config(
    provider_id: str,
    payload: ModelConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a model configuration to a provider."""
    stmt = select(ModelProvider).where(ModelProvider.id == provider_id)
    res = await db.execute(stmt)
    provider = res.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="服务商不存在")

    if provider.owner_id is None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以为系统全局服务商添加模型")
    elif provider.owner_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=404, detail="服务商不存在")

    if payload.is_default:
        await db.execute(update(ModelConfig).values(is_default=False))

    caps = payload.capabilities.model_dump() if payload.capabilities else {
        "supports_streaming": True,
        "supports_tool_calling": True,
        "supports_vision": False,
        "context_window": 32768,
    }

    cfg = ModelConfig(
        provider_id=provider.id,
        model_name=payload.model_name.strip(),
        display_name=payload.display_name.strip(),
        model_type=payload.model_type.strip(),
        capabilities_json=json.dumps(caps, ensure_ascii=False),
        is_default=payload.is_default,
        enabled=payload.enabled,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    return ModelConfigResponse(
        id=cfg.id,
        provider_id=cfg.provider_id,
        model_name=cfg.model_name,
        display_name=cfg.display_name,
        model_type=cfg.model_type,
        capabilities=ModelCapabilities(**cfg.capabilities),
        is_default=cfg.is_default,
        enabled=cfg.enabled,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


@router.put("/configs/{model_id}", response_model=ModelConfigResponse)
async def update_model_config(
    model_id: str,
    payload: ModelConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a model's settings, display name, or capabilities."""
    stmt = select(ModelConfig).options(selectinload(ModelConfig.provider)).where(ModelConfig.id == model_id)
    res = await db.execute(stmt)
    cfg = res.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="模型配置不存在")

    prov = cfg.provider
    if prov and prov.owner_id is None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以修改系统全局模型配置")
    elif prov and prov.owner_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=404, detail="模型配置不存在")

    if payload.display_name is not None:
        cfg.display_name = payload.display_name.strip()
    if payload.model_type is not None:
        cfg.model_type = payload.model_type.strip()
    if payload.enabled is not None:
        cfg.enabled = payload.enabled
    if payload.capabilities is not None:
        cfg.capabilities = payload.capabilities.model_dump()

    if payload.is_default is True:
        await db.execute(update(ModelConfig).values(is_default=False))
        cfg.is_default = True
    elif payload.is_default is False:
        cfg.is_default = False

    await db.commit()
    await db.refresh(cfg)

    return ModelConfigResponse(
        id=cfg.id,
        provider_id=cfg.provider_id,
        model_name=cfg.model_name,
        display_name=cfg.display_name,
        model_type=cfg.model_type,
        capabilities=ModelCapabilities(**cfg.capabilities),
        is_default=cfg.is_default,
        enabled=cfg.enabled,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


@router.delete("/configs/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_config(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a model configuration."""
    stmt = select(ModelConfig).options(selectinload(ModelConfig.provider)).where(ModelConfig.id == model_id)
    res = await db.execute(stmt)
    cfg = res.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="模型配置不存在")

    prov = cfg.provider
    if prov and prov.owner_id is None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以删除系统全局模型配置")
    elif prov and prov.owner_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=404, detail="模型配置不存在")

    await db.delete(cfg)
    await db.commit()


@router.post("/configs/{model_id}/set-default", response_model=ModelConfigResponse)
async def set_default_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set a model as the system default model (Admin only)."""
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以设置系统默认模型")

    stmt = select(ModelConfig).where(ModelConfig.id == model_id)
    res = await db.execute(stmt)
    cfg = res.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="模型配置不存在")

    await db.execute(update(ModelConfig).values(is_default=False))
    cfg.is_default = True
    cfg.enabled = True  # Default model must be enabled
    await db.commit()
    await db.refresh(cfg)

    return ModelConfigResponse(
        id=cfg.id,
        provider_id=cfg.provider_id,
        model_name=cfg.model_name,
        display_name=cfg.display_name,
        model_type=cfg.model_type,
        capabilities=ModelCapabilities(**cfg.capabilities),
        is_default=cfg.is_default,
        enabled=cfg.enabled,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


@router.get("/active", response_model=List[ActiveModelItem])
async def list_active_models(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get lightweight flat list of active enabled models for ChatPage model switcher.
    """
    await seed_default_models_if_empty(db)

    if current_user.role == "admin":
        provider_filter = True
    else:
        provider_filter = (ModelProvider.owner_id.is_(None)) | (ModelProvider.owner_id == current_user.id)

    stmt = (
        select(ModelConfig, ModelProvider)
        .join(ModelProvider, ModelConfig.provider_id == ModelProvider.id)
        .where(
            ModelConfig.enabled == True,
            ModelProvider.enabled == True,
            provider_filter,
        )
        .order_by(ModelConfig.is_default.desc(), ModelConfig.created_at.asc())
    )
    res = await db.execute(stmt)
    rows = res.all()

    items: List[ActiveModelItem] = []
    for cfg, prov in rows:
        items.append(
            ActiveModelItem(
                id=cfg.id,
                provider_id=prov.id,
                provider_name=prov.name,
                provider_type=prov.provider_type,
                model_name=cfg.model_name,
                display_name=cfg.display_name,
                is_default=cfg.is_default,
                capabilities=ModelCapabilities(**cfg.capabilities),
            )
        )
    return items


@router.post("/test", response_model=ModelTestResponse)
async def test_connection(
    payload: ModelTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Test real connectivity and chat completion inference of a model.
    Accepts either existing model_id/provider_id or ad-hoc test parameters.
    """
    base_url = payload.base_url
    api_key = payload.api_key
    model_name = payload.model_name

    # If model_id provided, look up from DB
    if payload.model_id:
        stmt = (
            select(ModelConfig)
            .options(selectinload(ModelConfig.provider))
            .where(ModelConfig.id == payload.model_id)
        )
        res = await db.execute(stmt)
        cfg = res.scalar_one_or_none()
        if not cfg:
            return ModelTestResponse(success=False, latency_ms=0, error="指定的模型配置不存在")
        if (
            cfg.provider
            and cfg.provider.owner_id is not None
            and cfg.provider.owner_id != current_user.id
            and current_user.role != "admin"
        ):
            return ModelTestResponse(success=False, latency_ms=0, error="指定的模型配置不存在")
        model_name = model_name or cfg.model_name
        if cfg.provider:
            base_url = base_url or cfg.provider.base_url
            if not api_key and cfg.provider.api_key_ciphertext:
                try:
                    api_key = decrypt_api_key(cfg.provider.api_key_ciphertext)
                except Exception as exc:
                    return ModelTestResponse(success=False, latency_ms=0, error=f"密钥解密失败: {exc}")

    # If provider_id provided without model_id
    elif payload.provider_id:
        stmt = select(ModelProvider).where(ModelProvider.id == payload.provider_id)
        res = await db.execute(stmt)
        prov = res.scalar_one_or_none()
        if not prov:
            return ModelTestResponse(success=False, latency_ms=0, error="指定的服务商不存在")
        if (
            prov.owner_id is not None
            and prov.owner_id != current_user.id
            and current_user.role != "admin"
        ):
            return ModelTestResponse(success=False, latency_ms=0, error="指定的服务商不存在")
        base_url = base_url or prov.base_url
        if not api_key and prov.api_key_ciphertext:
            try:
                api_key = decrypt_api_key(prov.api_key_ciphertext)
            except Exception as exc:
                return ModelTestResponse(success=False, latency_ms=0, error=f"密钥解密失败: {exc}")
        base_url = base_url or prov.base_url
        if not api_key and prov.api_key_ciphertext:
            try:
                api_key = decrypt_api_key(prov.api_key_ciphertext)
            except Exception as exc:
                return ModelTestResponse(success=False, latency_ms=0, error=f"密钥解密失败: {exc}")

    if not base_url or not model_name:
        return ModelTestResponse(
            success=False,
            latency_ms=0,
            error="缺少测试参数：必须提供 Base URL 和 模型名称 (Model Name)。",
        )

    return await test_model_connectivity(
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        timeout=15.0,
    )


@router.post("/probe/ollama", response_model=OllamaProbeResponse)
async def probe_ollama(payload: OllamaProbeRequest):
    """Probe installed models on a local Ollama server."""
    return await probe_ollama_models(base_url=payload.base_url, timeout=10.0)


@router.post("/probe/vllm", response_model=VLLMProbeResponse)
async def probe_vllm(payload: VLLMProbeRequest):
    """Probe hosted models on a vLLM server."""
    return await probe_vllm_models(base_url=payload.base_url, api_key=payload.api_key, timeout=10.0)
