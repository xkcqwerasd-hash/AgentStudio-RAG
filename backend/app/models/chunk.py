import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.datetime_utils import utc_now
from app.db.session import Base


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    knowledge_base_id = Column(
        String(36),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False, default=0)
    content = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=False, default=1)
    char_length = Column(Integer, nullable=False, default=0)
    # Dense vector representation stored as JSON string for cross-database compatibility (SQLite / Postgres)
    embedding_json = Column(Text, nullable=True)
    extra_metadata_json = Column(Text, nullable=True, default="{}")
    created_at = Column(DateTime, default=utc_now)

    document = relationship("Document", back_populates="chunks")

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
    def extra_metadata(self) -> Dict[str, Any]:
        if self.extra_metadata_json:
            try:
                return json.loads(self.extra_metadata_json)
            except Exception:
                return {}
        return {}

    @extra_metadata.setter
    def extra_metadata(self, data: Dict[str, Any]) -> None:
        self.extra_metadata_json = json.dumps(data, ensure_ascii=False)
