import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.workflow import WorkflowRAGEngine
from app.schemas.chat import ChatRequest


@pytest.mark.asyncio
async def test_workflow_rag_answer_and_citation(db_session: AsyncSession):
    # 1. Setup KB
    kb = KnowledgeBase(name="企业问答库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="公司考勤规范.pdf",
        file_type="pdf",
        file_size=2048,
        file_path="/tmp/company_rules.pdf",
        status="COMPLETED",
        chunk_count=1,
        page_count=3,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    emb_client = get_embedding_client("test_double")
    content = "弹性工作时间要求：员工应于每日上午 09:00 至 10:00 之间到岗，并在每日工作满 8 小时后可离岗打卡。"
    emb = await emb_client.embed_documents([content])

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content=content,
        page_number=2,
        char_length=len(content),
    )
    chunk.embedding = emb[0]
    db_session.add(chunk)
    await db_session.commit()

    # 2. Run Workflow RAG
    engine = WorkflowRAGEngine(db_session)
    req = ChatRequest(
        knowledge_base_id=kb.id,
        query="公司的弹性上班到岗时间是几点？",
        top_k=2,
    )
    resp = await engine.execute(req)

    assert resp.answer != ""
    assert len(resp.citations) == 1
    cit = resp.citations[0]
    assert cit.document_name == "公司考勤规范.pdf"
    assert cit.page_number == 2
    assert cit.similarity_score > 0.0
    assert "弹性工作时间要求" in cit.content_snippet
    assert resp.execution_info.total_duration_ms > 0
    assert resp.conversation_id is not None
