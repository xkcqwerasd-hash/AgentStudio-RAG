import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.vector_store import VectorRetriever


@pytest.mark.asyncio
async def test_vector_retriever_ranking(db_session: AsyncSession):
    # 1. Create KB & Doc
    kb = KnowledgeBase(name="检索测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="test_doc.md",
        file_type="markdown",
        file_size=100,
        file_path="/tmp/test_doc.md",
        status="COMPLETED",
        chunk_count=3,
        page_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    embedding_client = get_embedding_client("test_double")

    # Three different contents
    contents = [
        "数据库连接池采用 HikariCP，最小空闲连接数为 5，最大连接数为 20。",
        "前端采用 React 18 与 TypeScript 构建，组件库使用 Ant Design。",
        "大模型服务熔断器包含 CLOSED、OPEN、HALF_OPEN 三种状态，防止雪崩。",
    ]
    embeddings = await embedding_client.embed_documents(contents)

    for i, (text, emb) in enumerate(zip(contents, embeddings)):
        c = DocumentChunk(
            document_id=doc.id,
            knowledge_base_id=kb.id,
            chunk_index=i,
            content=text,
            page_number=1,
            char_length=len(text),
        )
        c.embedding = emb
        db_session.add(c)
    await db_session.commit()

    # Query for database
    q_vec = await embedding_client.embed_query("数据库连接池最大连接数是多少？")
    retriever = VectorRetriever(db_session)
    results = await retriever.search(kb.id, q_vec, top_k=2)

    assert len(results) == 2
    # The top result should be the database connection pool chunk
    assert "HikariCP" in results[0].content
    assert results[0].similarity_score > results[1].similarity_score
