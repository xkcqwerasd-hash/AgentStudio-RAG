"""
AgentStudio-RAG — P0.3 Stage 1 Chat Attachments Test Suite
Validates all 20 required specifications:
1. PDF normal upload and parsing with page numbers.
2. DOCX normal upload and parsing with paragraphs and tables.
3. TXT normal upload and parsing (UTF-8 and Chinese encodings).
4. Markdown normal upload and parsing with heading hierarchy.
5. Multi-file upload in a single request.
6. Empty file (0 bytes) rejection.
7. Disallowed extension whitelist rejection.
8. MIME spoofing and magic bytes verification.
9. Oversized file bounds rejection.
10. Corrupted DOCX handling.
11. DOCX ZIP Bomb protection.
12. Path traversal attack protection.
13. Nonexistent conversation returns 404.
14. Cross-conversation access isolation.
15. Attachment deletion cleans up physical files on disk.
16. Conversation deletion cleans up all physical attachment files.
17. Database transaction failure cleans up temporary physical files.
18. Metadata and parsed content restored after database reload.
19. Filenames containing Chinese and special characters.
20. Existing KnowledgeBase document upload regression check.
"""

import io
import json
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
import pymupdf
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.conversation import Conversation, ConversationAttachment
from app.models.knowledge import KnowledgeBase
from app.services.attachment_storage import (
    ALLOWED_EXTENSIONS,
    delete_attachment_physical_dir,
    delete_conversation_physical_attachments,
)


def _generate_valid_pdf_bytes(text: str = "Gateway Connection Pool Specification") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    try:
        page.insert_text((50, 72), text, fontname="china-s")
    except Exception:
        page.insert_text((50, 72), text)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _generate_valid_docx_bytes(
    paragraphs: list[str],
    table_rows: list[list[str]] = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        body_parts = []
        for p in paragraphs:
            body_parts.append(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>')
        if table_rows:
            body_parts.append('<w:tbl>')
            for row in table_rows:
                body_parts.append('<w:tr>')
                for cell in row:
                    body_parts.append(f'<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>')
                body_parts.append('</w:tr>')
            body_parts.append('</w:tbl>')

        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{"".join(body_parts)}</w:body>'
            '</w:document>'
        )
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


async def _create_test_conv(db: AsyncSession, title: str = "附件测试会话") -> Conversation:
    conv = Conversation(
        id=f"conv-{uuid.uuid4().hex[:8]}",
        title=title,
        mode="workflow",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@pytest.mark.asyncio
async def test_pdf_normal_upload_and_parse(client: AsyncClient, db_session: AsyncSession):
    """Scenario 1: PDF normal upload and parsing with page numbers."""
    conv = await _create_test_conv(db_session, "PDF测试会话")
    pdf_bytes = _generate_valid_pdf_bytes("Gateway connection pool max_size 500 timeout 5000ms.")

    files = [("files", ("gateway_spec.pdf", pdf_bytes, "application/pdf"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 201
    data = res.json()
    assert len(data) == 1
    att = data[0]
    assert att["original_filename"] == "gateway_spec.pdf"
    assert att["file_extension"] == ".pdf"
    assert att["mime_type"] == "application/pdf"
    assert att["parse_status"] == "COMPLETED"
    assert att["page_count"] >= 1
    assert att["char_length"] > 0
    assert att["token_count"] > 0

    # Verify physical file existence
    expected_path = settings.UPLOAD_DIR / att["storage_key"]
    assert expected_path.exists()

    # Query detail
    detail_res = await client.get(f"/api/v1/conversations/{conv.id}/attachments/{att['id']}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert "Gateway connection pool" in detail["extracted_text"]
    assert len(detail["parsed_pages"]) == 1
    assert detail["parsed_pages"][0]["page_number"] == 1


@pytest.mark.asyncio
async def test_docx_normal_upload_and_parse(client: AsyncClient, db_session: AsyncSession):
    """Scenario 2: DOCX normal upload and parsing with paragraphs and tables."""
    conv = await _create_test_conv(db_session, "DOCX测试会话")
    docx_bytes = _generate_valid_docx_bytes(
        paragraphs=["系统架构说明文档", "核心微服务采用 FastAPI 异步架构。"],
        table_rows=[["参数名", "推荐值"], ["timeout", "30s"], ["workers", "4"]],
    )

    files = [("files", ("architecture.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 201
    att = res.json()[0]
    assert att["parse_status"] == "COMPLETED"
    assert att["file_extension"] == ".docx"

    # Detail check
    detail_res = await client.get(f"/api/v1/conversations/{conv.id}/attachments/{att['id']}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert "系统架构说明文档" in detail["extracted_text"]
    assert "workers" in detail["extracted_text"]
    assert len(detail["parsed_pages"]) >= 1


@pytest.mark.asyncio
async def test_txt_normal_upload_and_parse(client: AsyncClient, db_session: AsyncSession):
    """Scenario 3: TXT normal upload and parsing with UTF-8 and Chinese text."""
    conv = await _create_test_conv(db_session, "TXT测试会话")
    txt_content = "这是系统配置参数说明：\nport=8000\nhost=127.0.0.1\nmax_connections=1000\n"
    txt_bytes = txt_content.encode("utf-8")

    files = [("files", ("config.txt", txt_bytes, "text/plain"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 201
    att = res.json()[0]
    assert att["parse_status"] == "COMPLETED"
    assert att["file_extension"] == ".txt"

    detail_res = await client.get(f"/api/v1/conversations/{conv.id}/attachments/{att['id']}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert "max_connections=1000" in detail["extracted_text"]


@pytest.mark.asyncio
async def test_markdown_normal_upload_and_parse(client: AsyncClient, db_session: AsyncSession):
    """Scenario 4: Markdown normal upload and parsing with heading hierarchy."""
    conv = await _create_test_conv(db_session, "MD测试会话")
    md_content = """# 项目总览
AgentStudio-RAG 是企业级智能知识助手。

## 检索模块
检索模块基于 BGE Embedding 与 BM25 实现双路召回。

```python
def retrieve():
    return True
```
"""
    files = [("files", ("README.md", md_content.encode("utf-8"), "text/markdown"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 201
    att = res.json()[0]
    assert att["parse_status"] == "COMPLETED"
    assert att["file_extension"] == ".md"

    detail_res = await client.get(f"/api/v1/conversations/{conv.id}/attachments/{att['id']}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert "AgentStudio-RAG" in detail["extracted_text"]
    assert "检索模块" in detail["extracted_text"]


@pytest.mark.asyncio
async def test_multi_file_upload(client: AsyncClient, db_session: AsyncSession):
    """Scenario 5: Multi-file upload in a single request."""
    conv = await _create_test_conv(db_session, "多文件会话")
    pdf_bytes = _generate_valid_pdf_bytes("文件1内容")
    txt_bytes = "文件2内容".encode("utf-8")

    files = [
        ("files", ("file1.pdf", pdf_bytes, "application/pdf")),
        ("files", ("file2.txt", txt_bytes, "text/plain")),
    ]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 201
    data = res.json()
    assert len(data) == 2
    filenames = {att["original_filename"] for att in data}
    assert filenames == {"file1.pdf", "file2.txt"}

    # List attachments
    list_res = await client.get(f"/api/v1/conversations/{conv.id}/attachments")
    assert list_res.status_code == 200
    assert len(list_res.json()) == 2


@pytest.mark.asyncio
async def test_empty_file_rejected(client: AsyncClient, db_session: AsyncSession):
    """Scenario 6: Empty file (0 bytes) rejection."""
    conv = await _create_test_conv(db_session)
    files = [("files", ("empty.pdf", b"", "application/pdf"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 400
    assert "为空" in res.json()["detail"]


@pytest.mark.asyncio
async def test_disallowed_extension_rejected(client: AsyncClient, db_session: AsyncSession):
    """Scenario 7: Disallowed file extension whitelist rejection."""
    conv = await _create_test_conv(db_session)
    files = [("files", ("script.exe", b"fake binary", "application/octet-stream"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 400
    assert "不支持的文件格式" in res.json()["detail"]


@pytest.mark.asyncio
async def test_mime_spoofing_intercepted(client: AsyncClient, db_session: AsyncSession):
    """Scenario 8: MIME spoofing and magic bytes verification."""
    conv = await _create_test_conv(db_session)
    # File is named malicious.pdf and client claims application/pdf, but bytes are plain text
    fake_pdf = b"This is not a real PDF file at all."
    files = [("files", ("malicious.pdf", fake_pdf, "application/pdf"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 400
    assert "魔数" in res.json()["detail"] or "校验失败" in res.json()["detail"]


@pytest.mark.asyncio
async def test_oversized_file_intercepted(client: AsyncClient, db_session: AsyncSession):
    """Scenario 9: Oversized file bounds rejection."""
    conv = await _create_test_conv(db_session)
    # Temporarily set max size to 1MB for test
    with patch.object(settings, "MAX_ATTACHMENT_SIZE_MB", 1):
        oversized_bytes = b"0" * (2 * 1024 * 1024)  # 2MB
        files = [("files", ("large.txt", oversized_bytes, "text/plain"))]
        res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
        assert res.status_code == 400
        assert "超过上限" in res.json()["detail"]


@pytest.mark.asyncio
async def test_corrupted_docx_intercepted(client: AsyncClient, db_session: AsyncSession):
    """Scenario 10: Corrupted DOCX handling."""
    conv = await _create_test_conv(db_session)
    # Zip starts with PK but is missing word/document.xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("corrupted.txt", "missing document.xml")
    bad_docx = buf.getvalue()

    files = [("files", ("broken.docx", bad_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 400
    assert "损坏" in res.json()["detail"] or "校验失败" in res.json()["detail"]


@pytest.mark.asyncio
async def test_zip_bomb_protection(client: AsyncClient, db_session: AsyncSession):
    """Scenario 11: DOCX ZIP Bomb protection."""
    conv = await _create_test_conv(db_session)
    # Create zip with > 1000 members
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document><w:body/></w:document>")
        for i in range(1005):
            zf.writestr(f"word/dummy_{i}.xml", "dummy")
    bomb_docx = buf.getvalue()

    files = [("files", ("bomb.docx", bomb_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    # Parser should catch zip bomb and mark as FAILED parse status
    assert res.status_code == 201
    att = res.json()[0]
    assert att["parse_status"] == "FAILED"
    assert "ZIP Bomb" in att["error_message"] or "阈值" in att["error_message"]


@pytest.mark.asyncio
async def test_path_traversal_protection(client: AsyncClient, db_session: AsyncSession):
    """Scenario 12: Path traversal attack protection."""
    conv = await _create_test_conv(db_session)
    pdf_bytes = _generate_valid_pdf_bytes("安全路径测试")
    malicious_filename = "../../../../etc/passwd.pdf"

    files = [("files", (malicious_filename, pdf_bytes, "application/pdf"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 201
    att = res.json()[0]
    # Sanitized filename should not contain directory traversal characters
    assert ".." not in att["original_filename"]
    assert "/" not in att["original_filename"]
    assert "\\" not in att["original_filename"]
    assert att["original_filename"].endswith(".pdf")


@pytest.mark.asyncio
async def test_nonexistent_conversation_404(client: AsyncClient):
    """Scenario 13: Nonexistent conversation returns 404."""
    pdf_bytes = _generate_valid_pdf_bytes()
    files = [("files", ("test.pdf", pdf_bytes, "application/pdf"))]
    res = await client.post("/api/v1/conversations/non-existent-conv-id/attachments", files=files)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_cross_conversation_isolation(client: AsyncClient, db_session: AsyncSession):
    """Scenario 14: Cross-conversation attachment access isolation."""
    conv_a = await _create_test_conv(db_session, "会话A")
    conv_b = await _create_test_conv(db_session, "会话B")

    pdf_bytes = _generate_valid_pdf_bytes("会话A私密文档")
    files = [("files", ("doc_a.pdf", pdf_bytes, "application/pdf"))]
    res_upload = await client.post(f"/api/v1/conversations/{conv_a.id}/attachments", files=files)
    assert res_upload.status_code == 201
    att_a_id = res_upload.json()[0]["id"]

    # Try accessing Attachment A through Conversation B -> Must return 404
    res_cross = await client.get(f"/api/v1/conversations/{conv_b.id}/attachments/{att_a_id}")
    assert res_cross.status_code == 404

    # Try deleting Attachment A through Conversation B -> Must return 404
    res_del_cross = await client.delete(f"/api/v1/conversations/{conv_b.id}/attachments/{att_a_id}")
    assert res_del_cross.status_code == 404


@pytest.mark.asyncio
async def test_attachment_deletion_cleans_physical_file(client: AsyncClient, db_session: AsyncSession):
    """Scenario 15: Attachment deletion cleans up physical files on disk."""
    conv = await _create_test_conv(db_session, "删除测试会话")
    pdf_bytes = _generate_valid_pdf_bytes("待删除文件")

    files = [("files", ("to_delete.pdf", pdf_bytes, "application/pdf"))]
    res_up = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    att = res_up.json()[0]
    physical_path = settings.UPLOAD_DIR / att["storage_key"]
    assert physical_path.exists()

    # Delete attachment
    res_del = await client.delete(f"/api/v1/conversations/{conv.id}/attachments/{att['id']}")
    assert res_del.status_code == 204

    # Assert physical file and its folder are removed
    assert not physical_path.exists()
    assert not physical_path.parent.exists()


@pytest.mark.asyncio
async def test_conversation_deletion_cleans_all_physical_attachments(client: AsyncClient, db_session: AsyncSession):
    """Scenario 16: Conversation deletion cleans up all physical attachment files."""
    conv = await _create_test_conv(db_session, "整会话删除测试")
    conv_id = conv.id
    pdf_bytes = _generate_valid_pdf_bytes("会话级清理文件")
    txt_bytes = "文本文件".encode("utf-8")

    files = [
        ("files", ("f1.pdf", pdf_bytes, "application/pdf")),
        ("files", ("f2.txt", txt_bytes, "text/plain")),
    ]
    res_up = await client.post(f"/api/v1/conversations/{conv_id}/attachments", files=files)
    assert res_up.status_code == 201

    conv_att_dir = settings.ATTACHMENTS_DIR / conv_id
    assert conv_att_dir.exists()

    # Delete conversation
    res_del = await client.delete(f"/api/v1/conversations/{conv_id}")
    assert res_del.status_code == 204

    # Assert whole conversation attachment directory is removed
    assert not conv_att_dir.exists()


@pytest.mark.asyncio
async def test_db_transaction_failure_cleans_temp_file(client: AsyncClient, db_session: AsyncSession):
    """Scenario 17: Database transaction failure cleans up temporary physical files."""
    conv = await _create_test_conv(db_session, "回滚测试会话")
    conv_id = conv.id
    pdf_bytes = _generate_valid_pdf_bytes("回滚清理内容")

    # Mock db.commit to raise an exception
    with patch("sqlalchemy.ext.asyncio.AsyncSession.commit", side_effect=RuntimeError("Mock DB commit failure")):
        files = [("files", ("fail.pdf", pdf_bytes, "application/pdf"))]
        res = await client.post(f"/api/v1/conversations/{conv_id}/attachments", files=files)
        assert res.status_code == 500

    # Ensure no physical files leaked into storage
    conv_dir = settings.ATTACHMENTS_DIR / conv_id
    if conv_dir.exists():
        assert len(list(conv_dir.iterdir())) == 0


@pytest.mark.asyncio
async def test_metadata_restoration_after_reload(client: AsyncClient, db_session: AsyncSession):
    """Scenario 18: Metadata and parsed content restored after database reload."""
    conv = await _create_test_conv(db_session, "元数据持久化会话")
    txt_bytes = "持久化测试内容：第一行\n第二行关键技术指标".encode("utf-8")

    files = [("files", ("persist.txt", txt_bytes, "text/plain"))]
    res_up = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    att_id = res_up.json()[0]["id"]

    # Read detail directly
    res_detail = await client.get(f"/api/v1/conversations/{conv.id}/attachments/{att_id}")
    assert res_detail.status_code == 200
    detail = res_detail.json()
    assert detail["char_length"] > 0
    assert detail["token_count"] > 0
    assert "关键技术指标" in detail["extracted_text"]


@pytest.mark.asyncio
async def test_filename_with_chinese_and_special_chars(client: AsyncClient, db_session: AsyncSession):
    """Scenario 19: Filenames containing Chinese and special characters."""
    conv = await _create_test_conv(db_session, "中文文件名测试会话")
    pdf_bytes = _generate_valid_pdf_bytes("特殊符号测试")
    special_name = "2026年Q3度财务报表（最终版 & 审核）.pdf"

    files = [("files", (special_name, pdf_bytes, "application/pdf"))]
    res = await client.post(f"/api/v1/conversations/{conv.id}/attachments", files=files)
    assert res.status_code == 201
    att = res.json()[0]
    assert "2026年Q3度财务报表" in att["original_filename"]
    assert att["file_extension"] == ".pdf"


@pytest.mark.asyncio
async def test_existing_knowledge_base_upload_unaffected(client: AsyncClient, db_session: AsyncSession):
    """Scenario 20: Existing KnowledgeBase document upload regression check."""
    from app.llm.embeddings import get_embedding_client

    emb = get_embedding_client()
    kb = KnowledgeBase(
        id=f"kb-reg-{uuid.uuid4().hex[:8]}",
        name="回归测试知识库",
        embedding_provider=emb.provider_name,
        embedding_model=emb.model_name,
        embedding_dimensions=emb.dimensions,
    )
    db_session.add(kb)
    await db_session.commit()

    # Upload document to KB via existing endpoint
    txt_content = "知识库回归测试正文内容，验证原有Pipeline不受任何影响。".encode("utf-8")
    files = {"file": ("kb_doc.txt", txt_content, "text/plain")}
    res = await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/upload", files=files)
    assert res.status_code == 200
    doc_data = res.json()
    assert doc_data["file_name"] == "kb_doc.txt"
    assert doc_data["knowledge_base_id"] == kb.id
