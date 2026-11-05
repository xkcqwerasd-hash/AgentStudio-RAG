import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.session import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(
        String(64),
        nullable=False,
        default=lambda: settings.DEFAULT_LOCAL_USER_ID,
        index=True,
    )
    knowledge_base_id = Column(
        String(36),
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title = Column(String(255), nullable=False, default="新建对话")
    mode = Column(String(32), nullable=False, default="workflow")  # workflow / agentic
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
    )

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="selectin",
    )
    attachments = relationship(
        "ConversationAttachment",
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ConversationAttachment.created_at",
        lazy="selectin",
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(32), nullable=False)  # user, assistant, system, tool
    content = Column(Text, nullable=False)
    citations_json = Column(Text, nullable=True, default="[]")
    execution_info_json = Column(Text, nullable=True, default="{}")
    created_at = Column(DateTime, default=utc_now)

    conversation = relationship("Conversation", back_populates="messages")

    @property
    def citations(self) -> List[Dict[str, Any]]:
        if self.citations_json:
            try:
                return json.loads(self.citations_json)
            except Exception:
                return []
        return []

    @citations.setter
    def citations(self, data: List[Dict[str, Any]]) -> None:
        self.citations_json = json.dumps(data, ensure_ascii=False)

    @property
    def execution_info(self) -> Dict[str, Any]:
        if self.execution_info_json:
            try:
                return json.loads(self.execution_info_json)
            except Exception:
                return {}
        return {}

    @execution_info.setter
    def execution_info(self, data: Dict[str, Any]) -> None:
        self.execution_info_json = json.dumps(data, ensure_ascii=False)


class ConversationAttachment(Base):
    """
    Attachment associated with a specific Conversation (P0.3).
    Stores parsed document metadata and relative storage reference.
    Strictly isolated from persistent KnowledgeBase / Document models.
    """
    __tablename__ = "conversation_attachments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    original_filename = Column(String(255), nullable=False)
    storage_key = Column(String(512), nullable=False)
    file_extension = Column(String(32), nullable=False)
    mime_type = Column(String(128), nullable=False)
    file_size = Column(Integer, nullable=False)
    parse_status = Column(String(32), nullable=False, default="PENDING")  # PENDING / COMPLETED / FAILED
    page_count = Column(Integer, nullable=False, default=0)
    char_length = Column(Integer, nullable=False, default=0)
    token_count = Column(Integer, nullable=False, default=0)
    parsed_content_json = Column(Text, nullable=True)  # List of dict: [{"page_number": int, "text": str, "metadata": dict}]
    error_message = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    conversation = relationship("Conversation", back_populates="attachments")

    @property
    def parsed_pages(self) -> List[Dict[str, Any]]:
        """List of structured pages/sections with text and metadata."""
        if not self.parsed_content_json:
            return []
        try:
            return json.loads(self.parsed_content_json)
        except Exception:
            return []

    @parsed_pages.setter
    def parsed_pages(self, pages: List[Dict[str, Any]]) -> None:
        self.parsed_content_json = json.dumps(pages, ensure_ascii=False)

    @property
    def extracted_text(self) -> str:
        """Full text content across all parsed pages/sections."""
        pages = self.parsed_pages
        return "\n\n".join(p.get("text", "") for p in pages if p.get("text"))
