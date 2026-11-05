from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class AttachmentPageItem(BaseModel):
    page_number: int = Field(..., description="页码或逻辑块序号(从1开始)")
    text: str = Field(..., description="提取的文本内容")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="结构化元数据(如标题路径、块类型)")


class ConversationAttachmentResponse(BaseModel):
    id: str = Field(..., description="附件唯一UUID")
    conversation_id: str = Field(..., description="所属会话UUID")
    original_filename: str = Field(..., description="用户原始文件名")
    storage_key: str = Field(..., description="服务器相对存储键值")
    file_extension: str = Field(..., description="文件扩展名(如 .pdf, .docx)")
    mime_type: str = Field(..., description="经真实校验的 MIME 类型")
    file_size: int = Field(..., description="文件体积(字节)")
    parse_status: str = Field(..., description="解析状态: PENDING / COMPLETED / FAILED")
    page_count: int = Field(..., description="总页数或逻辑分块数")
    char_length: int = Field(..., description="提取的总字符数")
    token_count: int = Field(..., description="估算Token消耗")
    error_message: Optional[str] = Field(None, description="解析失败原因")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConversationAttachmentDetailResponse(ConversationAttachmentResponse):
    parsed_pages: List[AttachmentPageItem] = Field(default_factory=list, description="分页/分块结构列表")
    extracted_text: str = Field(default="", description="完整提取的纯文本")
