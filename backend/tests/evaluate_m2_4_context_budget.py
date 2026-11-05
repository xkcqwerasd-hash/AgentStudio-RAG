"""
Evaluation Benchmark Script for M2.4: Token Budget & Context Optimization
Evaluates Token Budget allocation, dynamic pruning, latency, and context efficiency
across Short, Medium, and Long Context scenarios.
"""

import time
from typing import Dict, List
from app.rag.citation import CitationExtractor
from app.rag.context_budget import ContextBudgetManager, estimate_tokens
from app.retrieval.vector_store import ScoredChunk


def make_chunk(cid: str, text: str, score: float, doc: str = "doc.md", page: int = 1) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=cid,
        document_id=f"d_{cid}",
        document_name=doc,
        page_number=page,
        chunk_index=0,
        content=text,
        similarity_score=score,
    )


def run_benchmark():
    print("=" * 80)
    print("AgentStudio-RAG M2.4: Token Budget & Context Optimization Benchmark")
    print("=" * 80)

    system_prompt = (
        "你是由 AgentStudio-RAG 驱动的企业级智能知识助手。\n"
        "你的任务是根据下方提供的【参考资料】中经过严格检索确认的事实，准确、严谨、客观地回答用户的问题。\n"
        "请遵循核心原则：真实严谨、标准引用 [Ref-X]、无法回答时明确说明证据不足。"
    )

    # --------------------------------------------------------------------------
    # Scenario 1: Short Context (1-2 Turns, 3 Chunks, Default 32k window)
    # --------------------------------------------------------------------------
    mgr_short = ContextBudgetManager(model_context_window=32768)
    history_short = [
        {"role": "user", "content": "你好，请问系统微服务网关负责什么核心功能？"},
        {"role": "assistant", "content": "微服务网关主要负责统一路由、鉴权认证、流量限流与熔断防护 [Ref-1]。"},
    ]
    chunks_short = [
        make_chunk("c1", "网关路由服务基于动态路由表将客户端请求精确转发到后端真实节点。", 0.94, "gateway.md", 1),
        make_chunk("c2", "网关统一鉴权中间件在接收到请求时校验 JWT Token 及请求签名。", 0.89, "auth.md", 2),
        make_chunk("c3", "网关流量控制模块支持基于令牌桶与漏桶的平滑限流降级策略。", 0.82, "flow.md", 3),
    ]
    query_short = "网关如何处理未授权的非法请求？"

    t0 = time.perf_counter()
    plan_short = mgr_short.allocate_and_assemble(
        query=query_short,
        system_prompt=system_prompt,
        raw_history_messages=history_short,
        retrieved_chunks=chunks_short,
    )
    t_short_ms = (time.perf_counter() - t0) * 1000

    # --------------------------------------------------------------------------
    # Scenario 2: Medium Context (6 Turns, 8 Chunks, Moderate Budget)
    # --------------------------------------------------------------------------
    mgr_med = ContextBudgetManager(
        model_context_window=8192,
        max_output_tokens=1024,
        context_safety_margin=512,
        history_token_budget=1024,
        retrieval_token_budget=2048,
    )
    history_med = []
    for i in range(1, 7):
        history_med.append({"role": "user", "content": f"第{i}轮提问：请说明系统模块{i}的接口设计规范及异常处理流程？"})
        history_med.append({"role": "assistant", "content": f"第{i}轮回答：模块{i}基于标准RESTful协议，统一返回ErrorCode与ErrorMessage封装对象 [Ref-1]。"})

    chunks_med = [
        make_chunk(f"cm_{i}", f"中等规模知识片段第{i}部分：详述系统模块在处理并发调用时的线程隔离、超时控制及重试策略机制，确保整体雪崩预防。", 0.95 - i * 0.04, f"spec_{i}.md", i)
        for i in range(1, 9)
    ]
    query_med = "如果模块3发生超时，系统会执行几次重试？"

    t0 = time.perf_counter()
    plan_med = mgr_med.allocate_and_assemble(
        query=query_med,
        system_prompt=system_prompt,
        raw_history_messages=history_med,
        retrieved_chunks=chunks_med,
    )
    t_med_ms = (time.perf_counter() - t0) * 1000

    # --------------------------------------------------------------------------
    # Scenario 3: Long Context / Stress Scenario (15 Turns, 15 Chunks, Tight 4k Window)
    # --------------------------------------------------------------------------
    mgr_long = ContextBudgetManager(
        model_context_window=4096,
        max_output_tokens=1024,
        context_safety_margin=512,
        history_token_budget=800,
        retrieval_token_budget=1200,
    )
    history_long = []
    for i in range(1, 16):
        history_long.append({
            "role": "user",
            "content": f"长上下文第{i}轮详细探讨：请分析系统架构中分布式事务、两阶段提交与最终一致性在第{i}种业务场景下的技术选型理由。"
        })
        history_long.append({
            "role": "assistant",
            "content": f"长上下文第{i}轮深入解答：对于场景{i}，核心考量是系统吞吐量与数据强一致性的权衡，建议采用基于可靠消息队列的最终一致性方案 [Ref-1]。"
        })

    chunks_long = [
        make_chunk(
            f"cl_{i}",
            f"企业级长文档检索分块第{i}段：详细规范系统在极端网络分区、节点宕机及脑裂故障时的选举投票机制、仲裁算法与故障恢复时延指标约束。",
            0.98 - i * 0.03,
            f"cluster_ha_{i}.md",
            i,
        )
        for i in range(1, 16)
    ]
    query_long = "在极端网络分区情况下，仲裁节点如何防止脑裂发生？"

    t0 = time.perf_counter()
    plan_long = mgr_long.allocate_and_assemble(
        query=query_long,
        system_prompt=system_prompt,
        raw_history_messages=history_long,
        retrieved_chunks=chunks_long,
    )
    t_long_ms = (time.perf_counter() - t0) * 1000

    # --------------------------------------------------------------------------
    # Output Benchmark Results Table
    # --------------------------------------------------------------------------
    scenarios = [
        ("短上下文 (Short Context)", plan_short, t_short_ms, len(history_short), len(chunks_short)),
        ("中等上下文 (Medium Context)", plan_med, t_med_ms, len(history_med), len(chunks_med)),
        ("长上下文/高压 (Long Context)", plan_long, t_long_ms, len(history_long), len(chunks_long)),
    ]

    print("\n| 评估场景 | 模型窗口 | 输入预算 | 估算输入Token | 历史保留/总数 | 证据保留/总数 | 预算执行耗时 | 超限状态 |")
    print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for name, plan, duration, raw_hist_cnt, raw_chunk_cnt in scenarios:
        b = plan.budget_info
        hist_ratio = f"{b.history_messages_kept}/{raw_hist_cnt}"
        chunk_ratio = f"{b.retrieval_chunks_kept}/{raw_chunk_cnt}"
        overflow = "安全无超限" if b.estimated_input_tokens <= b.input_budget else "超限溢出!"
        print(f"| {name} | {b.model_context_window} | {b.input_budget} | {b.estimated_input_tokens} tokens | {hist_ratio} | {chunk_ratio} | {duration:.2f} ms | {overflow} |")

    print("\n" + "=" * 80)
    print("详细 Token 结构拆解:")
    print("=" * 80)
    for name, plan, _, _, _ in scenarios:
        b = plan.budget_info
        print(f"\n【{name}】")
        print(f"  - System Prompt Tokens: {b.system_prompt_tokens}")
        print(f"  - User Query Tokens:    {b.query_tokens}")
        print(f"  - History Tokens:       {b.history_tokens} (保留 {b.history_messages_kept} 条, 裁剪 {b.history_messages_pruned} 条)")
        print(f"  - Retrieval Tokens:     {b.retrieval_tokens} (保留 {b.retrieval_chunks_kept} 个, 裁剪 {b.retrieval_chunks_pruned} 个)")
        print(f"  - Total Estimated:      {b.estimated_input_tokens} / {b.input_budget} tokens")
        print(f"  - Reserved Output:      {b.reserved_output_tokens} tokens")
        print(f"  - Safety Margin:        {b.safety_margin} tokens")
        print(f"  - Estimation Method:    {b.budget_estimation_method}")


if __name__ == "__main__":
    run_benchmark()
