import os
import shutil
import tempfile
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import DocumentChunk
from app.models.document import Document
from app.models.knowledge import KnowledgeBase
from app.retrieval.bm25_retriever import (
    BM25IndexManager,
    BM25KBIndex,
    BM25Retriever,
    TechnicalBM25Tokenizer,
    bm25_index_manager,
)


# ==============================================================================
# Tokenizer Tests
# ==============================================================================


def test_tokenizer_technical_terms():
    """Test 1: Technical compounds, camelCase, acronyms, and parameter names."""
    # 1. CamelCase
    tokens_camel = TechnicalBM25Tokenizer.tokenize("ModelHealthStore")
    assert "modelhealthstore" in tokens_camel
    assert "model" in tokens_camel
    assert "health" in tokens_camel
    assert "store" in tokens_camel

    # 2. Acronyms & state names
    tokens_closed = TechnicalBM25Tokenizer.tokenize("状态机处于 CLOSED 状态")
    assert "closed" in tokens_closed

    # 3. Underscore compound
    tokens_half_open = TechnicalBM25Tokenizer.tokenize("熔断器 HALF_OPEN 切换")
    assert "half_open" in tokens_half_open
    assert "half" in tokens_half_open
    assert "open" in tokens_half_open

    # 4. Hyphenated compound
    tokens_bge = TechnicalBM25Tokenizer.tokenize("采用 BGE-small 嵌入模型")
    assert "bge-small" in tokens_bge
    assert "bge" in tokens_bge
    assert "small" in tokens_bge

    # 5. Parameters & numbers
    tokens_params = TechnicalBM25Tokenizer.tokenize("配置 min_idle=10 以及 timeout_ms=5000ms")
    assert "min_idle" in tokens_params
    assert "min" in tokens_params
    assert "idle" in tokens_params
    assert "timeout_ms" in tokens_params
    assert "10" in tokens_params
    assert "5000" in tokens_params or "5000ms" in tokens_params


def test_tokenizer_cjk_and_stop_words():
    """Test 2: Chinese segmentation and filtering of common stop words and punctuation."""
    raw = "这是一个用于测试的知识库，包含了系统的核心规范与数据字典。"
    tokens = TechnicalBM25Tokenizer.tokenize(raw)
    assert "知识库" in tokens
    assert "系统" in tokens
    assert "核心" in tokens
    assert "规范" in tokens
    # Common stop words should be stripped
    assert "的" not in tokens
    assert "了" not in tokens
    assert "这是" not in tokens or "这" not in tokens
    assert "，" not in tokens
    assert "。" not in tokens


# ==============================================================================
# BM25 Core Algorithm & Positive IDF Tests
# ==============================================================================


def test_bm25_positive_idf_guarantee():
    """
    Test 3: Small corpus (N=2, n=1) must produce strictly positive IDF and non-zero score.
    Robertson-Spärck Jones BM25 produces 0.0 or negative IDF on small corpora;
    our Lucene-style formula ensures positive score for existing terms.
    """
    idx = BM25KBIndex("test_kb")
    corpus = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "document_name": "pool.md",
            "page_number": 1,
            "chunk_index": 0,
            "content": "网关连接池配置：min_idle 最小空闲为 10。",
        },
        {
            "chunk_id": "c2",
            "document_id": "d2",
            "document_name": "breaker.md",
            "page_number": 1,
            "chunk_index": 0,
            "content": "系统三态熔断器包含 OPEN 和 CLOSED 状态。",
        },
    ]
    idx.build_from_chunks(corpus)

    # Search for term present in exactly 1 of 2 documents
    res_pool = idx.search("min_idle", top_k=2)
    assert len(res_pool) == 1
    assert res_pool[0].chunk_id == "c1"
    assert res_pool[0].similarity_score > 0.0

    res_breaker = idx.search("CLOSED", top_k=2)
    assert len(res_breaker) == 1
    assert res_breaker[0].chunk_id == "c2"
    assert res_breaker[0].similarity_score > 0.0


def test_bm25_ranking_and_score():
    """Test 4: More relevant document receives higher BM25 score."""
    idx = BM25KBIndex("test_ranking")
    corpus = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "document_name": "raft.md",
            "page_number": 1,
            "chunk_index": 0,
            "content": "Raft 共识算法：Raft 节点通过选举 Leader 实现强一致性，Raft 日志复制非常高效。",
        },
        {
            "chunk_id": "c2",
            "document_id": "d2",
            "document_name": "overview.md",
            "page_number": 1,
            "chunk_index": 0,
            "content": "系统技术选型概述：数据同步可选 Raft 或 Gossip 协议。",
        },
    ]
    idx.build_from_chunks(corpus)

    results = idx.search("Raft 共识一致性", top_k=2)
    assert len(results) == 2
    # c1 mentions Raft multiple times and has higher relevance
    assert results[0].chunk_id == "c1"
    assert results[0].similarity_score > results[1].similarity_score


def test_bm25_unmatched_query_returns_empty():
    """Test 5: Query with no matching terms returns empty list."""
    idx = BM25KBIndex("test_unmatched")
    idx.build_from_chunks([
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "document_name": "doc.md",
            "page_number": 1,
            "chunk_index": 0,
            "content": "系统架构采用微服务治理。",
        }
    ])
    res = idx.search("quantum_physics_xyz", top_k=4)
    assert res == []


# ==============================================================================
# Database Integration, KB Isolation & Lifecycle Synchronization Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_bm25_retriever_from_database(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 6: BM25Retriever loads and searches chunks stored in SQLite database.
    """
    kb = KnowledgeBase(name="BM25测试知识库")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="gateway_spec.md",
        file_type="markdown",
        file_size=100,
        file_path="/tmp/gateway_spec.md",
        status="COMPLETED",
        chunk_count=2,
    )
    db_session.add(doc)
    await db_session.commit()

    chunk1 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content="网关核心模块：限流组件采用令牌桶算法，保证峰值平滑过载保护。",
        page_number=1,
        char_length=30,
    )
    chunk2 = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=1,
        content="网关连接池配置：min_idle最小空闲为10，max_connections为100。",
        page_number=2,
        char_length=32,
    )
    db_session.add_all([chunk1, chunk2])
    await db_session.commit()

    retriever = BM25Retriever(db_session)
    results = await retriever.search(kb.id, query="令牌桶 限流", top_k=2)

    assert len(results) >= 1
    assert results[0].chunk_id == chunk1.id
    assert results[0].document_name == "gateway_spec.md"
    assert results[0].page_number == 1
    assert results[0].similarity_score > 0.0


@pytest.mark.asyncio
async def test_bm25_knowledge_base_strict_isolation(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 7: Multi-tenant strict KB isolation. Chunks from KB A never leak into KB B.
    """
    kb_a = KnowledgeBase(name="知识库A")
    kb_b = KnowledgeBase(name="知识库B")
    db_session.add_all([kb_a, kb_b])
    await db_session.commit()

    doc_a = Document(
        knowledge_base_id=kb_a.id,
        file_name="secret_a.md",
        file_type="markdown",
        file_size=50,
        file_path="/tmp/secret_a.md",
        status="COMPLETED",
    )
    doc_b = Document(
        knowledge_base_id=kb_b.id,
        file_name="public_b.md",
        file_type="markdown",
        file_size=50,
        file_path="/tmp/public_b.md",
        status="COMPLETED",
    )
    db_session.add_all([doc_a, doc_b])
    await db_session.commit()

    chunk_a = DocumentChunk(
        document_id=doc_a.id,
        knowledge_base_id=kb_a.id,
        content="项目机密关键词：SpecialSecretAlpha999。",
        page_number=1,
    )
    chunk_b = DocumentChunk(
        document_id=doc_b.id,
        knowledge_base_id=kb_b.id,
        content="公开业务指南：普通服务注册发现流程。",
        page_number=1,
    )
    db_session.add_all([chunk_a, chunk_b])
    await db_session.commit()

    # Invalidate cache to force reload
    bm25_index_manager.invalidate(kb_a.id)
    bm25_index_manager.invalidate(kb_b.id)

    retriever = BM25Retriever(db_session)

    # Search in KB B for the keyword from KB A
    res_b = await retriever.search(kb_b.id, query="SpecialSecretAlpha999")
    assert len(res_b) == 0  # Strictly isolated

    # Search in KB A
    res_a = await retriever.search(kb_a.id, query="SpecialSecretAlpha999")
    assert len(res_a) == 1
    assert res_a[0].chunk_id == chunk_a.id


@pytest.mark.asyncio
async def test_bm25_document_addition_and_deletion_sync(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 8: Ingestion and deletion dynamically synchronize BM25 index.
    """
    kb = KnowledgeBase(name="同步测试库")
    db_session.add(kb)
    await db_session.commit()

    # Step 1: Add Doc 1
    doc1 = Document(
        knowledge_base_id=kb.id,
        file_name="doc1.md",
        file_type="markdown",
        file_size=50,
        file_path="/tmp/doc1.md",
        status="COMPLETED",
    )
    db_session.add(doc1)
    await db_session.commit()

    c1 = DocumentChunk(
        document_id=doc1.id,
        knowledge_base_id=kb.id,
        content="微服务熔断器处于 HALF_OPEN 探测状态。",
    )
    db_session.add(c1)
    await db_session.commit()
    bm25_index_manager.invalidate(kb.id)

    retriever = BM25Retriever(db_session)
    res1 = await retriever.search(kb.id, query="HALF_OPEN")
    assert len(res1) == 1

    # Step 2: Add Doc 2
    doc2 = Document(
        knowledge_base_id=kb.id,
        file_name="doc2.md",
        file_type="markdown",
        file_size=50,
        file_path="/tmp/doc2.md",
        status="COMPLETED",
    )
    db_session.add(doc2)
    await db_session.commit()

    c2 = DocumentChunk(
        document_id=doc2.id,
        knowledge_base_id=kb.id,
        content="网关参数配置 min_idle 为 10。",
    )
    db_session.add(c2)
    await db_session.commit()
    bm25_index_manager.invalidate(kb.id)

    res2 = await retriever.search(kb.id, query="min_idle")
    assert len(res2) == 1
    assert res2[0].chunk_id == c2.id

    # Step 3: Delete Doc 1 via API endpoint
    del_resp = await client.delete(f"/api/v1/documents/{doc1.id}")
    assert del_resp.status_code == 204

    # Now HALF_OPEN should not be searchable in this KB
    res_after_del = await retriever.search(kb.id, query="HALF_OPEN")
    assert len(res_after_del) == 0

    # But min_idle should still be present
    res_min_idle = await retriever.search(kb.id, query="min_idle")
    assert len(res_min_idle) == 1


@pytest.mark.asyncio
async def test_bm25_server_restart_persistence_and_recovery(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 9: Server restart recovery. Clearing in-memory index reloads cleanly from disk/SQLite.
    """
    kb = KnowledgeBase(name="重启恢复测试库")
    db_session.add(kb)
    await db_session.commit()

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="persist.md",
        file_type="markdown",
        file_size=50,
        file_path="/tmp/persist.md",
        status="COMPLETED",
    )
    db_session.add(doc)
    await db_session.commit()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="分布式存储引擎采用 LSM-Tree 架构加速写入。",
    )
    db_session.add(chunk)
    await db_session.commit()

    retriever = BM25Retriever(db_session)
    res_before = await retriever.search(kb.id, query="LSM-Tree")
    assert len(res_before) == 1

    # Simulate server restart: clear memory cache
    bm25_index_manager._indices.clear()

    # Re-search: should automatically recover from disk cache or SQLite
    res_after = await retriever.search(kb.id, query="LSM-Tree")
    assert len(res_after) == 1
    assert res_after[0].chunk_id == chunk.id
    assert res_after[0].similarity_score == res_before[0].similarity_score


@pytest.mark.asyncio
async def test_bm25_test_api_endpoint(
    client: AsyncClient, db_session: AsyncSession
):
    """
    Test 10: POST /api/v1/retrieval/bm25-test endpoint works independently.
    """
    kb = KnowledgeBase(name="BM25接口测试库")
    db_session.add(kb)
    await db_session.commit()

    doc = Document(
        knowledge_base_id=kb.id,
        file_name="api_test.md",
        file_type="markdown",
        file_size=50,
        file_path="/tmp/api_test.md",
        status="COMPLETED",
    )
    db_session.add(doc)
    await db_session.commit()

    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        content="系统日志采集组件基于 FluentBit 实现轻量级传输。",
    )
    db_session.add(chunk)
    await db_session.commit()
    bm25_index_manager.invalidate(kb.id)

    resp = await client.post(
        "/api/v1/retrieval/bm25-test",
        json={
            "knowledge_base_id": kb.id,
            "query": "FluentBit 日志采集",
            "top_k": 3,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "FluentBit 日志采集"
    assert data["total_retrieved"] >= 1
    assert data["results"][0]["chunk_id"] == chunk.id
    assert "FluentBit" in data["results"][0]["content"]
    assert data["duration_ms"] >= 0.0
