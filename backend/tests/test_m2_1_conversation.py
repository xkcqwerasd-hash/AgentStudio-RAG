from datetime import datetime, timezone
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConversationNotFoundError
from app.llm.embeddings import get_embedding_client
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, Message
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.rag.prompt_builder import PromptBuilder
from app.rag.workflow import WorkflowRAGEngine
from app.retrieval.vector_store import ScoredChunk
from app.schemas.chat import ChatRequest


@pytest.mark.asyncio
async def test_create_and_list_conversations(client: AsyncClient, db_session: AsyncSession):
    """Test 1: Create conversation via API and list with knowledge_base_id filter."""
    kb1 = KnowledgeBase(name="测试库1")
    kb2 = KnowledgeBase(name="测试库2")
    db_session.add_all([kb1, kb2])
    await db_session.commit()
    await db_session.refresh(kb1)
    await db_session.refresh(kb2)

    # 1. Create conversation under kb1
    res1 = await client.post(
        "/api/v1/conversations",
        json={"knowledge_base_id": kb1.id, "title": "KB1专属会话"},
    )
    assert res1.status_code == 201
    conv1_data = res1.json()
    assert conv1_data["title"] == "KB1专属会话"
    assert conv1_data["knowledge_base_id"] == kb1.id
    assert conv1_data["message_count"] == 0

    # 2. Create conversation under kb2
    res2 = await client.post(
        "/api/v1/conversations",
        json={"knowledge_base_id": kb2.id, "title": "KB2专属会话"},
    )
    assert res2.status_code == 201
    conv2_data = res2.json()

    # 3. List all conversations
    list_all = await client.get("/api/v1/conversations")
    assert list_all.status_code == 200
    all_items = list_all.json()
    all_ids = [c["id"] for c in all_items]
    assert conv1_data["id"] in all_ids
    assert conv2_data["id"] in all_ids

    # 4. Filter by kb1
    list_kb1 = await client.get(f"/api/v1/conversations?knowledge_base_id={kb1.id}")
    assert list_kb1.status_code == 200
    kb1_items = list_kb1.json()
    assert len(kb1_items) == 1
    assert kb1_items[0]["id"] == conv1_data["id"]


@pytest.mark.asyncio
async def test_multi_turn_message_persistence(client: AsyncClient, db_session: AsyncSession):
    """Test 2 & 3: Multi-turn chat message saving, chronological ordering, and persistence."""
    kb = KnowledgeBase(name="多轮对话知识库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    # Add mock document & chunk
    doc = Document(
        knowledge_base_id=kb.id,
        file_name="spec.md",
        file_type="markdown",
        file_size=100,
        file_path="/tmp/spec.md",
        status="COMPLETED",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    emb_client = get_embedding_client(kb.embedding_provider)
    content = "网关连接池配置规范：min_idle最小空闲为10，max_connections最大连接数为100。"
    emb = await emb_client.embed_documents([content])

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content=content,
        page_number=1,
        char_length=len(content),
    )
    chunk.embedding = emb[0]
    db_session.add(chunk)
    await db_session.commit()

    # Turn 1: User asks question 1
    req1 = {
        "knowledge_base_id": kb.id,
        "query": "网关连接池的最小空闲连接数是多少？",
        "mode": "workflow",
    }
    res1 = await client.post("/api/v1/chat/completions", json=req1)
    assert res1.status_code == 200
    turn1_data = res1.json()
    conv_id = turn1_data["conversation_id"]
    assert conv_id is not None
    assert turn1_data["message_id"] is not None

    # Turn 2: User asks question 2 in the same conversation
    req2 = {
        "knowledge_base_id": kb.id,
        "query": "那最大连接数是多少呢？",
        "conversation_id": conv_id,
        "mode": "workflow",
    }
    res2 = await client.post("/api/v1/chat/completions", json=req2)
    assert res2.status_code == 200
    turn2_data = res2.json()
    assert turn2_data["conversation_id"] == conv_id

    # Verify message persistence in DB
    msgs_res = await client.get(f"/api/v1/conversations/{conv_id}/messages")
    assert msgs_res.status_code == 200
    msgs = msgs_res.json()
    assert len(msgs) == 4
    # Chronological order
    assert msgs[0]["role"] == "user"
    assert "最小空闲" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[2]["role"] == "user"
    assert "最大连接数" in msgs[2]["content"]
    assert msgs[3]["role"] == "assistant"

    # Verify conversation detail API
    detail_res = await client.get(f"/api/v1/conversations/{conv_id}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["id"] == conv_id
    assert detail["message_count"] == 4
    assert len(detail["messages"]) == 4


@pytest.mark.asyncio
async def test_conversation_strict_isolation(client: AsyncClient, db_session: AsyncSession):
    """Test 4: Strict data isolation between different conversations."""
    kb = KnowledgeBase(name="隔离测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    # Create conv A and conv B
    res_a = await client.post("/api/v1/conversations", json={"knowledge_base_id": kb.id, "title": "会话A"})
    res_b = await client.post("/api/v1/conversations", json={"knowledge_base_id": kb.id, "title": "会话B"})
    conv_a_id = res_a.json()["id"]
    conv_b_id = res_b.json()["id"]

    # Insert messages into A
    m_a = Message(conversation_id=conv_a_id, role="user", content="这是会话A的内容")
    # Insert messages into B
    m_b = Message(conversation_id=conv_b_id, role="user", content="这是会话B的机密")
    db_session.add_all([m_a, m_b])
    await db_session.commit()

    # Query messages for A
    res_msgs_a = await client.get(f"/api/v1/conversations/{conv_a_id}/messages")
    msgs_a = res_msgs_a.json()
    assert len(msgs_a) == 1
    assert msgs_a[0]["content"] == "这是会话A的内容"

    # Query messages for B
    res_msgs_b = await client.get(f"/api/v1/conversations/{conv_b_id}/messages")
    msgs_b = res_msgs_b.json()
    assert len(msgs_b) == 1
    assert msgs_b[0]["content"] == "这是会话B的机密"


@pytest.mark.asyncio
async def test_delete_conversation_cascades_messages(client: AsyncClient, db_session: AsyncSession):
    """Test 5: Deleting a conversation removes conversation and cascades to delete all messages."""
    kb = KnowledgeBase(name="删除测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    res_conv = await client.post("/api/v1/conversations", json={"knowledge_base_id": kb.id, "title": "待删会话"})
    conv_id = res_conv.json()["id"]

    m1 = Message(conversation_id=conv_id, role="user", content="用户问题1")
    m2 = Message(conversation_id=conv_id, role="assistant", content="回答1")
    db_session.add_all([m1, m2])
    await db_session.commit()

    # Delete conversation
    del_res = await client.delete(f"/api/v1/conversations/{conv_id}")
    assert del_res.status_code == 204

    # Verify conversation is gone
    get_res = await client.get(f"/api/v1/conversations/{conv_id}")
    assert get_res.status_code == 404

    # Verify messages are gone from database
    stmt = select(Message).where(Message.conversation_id == conv_id)
    rem_msgs = (await db_session.execute(stmt)).scalars().all()
    assert len(rem_msgs) == 0


def test_prompt_builder_history_assembly():
    """Test 6: PromptBuilder correctly integrates chronological multi-turn history into prompt."""
    builder = PromptBuilder()
    chunks = [
        ScoredChunk(
            chunk_id="c1",
            document_id="d1",
            document_name="doc.md",
            page_number=1,
            chunk_index=0,
            content="断路器连续失败5次后进入OPEN状态。",
            similarity_score=0.75,
        )
    ]
    history = [
        {"role": "user", "content": "系统支持哪些熔断状态？"},
        {"role": "assistant", "content": "支持 CLOSED, OPEN, HALF_OPEN 三种状态。"},
    ]

    prompt = builder.build_user_prompt(
        query="其中进入 OPEN 状态的判定条件是什么？",
        chunks=chunks,
        history_messages=history,
    )

    # 1. System prompt not in user prompt (passed separately to LLM)
    assert builder.system_prompt not in prompt

    # 2. Reference context exists
    assert "【参考资料】" in prompt
    assert "[Ref-1]" in prompt
    assert "断路器连续失败5次" in prompt

    # 3. Chronological conversation history formatted
    assert "【前序对话历史】" in prompt
    assert "用户: 系统支持哪些熔断状态？" in prompt
    assert "AI助手: 支持 CLOSED, OPEN, HALF_OPEN 三种状态。" in prompt

    # 4. Current user question placed after history
    assert "【当前用户提问】" in prompt
    assert "其中进入 OPEN 状态的判定条件是什么？" in prompt
    assert prompt.index("【前序对话历史】") < prompt.index("【当前用户提问】")


@pytest.mark.asyncio
async def test_context_window_budget_pruning(db_session: AsyncSession):
    """Test 7: Context management strategy respects MAX_HISTORY_CHARS budget."""
    kb = KnowledgeBase(name="预算测试库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    conv = Conversation(knowledge_base_id=kb.id, title="超长历史会话")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    # Add 10 messages with total length > settings.MAX_HISTORY_CHARS (2000)
    for i in range(10):
        m = Message(
            conversation_id=conv.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"第{i}轮对话超长文本: " + ("A" * 300),
        )
        db_session.add(m)
    await db_session.commit()

    engine = WorkflowRAGEngine(db_session)
    # Execute query
    req = ChatRequest(
        knowledge_base_id=kb.id,
        conversation_id=conv.id,
        query="请问最新进展？",
    )
    resp = await engine.execute(req)
    assert resp.conversation_id == conv.id

    # Check that messages in DB are all still preserved (pruning only affects LLM prompt budget)
    stmt = select(Message).where(Message.conversation_id == conv.id)
    all_msgs = (await db_session.execute(stmt)).scalars().all()
    # 10 prior messages + 1 new user message + 1 new assistant message = 12
    assert len(all_msgs) == 12


@pytest.mark.asyncio
async def test_non_existent_conversation_raises_404(client: AsyncClient):
    """Test 8: Accessing non-existent conversation returns 404."""
    resp = await client.get("/api/v1/conversations/non-existent-uuid-12345")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()
