import shutil
import uuid
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import get_current_user_id
from app.core.config import settings
from app.core.exceptions import (
    DocumentAlreadyExistsError,
    DocumentNotFoundError,
    FileTooLargeError,
    KnowledgeBaseNotFoundError,
    UnsupportedFileTypeError,
)
from app.core.logging import logger
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.reindex import ReindexService
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.schemas.document import ChunkPageResponse, ChunkResponse, DocumentResponse
from app.schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    ReindexRequest,
    ReindexResponse,
)
from sqlalchemy import delete

router = APIRouter(prefix="/knowledge-bases", tags=["Knowledge Bases"])


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    # Determine embedding configuration from payload or global settings
    embedding_client = get_embedding_client(
        provider=payload.embedding_provider or settings.EMBEDDING_PROVIDER,
        model_name=payload.embedding_model or settings.EMBEDDING_MODEL_NAME,
    )

    kb = KnowledgeBase(
        name=payload.name,
        description=payload.description or "",
        owner_id=user_id,
        embedding_provider=embedding_client.provider_name,
        embedding_model=embedding_client.model_name,
        embedding_dimensions=embedding_client.dimensions,
        index_version=1,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)

    resp = KnowledgeBaseResponse.model_validate(kb)
    resp.document_count = 0
    resp.chunk_count = 0
    return resp


@router.get("", response_model=List[KnowledgeBaseResponse])
async def list_knowledge_bases(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    stmt = (
        select(KnowledgeBase)
        .where(KnowledgeBase.owner_id == user_id)
        .order_by(KnowledgeBase.created_at.desc())
    )
    res = await db.execute(stmt)
    kbs = res.scalars().all()

    response_list: List[KnowledgeBaseResponse] = []
    for kb in kbs:
        doc_count_stmt = select(func.count(Document.id)).where(Document.knowledge_base_id == kb.id)
        chunk_count_stmt = select(func.count(DocumentChunk.id)).where(
            DocumentChunk.knowledge_base_id == kb.id
        )

        doc_count = (await db.execute(doc_count_stmt)).scalar() or 0
        chunk_count = (await db.execute(chunk_count_stmt)).scalar() or 0

        item = KnowledgeBaseResponse.model_validate(kb)
        item.document_count = doc_count
        item.chunk_count = chunk_count
        response_list.append(item)

    return response_list


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(kb_id)

    doc_count_stmt = select(func.count(Document.id)).where(Document.knowledge_base_id == kb.id)
    chunk_count_stmt = select(func.count(DocumentChunk.id)).where(
        DocumentChunk.knowledge_base_id == kb.id
    )

    doc_count = (await db.execute(doc_count_stmt)).scalar() or 0
    chunk_count = (await db.execute(chunk_count_stmt)).scalar() or 0

    item = KnowledgeBaseResponse.model_validate(kb)
    item.document_count = doc_count
    item.chunk_count = chunk_count
    return item


@router.post("/{kb_id}/reindex", response_model=ReindexResponse)
async def reindex_knowledge_base(
    kb_id: str,
    payload: Optional[ReindexRequest] = None,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    kb_stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(kb_stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(kb_id)

    reindex_service = ReindexService(db)
    target_provider = payload.target_provider if payload else None
    target_model = payload.target_model if payload else None
    batch_size = payload.batch_size if payload else None
    return await reindex_service.reindex_knowledge_base(
        kb_id=kb_id,
        target_provider=target_provider,
        target_model=target_model,
        batch_size=batch_size,
    )


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(kb_id)

    # 1. Clean up physical files for all documents in this KB
    docs_stmt = select(Document).where(Document.knowledge_base_id == kb_id)
    docs_res = await db.execute(docs_stmt)
    for d in docs_res.scalars().all():
        try:
            file_path = Path(d.file_path)
            if file_path.exists():
                file_path.unlink()
                parent_dir = file_path.parent
                if parent_dir.exists() and not any(parent_dir.iterdir()):
                    parent_dir.rmdir()
        except Exception as exc:
            logger.warning(f"Error removing file {d.file_path}: {exc}")

    # 2. Explicitly delete all chunks, documents, conversations and knowledge base using direct query
    await db.execute(delete(DocumentChunk).where(DocumentChunk.knowledge_base_id == kb_id))
    await db.execute(delete(Document).where(Document.knowledge_base_id == kb_id))
    await db.execute(delete(Conversation).where(Conversation.knowledge_base_id == kb_id))
    await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    await db.commit()

    # 3. Clean up BM25 index cache
    try:
        from app.retrieval.bm25_retriever import bm25_index_manager
        bm25_index_manager.remove_knowledge_base(kb_id)
    except Exception as e:
        logger.warning(f"Failed to remove BM25 index on KB deletion: {e}")

    return None


@router.post("/{kb_id}/documents/upload", response_model=DocumentResponse)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    # Verify KB existence and ownership
    kb_stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(kb_stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(kb_id)

    # Sanitize filename (prevent directory traversal)
    raw_name = file.filename or "unknown.txt"
    safe_name = Path(raw_name).name
    ext = Path(safe_name).suffix.lower()
    allowed_exts = [".pdf", ".md", ".markdown", ".txt"]
    if ext not in allowed_exts:
        raise UnsupportedFileTypeError(ext)

    # Check for duplicate document in this KB
    existing_stmt = select(Document).where(
        Document.knowledge_base_id == kb.id,
        Document.file_name == safe_name,
    )
    existing_doc = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing_doc:
        raise DocumentAlreadyExistsError(safe_name, kb.name)

    # Validate size and read content
    content = await file.read()
    file_size = len(content)
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if file_size > max_bytes:
        raise FileTooLargeError(settings.MAX_UPLOAD_SIZE_MB)

    # Determine file type string
    file_type = "pdf" if ext == ".pdf" else ("markdown" if ext in [".md", ".markdown"] else "txt")

    doc_id = str(uuid.uuid4())
    doc_dir = settings.UPLOAD_DIR / user_id / "documents" / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    saved_path = doc_dir / safe_name

    with open(saved_path, "wb") as f:
        f.write(content)

    doc = Document(
        id=doc_id,
        knowledge_base_id=kb.id,
        file_name=safe_name,
        file_type=file_type,
        file_size=file_size,
        file_path=str(saved_path),
        status="PENDING",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # Run ingestion pipeline
    pipeline = IngestionPipeline(db)
    processed_doc = await pipeline.process_document(doc.id)

    return DocumentResponse.model_validate(processed_doc)


@router.get("/{kb_id}/documents", response_model=List[DocumentResponse])
async def list_documents(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    kb_stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.owner_id == user_id,
    )
    res = await db.execute(kb_stmt)
    kb = res.scalar_one_or_none()
    if not kb:
        raise KnowledgeBaseNotFoundError(kb_id)

    stmt = (
        select(Document)
        .where(Document.knowledge_base_id == kb_id)
        .order_by(Document.created_at.desc())
    )
    res = await db.execute(stmt)
    docs = res.scalars().all()
    return [DocumentResponse.model_validate(d) for d in docs]
