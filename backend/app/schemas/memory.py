from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class MemoryCreateRequest(BaseModel):
    content: str = Field(..., min_length=2, max_length=2000, description="记忆事实文本或用户偏好描述")
    memory_type: str = Field(default="IMPORTANT_FACT", description="USER_PROFILE / PROJECT_CONTEXT / IMPORTANT_FACT / DIRECT_RULE")
    tags: Optional[List[str]] = Field(default_factory=list, description="分类标签")
    source_type: Optional[str] = Field(default="EXPLICIT_USER", description="EXPLICIT_USER / AUTO_EXTRACTED")
    source_conversation_id: Optional[str] = Field(default=None, description="来源会话 ID (可选)")
    source_message_id: Optional[str] = Field(default=None, description="来源消息 ID (可选)")
    confidence: Optional[float] = Field(default=1.0, ge=0.0, le=1.0, description="置信度")


class MemoryUpdateRequest(BaseModel):
    content: Optional[str] = Field(default=None, min_length=2, max_length=2000, description="更新的记忆内容")
    memory_type: Optional[str] = Field(default=None, description="记忆类型")
    tags: Optional[List[str]] = Field(default=None, description="分类标签")
    is_active: Optional[bool] = Field(default=None, description="启用/停用状态")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="置信度")


class MemoryResponse(BaseModel):
    id: str
    owner_id: str
    content: str
    memory_type: str
    tags: List[str]
    source_type: str
    source_conversation_id: Optional[str] = None
    source_message_id: Optional[str] = None
    confidence: float
    access_count: int
    last_accessed_at: Optional[datetime] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MemoryListResponse(BaseModel):
    items: List[MemoryResponse]
    total: int
    page: int
    page_size: int


class MemoryQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="检索匹配的 Query")
    top_k: Optional[int] = Field(default=5, ge=1, le=20, description="召回最大数量")
    threshold: Optional[float] = Field(default=0.65, ge=0.0, le=1.0, description="相似度门槛")
    memory_type: Optional[str] = Field(default=None, description="限定记忆类型过滤 (可选)")


class MemoryQueryResultItem(BaseModel):
    memory: MemoryResponse
    similarity_score: float


class UserMemorySettingResponse(BaseModel):
    owner_id: str
    auto_memory_enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserMemorySettingUpdateRequest(BaseModel):
    auto_memory_enabled: bool = Field(..., description="是否开启自动记忆沉淀")


class MemoryStatsResponse(BaseModel):
    total_memories: int
    active_memories: int
    profile_count: int
    background_count: int
    fact_count: int
    rule_count: int
    auto_memory_enabled: bool


class MemoryExtractionLogResponse(BaseModel):
    id: str
    message_id: str
    conversation_id: Optional[str] = None
    owner_id: str
    status: str
    extracted_count: int
    reason: Optional[str] = None
    created_at: datetime


class MemoryExtractionTestRequest(BaseModel):
    query: str = Field(..., min_length=1, description="用户提问内容")
    answer: Optional[str] = Field(default="", description="助手回答内容")
    conversation_id: Optional[str] = Field(default=None, description="来源会话 ID (可选)")

