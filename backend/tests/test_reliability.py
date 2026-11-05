import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeBase


@pytest.mark.asyncio
async def test_duplicate_document_upload_rejected(client: AsyncClient, db_session: AsyncSession):
    """Verify uploading a document with an identical filename in the same KB is rejected (HTTP 409)."""
    kb = KnowledgeBase(name="去重测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    file_content = b"first content"
    files = {"file": ("manual.md", file_content, "text/markdown")}

    # First upload -> Success
    res1 = await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/upload", files=files)
    assert res1.status_code == 200

    # Second upload with same filename -> 409 Conflict
    files2 = {"file": ("manual.md", b"second content", "text/markdown")}
    res2 = await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/upload", files=files2)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


@pytest.mark.asyncio
async def test_path_traversal_filename_sanitization(client: AsyncClient, db_session: AsyncSession):
    """Verify filenames with directory traversal like '../../malicious.md' are sanitized to 'malicious.md'."""
    kb = KnowledgeBase(name="路径安全测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    files = {"file": ("../../malicious.md", b"safe content", "text/markdown")}
    res = await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/upload", files=files)
    assert res.status_code == 200
    doc = res.json()
    assert doc["file_name"] == "malicious.md"
    assert ".." not in doc["file_name"]
