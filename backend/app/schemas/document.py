from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict


class DocumentResponse(BaseModel):
    id: str
    knowledge_base_id: str
    file_name: str
    file_type: str
    file_size: int
    status: str
    page_count: int
    chunk_count: int
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChunkResponse(BaseModel):
    id: str
    document_id: str
    knowledge_base_id: str
    chunk_index: int
    content: str
    page_number: int
    char_length: int
    extra_metadata: Dict[str, Any] = {}
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChunkPageResponse(BaseModel):
    items: List[ChunkResponse]
    total: int
    page: int
    page_size: int
