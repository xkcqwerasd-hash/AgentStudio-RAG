import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import relationship

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.session import Base


class MemoryType(str):
    USER_PROFILE = "USER_PROFILE"        # 长期用户画像、技术偏好、语言习惯、个人称谓
    PROJECT_CONTEXT = "PROJECT_CONTEXT"  # 核心业务架构、系统长期规划、技术栈约束
    IMPORTANT_FACT = "IMPORTANT_FACT"    # 关键业务事实、约定规则、重要历史决策
    DIRECT_RULE = "DIRECT_RULE"          # 用户通过“请记住...”主动设定的不可违背规则


class MemorySourceType(str):
    EXPLICIT_USER = "EXPLICIT_USER"      # 用户显式指令或主动手动录入
    AUTO_EXTRACTED = "AUTO_EXTRACTED"    # 系统后台从对话中自动提取沉淀


class Memory(Base):
    __tablename__ = "memories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 用户归属（兼容 Local 与 Server 双部署模式，未来无缝外键关联 users.id）
    owner_id = Column(
        String(64),
        nullable=False,
        default=lambda: settings.DEFAULT_LOCAL_USER_ID,
        index=True,
    )

    # 记忆提炼文本（高浓缩事实）
    content = Column(Text, nullable=False)

    # 记忆类型与分类标签
    memory_type = Column(String(32), nullable=False, default=MemoryType.IMPORTANT_FACT, index=True)
    tags_json = Column(Text, nullable=True, default="[]")  # e.g. ["frontend", "react"]

    # 来源机制与溯源指针
    source_type = Column(String(32), nullable=False, default=MemorySourceType.EXPLICIT_USER)
    source_conversation_id = Column(
        String(36),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_message_id = Column(
        String(36),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 置信度 (0.0 ~ 1.0)
    confidence = Column(Float, nullable=False, default=1.0)

    # 语义检索向量存储（复用与 DocumentChunk 相同的 embedding_json 方案）
    embedding_json = Column(Text, nullable=True)

    # 记忆使用统计与生命周期控制
    access_count = Column(Integer, nullable=False, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)

    created_at = Column(
        DateTime,
        nullable=False,
        default=utc_now,
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    # 复合索引：加速用户私有记忆查询与状态过滤
    __table_args__ = (
        Index("ix_memories_owner_active_type", "owner_id", "is_active", "memory_type"),
    )

    # 关联溯源
    conversation = relationship("Conversation", foreign_keys=[source_conversation_id])
    message = relationship("Message", foreign_keys=[source_message_id])

    @property
    def embedding(self) -> Optional[List[float]]:
        if self.embedding_json:
            try:
                return json.loads(self.embedding_json)
            except Exception:
                return None
        return None

    @embedding.setter
    def embedding(self, vec: Optional[List[float]]) -> None:
        if vec is not None:
            self.embedding_json = json.dumps(vec)
        else:
            self.embedding_json = None

    @property
    def tags(self) -> List[str]:
        if self.tags_json:
            try:
                return json.loads(self.tags_json)
            except Exception:
                return []
        return []

    @tags.setter
    def tags(self, t: List[str]) -> None:
        self.tags_json = json.dumps(t, ensure_ascii=False)


class UserMemorySetting(Base):
    """
    Per-user long-term memory configuration.
    Strictly partitioned by owner_id to support multi-tenant isolation.
    """
    __tablename__ = "user_memory_settings"

    owner_id = Column(
        String(64),
        primary_key=True,
        default=lambda: settings.DEFAULT_LOCAL_USER_ID,
    )
    auto_memory_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=utc_now,
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class MemoryExtractionLog(Base):
    """
    Audit log and idempotency tracking for automatic memory extraction runs.
    Ensures that a message turn is never processed repeatedly.
    """
    __tablename__ = "memory_extraction_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id = Column(String(36), nullable=False, index=True, unique=True)
    conversation_id = Column(String(36), nullable=True, index=True)
    owner_id = Column(
        String(64),
        nullable=False,
        default=lambda: settings.DEFAULT_LOCAL_USER_ID,
        index=True,
    )
    status = Column(String(32), nullable=False)  # "EXTRACTED", "SKIPPED", "DISABLED", "FAILED"
    extracted_count = Column(Integer, nullable=False, default=0)
    reason = Column(String(255), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=utc_now,
    )

