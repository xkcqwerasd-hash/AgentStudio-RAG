from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import Citation


class ConversationBase(BaseModel):
    title: str = Field(default="新建对话", max_length=255, description="会话标题")
    mode: str = Field(default="workflow", description="会话执行模式: workflow / agentic")


class ConversationCreate(BaseModel):
    knowledge_base_id: Optional[str] = Field(None, description="绑定的知识库ID (可选)")
    title: Optional[str] = Field(default="新建对话", max_length=255, description="会话标题")
    mode: str = Field(default="workflow", description="会话执行模式")


class ConversationUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=255, description="修改会话标题")


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    citations: List[Citation] = Field(default_factory=list)
    execution_info: Optional[Dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConversationResponse(BaseModel):
    id: str
    knowledge_base_id: Optional[str] = None
    title: str
    mode: str = "workflow"
    message_count: int = 0
    last_message_preview: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConversationDetailResponse(ConversationResponse):
    messages: List[MessageResponse] = Field(default_factory=list)
