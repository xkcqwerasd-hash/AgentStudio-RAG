import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.session import Base


class AgentDefinition(Base):
    """
    Persistent definition and configuration entity for an Agent in AgentStudio-RAG.
    Maintains system prompt, authorized tools, execution limits, and knowledge boundaries.
    """
    __tablename__ = "agent_definitions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # None indicates system public agent; non-empty string indicates user private agent
    owner_id = Column(String(64), nullable=True, index=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(Text, nullable=True, default="")
    system_prompt = Column(Text, nullable=True)
    knowledge_base_id = Column(
        String(36),
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    enabled_tools = Column(Text, nullable=False, default="[]")
    max_steps = Column(Integer, nullable=False, default=5)
    max_tool_calls = Column(Integer, nullable=False, default=10)
    timeout_seconds = Column(Float, nullable=False, default=60.0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
    )

    # Relationships
    knowledge_base = relationship("KnowledgeBase", backref="agents")
    runs = relationship(
        "AgentRun",
        back_populates="agent_definition",
        cascade="all, delete-orphan",
        order_by="desc(AgentRun.created_at)",
        lazy="selectin",
    )

    @property
    def tools(self) -> List[str]:
        """Parsed list of authorized tool names."""
        if self.enabled_tools:
            try:
                res = json.loads(self.enabled_tools)
                return res if isinstance(res, list) else []
            except Exception:
                return []
        return []

    def set_tools(self, tool_list: List[str]) -> None:
        """Set enabled tools list as JSON string."""
        self.enabled_tools = json.dumps(list(tool_list), ensure_ascii=False)


class AgentRun(Base):
    """
    Persistent execution record entity for an Agent Run.
    Preserves user task, final answer, execution steps, tool call count,
    verified citations, and observable trace timeline.
    """
    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(
        String(64),
        nullable=False,
        default=lambda: settings.DEFAULT_LOCAL_USER_ID,
        index=True,
    )
    agent_definition_id = Column(
        String(36),
        ForeignKey("agent_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(
        String(36),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Lifecycle status: pending / running / completed / failed / cancelled / max_steps_reached
    status = Column(String(32), nullable=False, default="pending", index=True)
    input = Column(Text, nullable=False)
    final_answer = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    execution_steps = Column(Integer, nullable=False, default=0)
    tool_call_count = Column(Integer, nullable=False, default=0)
    citations_json = Column(Text, nullable=True, default="[]")
    execution_trace_json = Column(Text, nullable=True, default="[]")
    started_at = Column(DateTime, default=utc_now)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Float, nullable=True)
    created_at = Column(DateTime, default=utc_now, index=True)

    # Relationships
    agent_definition = relationship("AgentDefinition", back_populates="runs")
    conversation = relationship("Conversation")

    @property
    def citations(self) -> List[Dict[str, Any]]:
        """Parsed list of verified citation objects."""
        if self.citations_json:
            try:
                res = json.loads(self.citations_json)
                return res if isinstance(res, list) else []
            except Exception:
                return []
        return []

    @property
    def execution_trace(self) -> List[Dict[str, Any]]:
        """Parsed list of observable trace events."""
        if self.execution_trace_json:
            try:
                res = json.loads(self.execution_trace_json)
                return res if isinstance(res, list) else []
            except Exception:
                return []
        return []
