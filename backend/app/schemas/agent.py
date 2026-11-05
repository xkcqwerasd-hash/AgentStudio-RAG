import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentDefinitionCreate(BaseModel):
    """Payload to create an AgentDefinition."""
    name: str = Field(..., min_length=1, max_length=128, description="智能体唯一名称")
    description: Optional[str] = Field(default="", max_length=1000, description="智能体业务描述")
    system_prompt: Optional[str] = Field(default=None, max_length=10000, description="智能体系统提示词")
    knowledge_base_id: Optional[str] = Field(default=None, description="限定访问的唯一知识库UUID")
    enabled_tools: List[str] = Field(
        default_factory=lambda: ["search_knowledge_base"],
        description="允许使用的工具名称列表",
    )
    max_steps: int = Field(default=5, ge=1, le=20, description="最大执行步数")
    max_tool_calls: int = Field(default=10, ge=1, le=30, description="最大工具调用次数")
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0, description="运行超时时间(秒)")
    is_active: bool = Field(default=True, description="是否启用该智能体")
    is_public: bool = Field(default=False, description="是否设为系统公共智能体 (仅管理员可设置)")


class AgentDefinitionUpdate(BaseModel):
    """Payload to update an AgentDefinition."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=1000)
    system_prompt: Optional[str] = Field(default=None, max_length=10000)
    knowledge_base_id: Optional[str] = Field(default=None)
    enabled_tools: Optional[List[str]] = Field(default=None)
    max_steps: Optional[int] = Field(default=None, ge=1, le=20)
    max_tool_calls: Optional[int] = Field(default=None, ge=1, le=30)
    timeout_seconds: Optional[float] = Field(default=None, ge=5.0, le=300.0)
    is_active: Optional[bool] = Field(default=None)


class AgentDefinitionResponse(BaseModel):
    """Full detail of an AgentDefinition."""
    id: str
    name: str
    description: Optional[str] = ""
    system_prompt: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    enabled_tools: List[str]
    max_steps: int
    max_tool_calls: int
    timeout_seconds: float
    is_active: bool
    owner_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentRunRequest(BaseModel):
    """Request payload to execute an Agent Run."""
    input: str = Field(..., min_length=1, max_length=10000, description="用户原始任务或指令")
    conversation_id: Optional[str] = Field(default=None, description="可选关联的历史会话ID")
    knowledge_base_id: Optional[str] = Field(default=None, description="可选限定的目标知识库ID")
    model_id: Optional[str] = Field(default=None, description="运行时指定的具体模型配置ID (可选)")


class AgentRunListItemResponse(BaseModel):
    """Summary item in Agent Runs list."""
    id: str
    agent_definition_id: str
    conversation_id: Optional[str] = None
    status: str
    input: str
    final_answer_preview: Optional[str] = None
    execution_steps: int
    tool_call_count: int
    citations_count: int
    owner_id: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    created_at: datetime


class AgentRunResponse(BaseModel):
    """Full execution detail of an Agent Run."""
    id: str
    agent_definition_id: str
    conversation_id: Optional[str] = None
    status: str
    input: str
    final_answer: Optional[str] = None
    error: Optional[str] = None
    execution_steps: int
    tool_call_count: int
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    execution_trace: List[Dict[str, Any]] = Field(default_factory=list)
    owner_id: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
