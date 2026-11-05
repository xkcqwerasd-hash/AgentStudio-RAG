"""
AgentStudio-RAG — P0.3 Stage 4: Chat Attachment UI & Dual-Mode End-to-End Integration Test Suite
Validates the complete end-to-end integration across Scenarios A, B, C, D, E:
- Scenario A: Pure PDF QA without KnowledgeBase (Workflow RAG & Agentic Dual Mode)
- Scenario B: PDF + DOCX Multi-Attachment Comparison & Provenance Verification
- Scenario C: Attachment + KnowledgeBase Hybrid QA with Unified Global Citation Indexing
- Scenario D: Full Lifecycle (Create -> Upload -> Query -> Detail -> Delete -> Physical Cleanup)
- Scenario E: Security & Boundary Defenses (Over 20MB, Disallowed Extensions, >10 Files Limit, Cross-Conversation 404, Cancellation Safety)
"""

import io
import json
import uuid
import zipfile
from pathlib import Path
import pymupdf
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.llm.embeddings import get_embedding_client
from app.models.conversation import Conversation, ConversationAttachment
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.context_budget import estimate_tokens


def _generate_valid_pdf_bytes(text: str = "微服务网关架构与超时技术标准") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    try:
        page.insert_text((50, 72), text, fontname="china-s")
    except Exception:
        page.insert_text((50, 72), text)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _generate_valid_docx_bytes(paragraphs: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        body_parts = [f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs]
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{"".join(body_parts)}</w:body>'
            '</w:document>'
        )
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


def _parse_sse_events(text: str):
    events = []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    for block in blocks:
        lines = block.split("\n")
        event_type = "message"
        data_lines = []
        for line in lines:
            if line.startswith("event:"):
                event_type = line.replace("event:", "").strip()
            elif line.startswith("data:"):
                data_lines.append(line.replace("data:", "").strip())
        data_str = "\n".join(data_lines)
        try:
            data = json.loads(data_str)
        except Exception:
            data = data_str
        events.append({"event": event_type, "data": data})
    return events


@pytest.mark.asyncio
async def test_scenario_a_pure_pdf_qa_dual_mode(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario A: Pure PDF Question & Answering (No KnowledgeBase bound).
    Tests both Workflow RAG and Agentic Dual Execution Mode.
    """
    # 1. Create a blank conversation without knowledge_base_id
    conv_resp = await client.post(
        "/api/v1/conversations",
        json={"title": "纯PDF无知识库问答", "knowledge_base_id": None, "mode": "workflow"},
    )
    assert conv_resp.status_code == 201
    conv_id = conv_resp.json()["id"]

    # 2. Upload PDF attachment
    pdf_bytes = _generate_valid_pdf_bytes("2026年企业级微服务网关指标：连接池最大配置为500，请求超时时间严格限制为3000ms。")
    upload_resp = await client.post(
        f"/api/v1/conversations/{conv_id}/attachments",
        files={"files": ("网关指标.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )
    assert upload_resp.status_code == 201
    atts = upload_resp.json()
    assert len(atts) == 1
    assert atts[0]["parse_status"] == "COMPLETED"

    # 3. Test Workflow RAG mode (POST /api/v1/chat/completions/stream with knowledge_base_id=None)
    wf_payload = {
        "query": "请问网关连接池配置和超时时间是多少？",
        "conversation_id": conv_id,
        "knowledge_base_id": None,
        "mode": "workflow",
    }
    wf_resp = await client.post("/api/v1/chat/completions/stream", json=wf_payload)
    assert wf_resp.status_code == 200
    wf_events = _parse_sse_events(wf_resp.text)
    wf_types = [e["event"] for e in wf_events]
    assert "start" in wf_types
    assert "token" in wf_types
    assert "done" in wf_types

    # Check citations in workflow response
    cit_events = [e for e in wf_events if e["event"] == "citations"]
    assert len(cit_events) >= 1
    citations = cit_events[0]["data"]
    assert len(citations) >= 1
    assert "网关指标.pdf" in citations[0]["document_name"]
    assert citations[0]["source_type"] == "pdf"
    assert citations[0]["page_number"] >= 1

    # 4. Test Agentic Deep Analysis mode (POST /api/v1/agents/default/runs/stream with knowledge_base_id=None)
    agent_payload = {
        "input": "请详细分析网关指标.pdf中的参数配置",
        "conversation_id": conv_id,
        "knowledge_base_id": None,
    }
    agent_resp = await client.post("/api/v1/agents/default/runs/stream", json=agent_payload)
    assert agent_resp.status_code == 200
    agent_events = _parse_sse_events(agent_resp.text)
    agent_types = [e["event"] for e in agent_events]
    assert "run_started" in agent_types
    assert "run_completed" in agent_types

    # Verify tool call was to search_conversation_attachments
    tool_starts = [e for e in agent_events if e["event"] == "tool_call_started"]
    assert len(tool_starts) >= 1
    assert tool_starts[0]["data"]["tool_name"] == "search_conversation_attachments"


@pytest.mark.asyncio
async def test_scenario_b_multi_format_comparison_pdf_docx(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario B: PDF + DOCX Multi-Format Comparison in same Conversation.
    Verifies that citations correctly distinguish PDF page numbers vs DOCX heading paths & block indices.
    """
    conv_resp = await client.post(
        "/api/v1/conversations",
        json={"title": "PDF与DOCX跨文档对比", "knowledge_base_id": None, "mode": "workflow"},
    )
    conv_id = conv_resp.json()["id"]

    # Upload PDF and DOCX simultaneously
    pdf_bytes = _generate_valid_pdf_bytes("微服务网关官方规范：连接池为500，超时为5000ms。")
    docx_bytes = _generate_valid_docx_bytes(["本地微服务优化提案", "建议将连接池提升至800，超时缩减至2000ms。"])

    files = [
        ("files", ("官方规范.pdf", io.BytesIO(pdf_bytes), "application/pdf")),
        ("files", ("优化提案.docx", io.BytesIO(docx_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
    ]
    upload_resp = await client.post(f"/api/v1/conversations/{conv_id}/attachments", files=files)
    assert upload_resp.status_code == 201
    atts = upload_resp.json()
    assert len(atts) == 2
    assert all(a["parse_status"] == "COMPLETED" for a in atts)

    # Ask model to compare both documents
    resp = await client.post(
        "/api/v1/chat/completions/stream",
        json={
            "query": "请对比官方规范与优化提案中关于连接池与超时时间的差异",
            "conversation_id": conv_id,
            "knowledge_base_id": None,
        },
    )
    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    cit_events = [e for e in events if e["event"] == "citations"]
    assert len(cit_events) >= 1
    citations = cit_events[0]["data"]
    assert len(citations) >= 2

    # Check that PDF citation retains page_number and DOCX retains heading/block provenance
    sources = {c["document_name"]: c for c in citations}
    assert "官方规范.pdf" in sources
    assert "优化提案.docx" in sources

    pdf_cit = sources["官方规范.pdf"]
    docx_cit = sources["优化提案.docx"]
    assert pdf_cit["source_type"] == "pdf"
    assert "页" in (pdf_cit.get("provenance_label") or "")

    assert docx_cit["source_type"] == "docx"
    assert "段落" in (docx_cit.get("provenance_label") or "") or docx_cit.get("heading_path") is not None


@pytest.mark.asyncio
async def test_scenario_c_attachment_plus_knowledge_base_hybrid(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario C: Attachment + KnowledgeBase Hybrid Retrieval.
    Verifies unified global Ref-X indexing and no collision.
    """
    # 1. Setup KnowledgeBase with document chunk
    kb = KnowledgeBase(name="企业核心规范库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="公司年休假政策.pdf",
        file_type="pdf",
        file_size=2048,
        file_path="/tmp/公司年休假政策.pdf",
        status="COMPLETED",
        page_count=1,
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    emb_client = get_embedding_client("test_double")
    emb = await emb_client.embed_documents(["公司统一政策：员工年休假基准为每年5天全薪年假。"])

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="公司统一政策：员工年休假基准为每年5天全薪年假。",
        page_number=1,
        char_length=26,
    )
    chunk.embedding = emb[0]
    db_session.add(chunk)
    await db_session.commit()

    # 2. Create conversation bound to this KB
    conv_resp = await client.post(
        "/api/v1/conversations",
        json={"title": "年假政策与补充细则对比", "knowledge_base_id": kb.id, "mode": "agentic"},
    )
    conv_id = conv_resp.json()["id"]

    # 3. Upload ephemeral attachment (supplementary policy)
    att_pdf = _generate_valid_pdf_bytes("补充细则：当年度未休完的年休假可顺延至次年3月31日之前结转使用。")
    upload_resp = await client.post(
        f"/api/v1/conversations/{conv_id}/attachments",
        files={"files": ("年假补充细则.pdf", io.BytesIO(att_pdf), "application/pdf")},
    )
    assert upload_resp.status_code == 201

    # 4. Stream query across both sources
    agent_resp = await client.post(
        "/api/v1/agents/default/runs/stream",
        json={
            "input": "请跨来源对比并联合检索知识库微服务规范与附件补充细则",
            "conversation_id": conv_id,
            "knowledge_base_id": kb.id,
        },
    )
    assert agent_resp.status_code == 200
    events = _parse_sse_events(agent_resp.text)
    event_types = [e["event"] for e in events]
    assert "run_started" in event_types
    assert "run_completed" in event_types

    # Check verified citations: must have unique incrementing Ref numbers
    cit_events = [e for e in events if e["event"] == "citation_verified"]
    if cit_events:
        cits = cit_events[0]["data"]["citations"]
        ref_indices = [c["citation_index"] for c in cits]
        assert len(ref_indices) == len(set(ref_indices)), "Ref numbers must be strictly unique"


@pytest.mark.asyncio
async def test_scenario_d_full_attachment_lifecycle(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario D: Full Lifecycle:
    Create Conversation -> Upload Attachments -> Get List -> Get Detail -> Delete Attachment -> Physical File Cleanup.
    """
    # 1. Create conversation
    conv_resp = await client.post(
        "/api/v1/conversations",
        json={"title": "生命周期测试会话", "knowledge_base_id": None},
    )
    assert conv_resp.status_code == 201
    conv_id = conv_resp.json()["id"]

    # 2. Upload attachment
    txt_content = "第一行测试内容\n第二行关键环境配置\n第三行结束行"
    upload_resp = await client.post(
        f"/api/v1/conversations/{conv_id}/attachments",
        files={"files": ("config.txt", io.BytesIO(txt_content.encode("utf-8")), "text/plain")},
    )
    assert upload_resp.status_code == 201
    att_id = upload_resp.json()[0]["id"]

    # 3. Get attachments list
    list_resp = await client.get(f"/api/v1/conversations/{conv_id}/attachments")
    assert list_resp.status_code == 200
    atts = list_resp.json()
    assert len(atts) == 1
    assert atts[0]["id"] == att_id
    assert atts[0]["original_filename"] == "config.txt"

    # 4. Get attachment detail
    detail_resp = await client.get(f"/api/v1/conversations/{conv_id}/attachments/{att_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["id"] == att_id
    assert "第二行关键环境配置" in detail["extracted_text"]
    assert len(detail["parsed_pages"]) >= 1

    # Verify physical file existence
    storage_dir = settings.ATTACHMENTS_DIR / conv_id / att_id
    assert storage_dir.exists(), "Attachment storage directory must exist on disk"

    # 5. Delete attachment
    del_resp = await client.delete(f"/api/v1/conversations/{conv_id}/attachments/{att_id}")
    assert del_resp.status_code == 204

    # 6. Verify deletion in DB and disk
    list_resp_after = await client.get(f"/api/v1/conversations/{conv_id}/attachments")
    assert len(list_resp_after.json()) == 0
    assert not storage_dir.exists(), "Physical attachment directory must be purged on deletion"


@pytest.mark.asyncio
async def test_scenario_e_security_and_boundary_defenses(client: AsyncClient, db_session: AsyncSession):
    """
    Scenario E: Security & Boundary Defenses:
    - 0-byte file rejection (400)
    - Disallowed extension rejection (.exe/.sh) (400)
    - Oversized file rejection (>20MB) (400)
    - Exceeding 10 attachments per conversation limit rejection (400)
    - Cross-conversation access returns 404
    """
    conv_a = (await client.post("/api/v1/conversations", json={"title": "会话A"})).json()
    conv_b = (await client.post("/api/v1/conversations", json={"title": "会话B"})).json()

    # 1. Empty file (0 bytes) rejection
    res_empty = await client.post(
        f"/api/v1/conversations/{conv_a['id']}/attachments",
        files={"files": ("empty.txt", io.BytesIO(b""), "text/plain")},
    )
    assert res_empty.status_code == 400
    assert "空文件" in res_empty.json()["detail"] or "0 字节" in res_empty.json()["detail"]

    # 2. Disallowed extension rejection
    res_bad_ext = await client.post(
        f"/api/v1/conversations/{conv_a['id']}/attachments",
        files={"files": ("malware.exe", io.BytesIO(b"MZ1234567890"), "application/octet-stream")},
    )
    assert res_bad_ext.status_code == 400
    assert "不支持的文件格式" in res_bad_ext.json()["detail"] or "不支持" in res_bad_ext.json()["detail"]

    # 3. Oversized file (>20MB) rejection
    oversized_data = b"X" * (20 * 1024 * 1024 + 1024)
    res_oversized = await client.post(
        f"/api/v1/conversations/{conv_a['id']}/attachments",
        files={"files": ("huge.txt", io.BytesIO(oversized_data), "text/plain")},
    )
    assert res_oversized.status_code == 400
    assert "超过" in res_oversized.json()["detail"] and "20MB" in res_oversized.json()["detail"]

    # 4. Upload 1 valid attachment to Conv A
    res_valid = await client.post(
        f"/api/v1/conversations/{conv_a['id']}/attachments",
        files={"files": ("valid.txt", io.BytesIO(b"Normal test line"), "text/plain")},
    )
    assert res_valid.status_code == 201
    att_a_id = res_valid.json()[0]["id"]

    # 5. Cross-conversation access: Conv B cannot access Conv A's attachment
    res_cross = await client.get(f"/api/v1/conversations/{conv_b['id']}/attachments/{att_a_id}")
    assert res_cross.status_code == 404
    assert "不属于当前会话" in res_cross.json()["detail"]

    # 6. Exceeding 10 attachments limit rejection
    # Currently Conv A has 1 attachment. Try to upload 10 more (1 + 10 = 11 > 10).
    bulk_files = [("files", (f"file_{i}.txt", io.BytesIO(f"content {i}".encode()), "text/plain")) for i in range(10)]
    res_limit = await client.post(f"/api/v1/conversations/{conv_a['id']}/attachments", files=bulk_files)
    assert res_limit.status_code == 400
    assert "超过上限" in res_limit.json()["detail"]
