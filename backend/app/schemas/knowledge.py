from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="知识库名称")
    description: Optional[str] = Field(default="", max_length=1000, description="知识库描述")
    embedding_provider: Optional[str] = Field(
        default=None,
        description="向量模型提供方 (local_bge, openai, test_double)",
    )
    embedding_model: Optional[str] = Field(
        default=None,
        description="向量模型名称 (例如 BAAI/bge-small-zh-v1.5 或 BAAI/bge-m3)",
    )
    chunk_size: int = Field(default=500, ge=100, le=2000, description="分块大小 (字符数)")
    chunk_overlap: int = Field(default=50, ge=0, le=500, description="分块重叠大小")


class KnowledgeBaseCreate(KnowledgeBaseBase):
    pass


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=1000)
    chunk_size: Optional[int] = Field(None, ge=100, le=2000)
    chunk_overlap: Optional[int] = Field(None, ge=0, le=500)


class KnowledgeBaseResponse(KnowledgeBaseBase):
    id: str
    embedding_provider: str = "local_bge"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimensions: int = 512
    index_version: int = 1
    document_count: int = 0
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReindexRequest(BaseModel):
    target_provider: Optional[str] = Field(
        default=None,
        description="目标 Embedding 提供方 (如 'local_bge', 'openai', 'test_double')，留空则使用当前配置",
    )
    target_model: Optional[str] = Field(
        default=None,
        description="目标 Embedding 模型标识，留空则使用当前配置",
    )
    batch_size: Optional[int] = Field(
        default=None,
        ge=1,
        le=256,
        description="向量重新计算批次大小",
    )


class ReindexResponse(BaseModel):
    knowledge_base_id: str
    reindexed_chunks: int
    duration_ms: float
    previous_provider: str
    previous_model: str
    previous_dimensions: int
    new_provider: str
    new_model: str
    new_dimensions: int
    new_index_version: int
    status: str = "COMPLETED"
