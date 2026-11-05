import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.session import Base


def default_kb_provider() -> str:
    return settings.EMBEDDING_PROVIDER


def default_kb_model() -> str:
    if settings.EMBEDDING_PROVIDER == "test_double":
        return "hash-projection-v1"
    return settings.EMBEDDING_MODEL_NAME


def default_kb_dimensions() -> int:
    if settings.EMBEDDING_PROVIDER == "test_double":
        return 1024
    return settings.EMBEDDING_DIMENSIONS


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(
        String(64),
        nullable=False,
        default=lambda: settings.DEFAULT_LOCAL_USER_ID,
        index=True,
    )
    name = Column(String(128), nullable=False, index=True)
    description = Column(Text, nullable=True, default="")
    embedding_provider = Column(String(32), nullable=False, default=default_kb_provider)
    embedding_model = Column(String(64), nullable=False, default=default_kb_model)
    embedding_dimensions = Column(Integer, nullable=False, default=default_kb_dimensions)
    index_version = Column(Integer, nullable=False, default=1)
    chunk_size = Column(Integer, nullable=False, default=500)
    chunk_overlap = Column(Integer, nullable=False, default=50)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
    )

    documents = relationship(
        "Document",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
