import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import relationship

from app.db.session import Base


class ModelProvider(Base):
    """
    Model Provider Configuration (OpenAI Compatible, Ollama, vLLM, DeepSeek, etc.).
    A provider can host multiple models with shared base_url and API key.
    """
    __tablename__ = "model_providers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # None indicates system public provider; non-empty string indicates user private provider
    owner_id = Column(String(64), nullable=True, index=True)
    name = Column(String(128), nullable=False)
    provider_type = Column(String(32), nullable=False, default="openai_compatible")
    base_url = Column(String(512), nullable=False)
    api_key_ciphertext = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
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

    # One provider has many model configurations
    models = relationship(
        "ModelConfig",
        back_populates="provider",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ModelConfig.created_at.asc()",
    )


class ModelConfig(Base):
    """
    Specific Model Configuration under a Provider.
    Defines model name, capabilities (streaming, tool calling, context window), and default status.
    """
    __tablename__ = "model_configs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id = Column(
        String(36),
        ForeignKey("model_providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_name = Column(String(128), nullable=False)
    display_name = Column(String(128), nullable=False)
    model_type = Column(String(32), nullable=False, default="chat")
    capabilities_json = Column(
        Text,
        nullable=False,
        default='{"supports_streaming": true, "supports_tool_calling": true, "supports_vision": false, "context_window": 32768}',
    )
    is_default = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
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

    provider = relationship("ModelProvider", back_populates="models", lazy="selectin")

    @property
    def capabilities(self) -> Dict[str, Any]:
        """Parsed dictionary of model capabilities."""
        if not self.capabilities_json:
            return {
                "supports_streaming": True,
                "supports_tool_calling": True,
                "supports_vision": False,
                "context_window": 32768,
            }
        try:
            return json.loads(self.capabilities_json)
        except Exception:
            return {
                "supports_streaming": True,
                "supports_tool_calling": True,
                "supports_vision": False,
                "context_window": 32768,
            }

    @capabilities.setter
    def capabilities(self, value: Dict[str, Any]) -> None:
        self.capabilities_json = json.dumps(value, ensure_ascii=False)
