from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class UserIntent(str, Enum):
    """Core intent categories recognized by Intent Router."""
    DIRECT_CHAT = "DIRECT_CHAT"                      # 通用问答 / 概念常识 / 闲聊礼貌
    CONVERSATION_FOLLOWUP = "CONVERSATION_FOLLOWUP"  # 会话追问 (指代前文、继续、展开讲讲)
    KNOWLEDGE_BASE = "KNOWLEDGE_BASE"                # 明确企业知识库检索
    ATTACHMENT = "ATTACHMENT"                        # 明确会话临时附件检索
    HYBRID = "HYBRID"                                # 跨源联合检索 (KB + Attachment)
    COMPLEX_AGENT = "COMPLEX_AGENT"                  # 复杂多步规划与工具协同分析


class ExecutionPath(str, Enum):
    """Target execution paths decided by Intent Router."""
    DIRECT_LLM = "DIRECT_LLM"          # 纯大模型直接生成 (0 检索、0 工具)
    KNOWLEDGE_BASE = "KNOWLEDGE_BASE"  # Workflow RAG 知识库检索
    ATTACHMENT = "ATTACHMENT"          # 会话临时附件检索
    HYBRID = "HYBRID"                  # 知识库 + 附件联合检索
    AGENT = "AGENT"                    # LangGraph Agent Runtime 复杂智能体推理


class RouteDecision(BaseModel):
    """Immutable routing decision payload containing intent, target path, and rationale."""
    intent: UserIntent = Field(..., description="识别出的核心用户意图类型")
    execution_path: ExecutionPath = Field(..., description="决定的最终执行路径")
    required_sources: List[str] = Field(
        default_factory=list,
        description="需要调用的外部数据源列表: ['knowledge_base', 'attachments']",
    )
    reason: str = Field(..., description="路由判定原因说明")
    should_retrieve: bool = Field(..., description="是否需要触发外部检索 (Dense/BM25/Attachment)")
    should_use_agent: bool = Field(..., description="是否需要进入 LangGraph Agent Runtime")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="路由置信度")
