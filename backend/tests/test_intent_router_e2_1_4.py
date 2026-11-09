"""
Unit test suite for IntentRouter fixes in AgentStudio-RAG E2.1.4.

Verifies:
1. Specified KB + HTTP -> KNOWLEDGE_BASE (Workflow RAG)
2. Specified KB + JWT -> KNOWLEDGE_BASE (Workflow RAG)
3. Specified KB + Python Decorator -> KNOWLEDGE_BASE (Workflow RAG)
4. Specified KB + Quicksort Algorithm -> KNOWLEDGE_BASE (Workflow RAG)
5. Unspecified KB + Python Decorator -> DIRECT_LLM (Direct Chat)
6. Unspecified KB + Quicksort Algorithm -> DIRECT_LLM (Direct Chat)
7. Specified KB + 并发 -> KNOWLEDGE_BASE (Workflow RAG)
8. Specified KB + PDF -> KNOWLEDGE_BASE (Workflow RAG, not degraded as missing attachment)
9. Unspecified KB + Chitchat -> DIRECT_LLM (Direct Chat)
10. Explicit Direct Chat Mode (requested_mode='direct') -> DIRECT_LLM
11. Unspecified KB + Missing Attachment -> DIRECT_LLM (Graceful degradation with prompt)
12. GEN-26, GEN-30 are not diverted by ATT_PATTERNS
13. All 30 Benchmark generation questions route to KNOWLEDGE_BASE when KB is bound
"""

import json
from pathlib import Path
import pytest

from app.routing import IntentRouter, ExecutionPath, UserIntent, get_intent_router


@pytest.fixture
def router() -> IntentRouter:
    return get_intent_router()


def test_specified_kb_with_http(router: IntentRouter):
    """1. 已选择知识库 + HTTP 问题，必须走 RAG."""
    query = "在微服务网关架构中，HTTP 504 错误与三态熔断器是如何协同处理上游超时的？"
    decision = router.route(
        query=query,
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert decision.intent == UserIntent.KNOWLEDGE_BASE
    assert decision.should_retrieve is True
    assert "knowledge_base" in decision.required_sources


def test_specified_kb_with_jwt(router: IntentRouter):
    """2. 已选择知识库 + JWT 问题，必须走 RAG."""
    query = "微服务网关中 JWT 令牌的校验签名算法是什么，公钥缓存 TTL 是多长时间？"
    decision = router.route(
        query=query,
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert decision.intent == UserIntent.KNOWLEDGE_BASE
    assert decision.should_retrieve is True
    assert "knowledge_base" in decision.required_sources


def test_specified_kb_with_python_decorator(router: IntentRouter):
    """3. 已选择知识库 + Python 装饰器，必须走 RAG."""
    query = "Python 装饰器是什么？"
    decision = router.route(
        query=query,
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert decision.intent == UserIntent.KNOWLEDGE_BASE
    assert decision.should_retrieve is True
    assert "knowledge_base" in decision.required_sources


def test_specified_kb_with_quicksort(router: IntentRouter):
    """4. 已选择知识库 + 快速排序，必须走 RAG."""
    query = "快速排序的时间复杂度与稳定性分析"
    decision = router.route(
        query=query,
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert decision.intent == UserIntent.KNOWLEDGE_BASE
    assert decision.should_retrieve is True
    assert "knowledge_base" in decision.required_sources


def test_unspecified_kb_with_python_decorator(router: IntentRouter):
    """5. 未选择知识库 + Python 装饰器，保持 Direct Chat."""
    query = "Python 装饰器是什么？"
    decision = router.route(
        query=query,
        has_knowledge_base=False,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.DIRECT_LLM
    assert decision.intent == UserIntent.DIRECT_CHAT
    assert decision.should_retrieve is False


def test_unspecified_kb_with_quicksort(router: IntentRouter):
    """未选择知识库 + 快速排序，保持 Direct Chat."""
    query = "快速排序的时间复杂度与稳定性分析"
    decision = router.route(
        query=query,
        has_knowledge_base=False,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.DIRECT_LLM
    assert decision.intent == UserIntent.DIRECT_CHAT
    assert decision.should_retrieve is False


def test_specified_kb_with_concurrency(router: IntentRouter):
    """已选择知识库 + 并发，必须走 RAG."""
    query = "微服务网关在高并发场景下如何联动异步连接池与三态熔断器？"
    decision = router.route(
        query=query,
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert decision.intent == UserIntent.KNOWLEDGE_BASE
    assert decision.should_retrieve is True
    assert "knowledge_base" in decision.required_sources


def test_specified_kb_with_pdf(router: IntentRouter):
    """已选择知识库 + PDF，必须走 RAG (不得被 ATT_PATTERNS 错误分流)."""
    query = "微服务网关处理扫描件 PDF 时，是否会集成商业 OCR 引擎自动提取公章与手写签名？"
    decision = router.route(
        query=query,
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert decision.intent == UserIntent.KNOWLEDGE_BASE
    assert decision.should_retrieve is True
    assert "knowledge_base" in decision.required_sources


def test_unspecified_kb_chitchat(router: IntentRouter):
    """未指定知识库闲聊，保持 Direct Chat."""
    for q in ["你好", "您好呀", "hello", "早上好", "你是谁", "介绍一下你自己", "谢谢你"]:
        decision = router.route(
            query=q,
            has_knowledge_base=False,
            has_completed_attachments=False,
            requested_mode="workflow",
        )
        assert decision.execution_path == ExecutionPath.DIRECT_LLM
        assert decision.intent == UserIntent.DIRECT_CHAT
        assert decision.should_retrieve is False


def test_explicit_direct_chat_mode(router: IntentRouter):
    """6. 显式 Direct Chat (mode='direct')，保持 Direct Chat."""
    # With KB
    decision_kb = router.route(
        query="微服务网关中 JWT 令牌的校验签名算法是什么？",
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="direct",
    )
    assert decision_kb.execution_path == ExecutionPath.DIRECT_LLM
    assert decision_kb.intent == UserIntent.DIRECT_CHAT
    assert decision_kb.should_retrieve is False

    # Without KB
    decision_no_kb = router.route(
        query="Python 装饰器是什么？",
        has_knowledge_base=False,
        has_completed_attachments=False,
        requested_mode="direct",
    )
    assert decision_no_kb.execution_path == ExecutionPath.DIRECT_LLM
    assert decision_no_kb.intent == UserIntent.DIRECT_CHAT
    assert decision_no_kb.should_retrieve is False


def test_unspecified_kb_missing_attachment_degrade(router: IntentRouter):
    """未选择知识库且缺少附件时，友好提示补充附件，不盲目调用大模型编造."""
    query = "请总结我上传的 PDF 文件中的核心参数配置"
    decision = router.route(
        query=query,
        has_knowledge_base=False,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert decision.execution_path == ExecutionPath.DIRECT_LLM
    assert decision.intent == UserIntent.DIRECT_CHAT
    assert decision.should_retrieve is False
    assert "未上传可用附件" in decision.reason


def test_gen26_gen30_not_diverted_by_att_patterns(router: IntentRouter):
    """7. GEN-26、GEN-30 不得被 ATT_PATTERNS 错误分流."""
    # GEN-26 (contains 审计日志)
    d26 = router.route(
        query="企业超过 180 天的冷备份审计日志是否统一归档在 ClickHouse 或 Snowflake 分布式列式数仓中？",
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert d26.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert d26.should_retrieve is True

    # GEN-30 (contains 扫描件 PDF)
    d30 = router.route(
        query="知识库在解析财务报销发票或合同扫描件 PDF 时，是否集成了商业 OCR 服务来提取手写签名？",
        has_knowledge_base=True,
        has_completed_attachments=False,
        requested_mode="workflow",
    )
    assert d30.execution_path == ExecutionPath.KNOWLEDGE_BASE
    assert d30.should_retrieve is True


def test_all_30_benchmark_questions_route_to_kb(router: IntentRouter):
    """Verify that all 30 questions in Benchmark Generation route to KNOWLEDGE_BASE when KB is bound."""
    dataset_path = Path("eval/datasets/benchmark_generation_v1.jsonl")
    assert dataset_path.exists(), "benchmark dataset does not exist"

    with open(dataset_path, "r", encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]

    assert len(items) == 30, f"Expected 30 questions, got {len(items)}"

    for item in items:
        decision = router.route(
            query=item["query"],
            has_knowledge_base=True,
            has_completed_attachments=False,
            requested_mode="workflow",
        )
        assert decision.execution_path == ExecutionPath.KNOWLEDGE_BASE, (
            f"Question {item['id']} ({item['query']}) unexpectedly routed to "
            f"{decision.execution_path} (reason: {decision.reason})"
        )
        assert decision.should_retrieve is True
