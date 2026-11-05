import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ModelCapabilities(BaseModel):
    """Declarative capability metadata for a model."""
    supports_streaming: bool = Field(default=True, description="是否支持 SSE 流式生成")
    supports_tool_calling: bool = Field(default=True, description="是否支持原生 Function / Tool Calling")
    supports_vision: bool = Field(default=False, description="是否支持图像多模态输入 (预留)")
    context_window: int = Field(default=32768, ge=512, le=2000000, description="最大上下文窗口大小 (Tokens)")


class ModelConfigCreate(BaseModel):
    """Payload to add a model to a provider."""
    model_name: str = Field(..., min_length=1, max_length=128, description="实际模型调用标识 (如 deepseek-chat)")
    display_name: str = Field(..., min_length=1, max_length=128, description="前端展示名称 (如 DeepSeek V3)")
    model_type: str = Field(default="chat", max_length=32, description="模型类型: chat / completion")
    capabilities: Optional[ModelCapabilities] = Field(default=None, description="模型能力规格")
    is_default: bool = Field(default=False, description="是否设为系统默认模型")
    enabled: bool = Field(default=True, description="是否启用该模型")


class ModelConfigUpdate(BaseModel):
    """Payload to update an existing model configuration."""
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    model_type: Optional[str] = Field(default=None, max_length=32)
    capabilities: Optional[ModelCapabilities] = Field(default=None)
    is_default: Optional[bool] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)


class ModelConfigResponse(BaseModel):
    """Response representing a model configuration."""
    id: str
    provider_id: str
    model_name: str
    display_name: str
    model_type: str
    capabilities: ModelCapabilities
    is_default: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ModelProviderCreate(BaseModel):
    """Payload to create a new model provider."""
    name: str = Field(..., min_length=1, max_length=128, description="服务商名称 (如 DeepSeek 官方)")
    provider_type: str = Field(
        default="openai_compatible",
        max_length=32,
        description="类型: openai_compatible / ollama / vllm / deepseek / moonshot / dashscope",
    )
    base_url: str = Field(..., min_length=1, max_length=512, description="API 服务基础地址")
    api_key: Optional[str] = Field(default=None, description="API Key (入库自动加密存储)")
    enabled: bool = Field(default=True, description="是否启用该服务商")
    is_public: bool = Field(default=False, description="是否设为系统公共服务商 (仅管理员可设置)")
    models: Optional[List[ModelConfigCreate]] = Field(default=None, description="创建时同时配置的模型列表")


class ModelProviderUpdate(BaseModel):
    """Payload to update a model provider."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    provider_type: Optional[str] = Field(default=None, max_length=32)
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=512)
    api_key: Optional[str] = Field(
        default=None,
        description="新的 API Key；若为 null 则保留原密钥，若为空字符串 '' 则清空密钥",
    )
    enabled: Optional[bool] = Field(default=None)


class ModelProviderResponse(BaseModel):
    """Response representing a model provider (API key masked for security)."""
    id: str
    name: str
    provider_type: str
    base_url: str
    has_api_key: bool
    masked_api_key: Optional[str] = None
    enabled: bool
    owner_id: Optional[str] = None
    models: List[ModelConfigResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ActiveModelItem(BaseModel):
    """Lightweight item for chat page model selector."""
    id: str
    provider_id: str
    provider_name: str
    provider_type: str
    model_name: str
    display_name: str
    is_default: bool
    capabilities: ModelCapabilities


class ModelTestRequest(BaseModel):
    """Request payload for model connectivity test."""
    provider_id: Optional[str] = Field(default=None, description="已有 Provider ID")
    model_id: Optional[str] = Field(default=None, description="已有 Model ID")
    provider_type: Optional[str] = Field(default="openai_compatible")
    base_url: Optional[str] = Field(default=None, description="测试的 Base URL (如未提供则从 provider_id 读取)")
    api_key: Optional[str] = Field(default=None, description="测试的 API Key (若传空且有 provider_id 则使用已保存密钥)")
    model_name: Optional[str] = Field(default=None, description="测试的具体模型名称")


class ModelTestResponse(BaseModel):
    """Response for model connectivity test."""
    success: bool
    latency_ms: float
    reply_snippet: Optional[str] = None
    error: Optional[str] = None


class OllamaProbeRequest(BaseModel):
    """Request to probe downloaded models on an Ollama server."""
    base_url: str = Field(default="http://localhost:11434", description="Ollama 服务地址")


class OllamaModelItem(BaseModel):
    name: str
    size: Optional[int] = None
    modified_at: Optional[str] = None
    family: Optional[str] = None
    parameter_size: Optional[str] = None
    quantization_level: Optional[str] = None


class OllamaProbeResponse(BaseModel):
    success: bool
    models: List[OllamaModelItem] = Field(default_factory=list)
    error: Optional[str] = None


class VLLMProbeRequest(BaseModel):
    """Request to probe available models on a vLLM server."""
    base_url: str = Field(default="http://localhost:8000/v1", description="vLLM 服务地址")
    api_key: Optional[str] = Field(default=None, description="可选服务访问密钥")


class VLLMProbeResponse(BaseModel):
    success: bool
    models: List[str] = Field(default_factory=list)
    error: Optional[str] = None
