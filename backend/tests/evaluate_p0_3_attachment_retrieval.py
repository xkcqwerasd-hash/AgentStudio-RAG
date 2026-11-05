import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.attachments.budget import AttachmentBudgetManager
from app.attachments.chunker import AttachmentChunker
from app.attachments.retriever import EphemeralBM25Index
from app.attachments.service import AttachmentRetrievalService
from app.models.conversation import ConversationAttachment
from app.rag.context_budget import estimate_tokens


# =========================================================================
# Realistic Enterprise Test Corpus
# =========================================================================

CORPUS_SPECS = [
    {
        "id": "att-eval-001",
        "name": "gateway_architecture.md",
        "ext": ".md",
        "pages": [
            {
                "page_number": 1,
                "text": (
                    "# 微服务 API 网关架构规约\n\n"
                    "## 1. 动态路由引擎\n"
                    "网关基于前缀树 Trie 实现 URL 动态路由匹配，支持根据 Path、Host、Header 进行多维加权分流。\n"
                    "在金丝雀灰度发布场景下，通过注入流量染色标识，按权重将 10% 流量导流向 v2 实例。\n"
                    "## 2. 限流与熔断策略\n"
                    "网关限流器采用分布式令牌桶算法，由 Redis 提供高并发原子计数器支撑，限制单 IP QPS 上限为 500。\n"
                    "熔断器遵循半开状态机模式，当上游 5xx 错误率超过 15% 时自动熔断 30 秒，保护后端微服务集群。\n"
                    "## 3. TLS 终止与证书配置\n"
                    "网关接入层统一进行 TLS 1.3 卸载，采用 ECDSA 与 RSA 双证书自适应协商机制，确保网络链路安全。"
                ),
                "metadata": {"heading_path": "# 微服务 API 网关架构规约"},
            }
        ],
    },
    {
        "id": "att-eval-002",
        "name": "database_optimization.docx",
        "ext": ".docx",
        "pages": [
            {
                "page_number": 1,
                "text": (
                    "### 数据库连接池与读写分离架构指南\n"
                    "1. 连接池调优参数配置\n"
                    "连接池建议设置 min_idle=16，max_size=128，连接超时 acquisition_timeout 设置为 5000ms。\n"
                    "高并发峰值期如果连接耗尽，连接池将触发等待队列并产生排队告警。\n"
                    "2. 读写分离与从库复制延迟\n"
                    "主库承接全部事务性写操作，从库分担批量复杂报表读操作。\n"
                    "在主从同步延迟超过 1000ms 时，动态路由中间件自动将读流量回切至主库，防止脏读。\n"
                    "3. 分库分表分片键选取\n"
                    "分库分表键推荐采用全局递增分布式雪花算法 ID 或用户组织 ID，避免跨分片聚合查询产生的网络通信风暴。"
                ),
                "metadata": {"heading_path": "数据库连接池与读写分离架构指南", "block_index": 1},
            }
        ],
    },
    {
        "id": "att-eval-003",
        "name": "security_policy.pdf",
        "ext": ".pdf",
        "pages": [
            {
                "page_number": 1,
                "text": (
                    "企业网络安全与身份认证管理规约（第一部分）\n"
                    "第 1 条：OAuth2.0 与 OpenID Connect 规范\n"
                    "所有客户端访问必须持有由认证中心颁发的 Bearer Access Token。\n"
                    "Token 必须使用非对称加密算法 RS256 进行数字签名，公钥在 JWKS 端点定期轮换发布。\n"
                    "Access Token 有效期限制为 2 小时，Refresh Token 有效期最长为 14 天。\n"
                    "第 2 条：密码学与密码哈希存储\n"
                    "系统严禁明文存储用户口令，必须使用带有随机加盐机制的 bcrypt 算法或 Argon2id 算法计算哈希。"
                ),
                "metadata": {"page_number": 1},
            },
            {
                "page_number": 2,
                "text": (
                    "企业网络安全与身份认证管理规约（第二部分）\n"
                    "第 3 条：基于角色的访问控制（RBAC）与权限继承\n"
                    "系统权限模型划分为角色与权限原子操作。\n"
                    "支持权限继承树，部门管理员自动继承普通员工的基础数据读取权限，但关键财务与审计权限禁止向下隐式派生。"
                ),
                "metadata": {"page_number": 2},
            },
        ],
    },
    {
        "id": "att-eval-004",
        "name": "microservice_deploy.txt",
        "ext": ".txt",
        "pages": [
            {
                "page_number": 1,
                "text": (
                    "Kubernetes 容器化微服务持续部署与高可用指引\n"
                    "一、健康检查与探针配置\n"
                    "每个 Pod 必须配置存活探针 livenessProbe 与就绪探针 readinessProbe。\n"
                    "就绪探针初始等待时间 initialDelaySeconds 建议 15 秒，探测失败阈值 3 次，探测通过前不接入流量。\n"
                    "二、零停机滚动更新策略\n"
                    "发布更新采用 RollingUpdate 模式，设置 maxSurge=25%，maxUnavailable=0，确保在副本扩容并健康就绪前不缩容旧副本。\n"
                    "三、HPA 自动水平伸缩\n"
                    "配置 HorizontalPodAutoscaler 监控 CPU 利用率（目标 75%）与每秒请求数，缩容冷却窗口设置为 300 秒防止频繁抖动。"
                ),
                "metadata": {"encoding": "utf-8"},
            }
        ],
    },
    {
        "id": "att-eval-005",
        "name": "compliance_audit.pdf",
        "ext": ".pdf",
        "pages": [
            {
                "page_number": 1,
                "text": (
                    "GDPR 与网络数据合规安全审计指引\n"
                    "第一章：个人隐私信息（PII）脱敏规范\n"
                    "用户手机号、身份证号、银行卡号等敏感信息在进入日志系统前必须经过严格掩码处理。\n"
                    "手机号仅保留前 3 位和后 4 位，中间 4 位以星号替代（如 138****1234）。\n"
                    "第二章：日志归档与数据保留期限\n"
                    "核心业务操作日志与安全审计跟踪日志必须持久化归档至不可变对象存储，最低保留期限为 180 天。\n"
                    "第三章：数据被遗忘权响应流程\n"
                    "用户提交注销与数据删除申请后，系统需在 30 个工作日内完成主库与备份介质中个人识别信息的匿名化擦除。"
                ),
                "metadata": {"page_number": 1},
            }
        ],
    },
]

# 10 Ground-Truth Queries with target document IDs
EVAL_QUERIES = [
    {
        "query": "API网关如何配置限流与路由匹配规则？",
        "expected_doc": "att-eval-001",
        "target_snippet": "动态路由匹配",
    },
    {
        "query": "数据库连接池大小与读写分离延迟调优",
        "expected_doc": "att-eval-002",
        "target_snippet": "连接池调优参数",
    },
    {
        "query": "OAuth2 访问令牌与 JWT 签名验证规范",
        "expected_doc": "att-eval-003",
        "target_snippet": "Bearer Access Token",
    },
    {
        "query": "Kubernetes 滚动更新与健康探针探活机制",
        "expected_doc": "att-eval-004",
        "target_snippet": "livenessProbe",
    },
    {
        "query": "用户个人信息数据脱敏与日志审计归档保留期限",
        "expected_doc": "att-eval-005",
        "target_snippet": "脱敏规范",
    },
    {
        "query": "网关 TLS 1.3 卸载与证书协商机制",
        "expected_doc": "att-eval-001",
        "target_snippet": "TLS 1.3",
    },
    {
        "query": "分库分表分片键选取原则与雪花算法",
        "expected_doc": "att-eval-002",
        "target_snippet": "分库分表键",
    },
    {
        "query": "基于角色的访问控制 RBAC 与权限继承模型",
        "expected_doc": "att-eval-003",
        "target_snippet": "角色与权限",
    },
    {
        "query": "HPA 自动水平伸缩指标与冷却窗口",
        "expected_doc": "att-eval-004",
        "target_snippet": "HorizontalPodAutoscaler",
    },
    {
        "query": "GDPR 合规性与个人数据被遗忘权流程",
        "expected_doc": "att-eval-005",
        "target_snippet": "被遗忘权",
    },
]


def _build_test_attachments(count: int = 5) -> List[ConversationAttachment]:
    """Helper to construct ConversationAttachment instances from corpus."""
    attachments: List[ConversationAttachment] = []
    for spec in CORPUS_SPECS[:count]:
        att = ConversationAttachment(
            id=spec["id"],
            conversation_id="eval-conv-p03",
            original_filename=spec["name"],
            storage_key=f"attachments/eval/{spec['name']}",
            file_extension=spec["ext"],
            mime_type="application/octet-stream",
            file_size=2048,
            parse_status="COMPLETED",
            page_count=len(spec["pages"]),
        )
        att.parsed_pages = spec["pages"]
        att.char_length = len(att.extracted_text)
        att.token_count = estimate_tokens(att.extracted_text)
        attachments.append(att)
    return attachments


# =========================================================================
# Evaluation Benchmark Suite
# =========================================================================

@dataclass
class EvalMetrics:
    scenario_name: str
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    avg_latency_ms: float
    p95_latency_ms: float
    avg_token_usage: int


def run_benchmark_experiment(
    attachments: List[ConversationAttachment],
    chunk_size: int = 500,
    top_k: int = 5,
    scenario_name: str = "BM25 (chunk=500, k=5)",
    strategy_override: Optional[str] = None,
) -> EvalMetrics:
    """
    Execute real benchmark over 10 test queries and calculate exact metrics.
    """
    service = AttachmentRetrievalService(chunk_size=chunk_size)
    chunker = AttachmentChunker(chunk_size=chunk_size)
    chunks = chunker.chunk_attachments(attachments)

    index = EphemeralBM25Index(conversation_id="eval-conv-p03")
    index.build(chunks)

    r1_hits = 0
    r3_hits = 0
    r5_hits = 0
    reciprocal_ranks = []
    latencies = []
    token_usages = []

    for item in EVAL_QUERIES:
        query = item["query"]
        expected_doc = item["expected_doc"]

        # Only evaluate if expected document is part of current active attachments
        active_ids = {a.id for a in attachments}
        if expected_doc not in active_ids:
            continue

        start_time = time.perf_counter()

        if strategy_override == "full_text":
            # Full text injection strategy
            ctx = service.prepare_attachment_context(
                conversation_id="eval-conv-p03",
                query=query,
                attachments=attachments,
                force_strategy="full_text",
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            latencies.append(elapsed_ms)
            token_usages.append(ctx.used_tokens)

            # In full-text, all active documents are present in context
            r1_hits += 1
            r3_hits += 1
            r5_hits += 1
            reciprocal_ranks.append(1.0)

        else:
            # BM25 retrieval strategy
            results = index.search(query=query, top_k=top_k)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            latencies.append(elapsed_ms)

            # Estimate token usage for retrieved chunks
            ctx = service.budget_manager.assemble_retrieved_context(
                candidate_chunks=results,
                total_attachment_tokens=service.budget_manager.calculate_total_attachment_tokens(attachments),
                budget=4000,
                attachments_count=len(attachments),
            )
            token_usages.append(ctx.used_tokens)

            # Calculate hit ranks
            retrieved_doc_ids = [c.document_id for c in results]

            # Recall@1
            if len(retrieved_doc_ids) >= 1 and retrieved_doc_ids[0] == expected_doc:
                r1_hits += 1

            # Recall@3
            if expected_doc in retrieved_doc_ids[:3]:
                r3_hits += 1

            # Recall@5
            if expected_doc in retrieved_doc_ids[:5]:
                r5_hits += 1

            # MRR
            if expected_doc in retrieved_doc_ids:
                rank = retrieved_doc_ids.index(expected_doc) + 1
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)

    total_queries = len(reciprocal_ranks)
    if total_queries == 0:
        return EvalMetrics(
            scenario_name=scenario_name,
            recall_at_1=0.0,
            recall_at_3=0.0,
            recall_at_5=0.0,
            mrr=0.0,
            avg_latency_ms=0.0,
            p95_latency_ms=0.0,
            avg_token_usage=0,
        )

    r1 = r1_hits / total_queries
    r3 = r3_hits / total_queries
    r5 = r5_hits / total_queries
    mrr = float(np.mean(reciprocal_ranks))
    avg_lat = float(np.mean(latencies))
    p95_lat = float(np.percentile(latencies, 95))
    avg_tokens = int(np.mean(token_usages))

    return EvalMetrics(
        scenario_name=scenario_name,
        recall_at_1=round(r1, 4),
        recall_at_3=round(r3, 4),
        recall_at_5=round(r5, 4),
        mrr=round(mrr, 4),
        avg_latency_ms=round(avg_lat, 2),
        p95_latency_ms=round(p95_lat, 2),
        avg_token_usage=avg_tokens,
    )


def run_full_evaluation() -> List[EvalMetrics]:
    """
    Run comprehensive evaluation suite comparing:
    1. Full-text injection vs BM25 retrieval
    2. Different chunk sizes (250, 500, 1000)
    3. Different Top-K (1, 3, 5)
    4. Different attachment counts (1, 3, 5)
    """
    all_atts = _build_test_attachments(5)
    metrics_list: List[EvalMetrics] = []

    # 1. Full text vs BM25 (5 attachments)
    m_full = run_benchmark_experiment(
        attachments=all_atts,
        scenario_name="[Baseline] Full-Text (5 docs)",
        strategy_override="full_text",
    )
    metrics_list.append(m_full)

    m_bm25_std = run_benchmark_experiment(
        attachments=all_atts,
        chunk_size=500,
        top_k=5,
        scenario_name="BM25 Standard (chunk=500, k=5, 5 docs)",
    )
    metrics_list.append(m_bm25_std)

    # 2. Chunk size variations (250, 500, 1000)
    for c_size in [250, 1000]:
        m = run_benchmark_experiment(
            attachments=all_atts,
            chunk_size=c_size,
            top_k=5,
            scenario_name=f"Chunk Size = {c_size} (k=5, 5 docs)",
        )
        metrics_list.append(m)

    # 3. Top-K variations (1, 3)
    for k in [1, 3]:
        m = run_benchmark_experiment(
            attachments=all_atts,
            chunk_size=500,
            top_k=k,
            scenario_name=f"Top-K = {k} (chunk=500, 5 docs)",
        )
        metrics_list.append(m)

    # 4. Attachment count scaling (1, 3 docs)
    for count in [1, 3]:
        atts = _build_test_attachments(count)
        m = run_benchmark_experiment(
            attachments=atts,
            chunk_size=500,
            top_k=5,
            scenario_name=f"Doc Count = {count} (chunk=500, k=5)",
        )
        metrics_list.append(m)

    return metrics_list


def print_evaluation_report(metrics_list: List[EvalMetrics]) -> str:
    """Format evaluation benchmark into a Markdown table."""
    header = (
        "| 评测场景 | Recall@1 | Recall@3 | Recall@5 | MRR | 平均延迟 (ms) | P95 延迟 (ms) | Token 使用量 |\n"
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
    )
    rows = []
    for m in metrics_list:
        row = (
            f"| {m.scenario_name} | {m.recall_at_1:.2%} | {m.recall_at_3:.2%} | {m.recall_at_5:.2%} | "
            f"{m.mrr:.4f} | {m.avg_latency_ms:.2f} ms | {m.p95_latency_ms:.2f} ms | {m.avg_token_usage} |"
        )
        rows.append(row)
    return "\n".join([header] + rows)


# Pytest test cases to guarantee evaluation script is executable and verified
def test_evaluation_benchmark_runs_successfully():
    """Verify that the evaluation benchmark runs end-to-end and computes valid non-mock metrics."""
    metrics = run_full_evaluation()
    assert len(metrics) >= 7

    # Verify standard BM25 retrieval has high recall on real corpus
    bm25_std = next(m for m in metrics if "BM25 Standard" in m.scenario_name)
    assert bm25_std.recall_at_3 >= 0.80
    assert bm25_std.recall_at_5 >= 0.90
    assert bm25_std.mrr >= 0.80
    assert bm25_std.avg_latency_ms < 50.0  # Fast in-memory BM25 < 50ms

    # Full text baseline uses much more tokens than BM25
    full_text = next(m for m in metrics if "[Baseline] Full-Text" in m.scenario_name)
    assert full_text.avg_token_usage > bm25_std.avg_token_usage

    # Print summary to terminal
    print("\n" + print_evaluation_report(metrics))


if __name__ == "__main__":
    results = run_full_evaluation()
    report = print_evaluation_report(results)
    print("\n=== AgentStudio-RAG P0.3 Stage 2 Attachment Retrieval Benchmark ===")
    print(report)
