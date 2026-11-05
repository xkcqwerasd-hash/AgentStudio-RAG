from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class Citation(BaseModel):
    citation_index: int = Field(..., description="引用序号 (如 1 对应 [Ref-1])")
    chunk_id: str = Field(..., description="真实分块唯一ID")
    document_id: str = Field(..., description="真实源文档ID")
    document_name: str = Field(..., description="真实源文件名")
    page_number: int = Field(..., description="原始文档页码 (从1开始)")
    similarity_score: float = Field(..., description="检索相似度得分")
    content_snippet: str = Field(..., description="分块文本预览或原句")
    source_type: Optional[str] = Field(default=None, description="来源类型: knowledge_base / attachment / pdf / docx / markdown / txt")
    heading_path: Optional[Union[List[str], str]] = Field(default=None, description="章节路径 (DOCX / Markdown)")
    block_index: Optional[int] = Field(default=None, description="段落编号 (DOCX)")
    line_start: Optional[int] = Field(default=None, description="起始行号 (TXT)")
    line_end: Optional[int] = Field(default=None, description="结束行号 (TXT)")
    provenance_label: Optional[str] = Field(default=None, description="精准来源位置描述文本")


class QueryRewriteInfo(BaseModel):
    original_query: str = Field(..., description="用户原始问题")
    rewritten_query: str = Field(..., description="重写后的检索问题")
    is_rewritten: bool = Field(default=False, description="是否发生了重写")
    duration_ms: float = Field(default=0.0, description="重写耗时(ms)")
    tokens_used: Optional[int] = Field(default=None, description="重写消耗的Token数")
    fallback: bool = Field(default=False, description="是否触发了异常回退")
    fallback_reason: Optional[str] = Field(default=None, description="回退原因说明")


class ContextBudgetInfo(BaseModel):
    model_context_window: int = Field(default=32768, description="模型支持的总上下文窗口Token数")
    input_budget: int = Field(default=26624, description="可用输入Token预算")
    estimated_input_tokens: int = Field(default=0, description="实际组装Prompt估算Token数")
    actual_input_tokens: Optional[int] = Field(default=None, description="模型服务端返回的实际输入Token数")
    reserved_output_tokens: int = Field(default=4096, description="预留给模型输出的Token数")
    safety_margin: int = Field(default=2048, description="安全缓冲余量Token数")
    system_prompt_tokens: int = Field(default=0, description="System Prompt消耗Token数")
    history_tokens: int = Field(default=0, description="历史对话纳入Prompt消耗Token数")
    retrieval_tokens: int = Field(default=0, description="检索证据纳入Prompt消耗Token数")
    query_tokens: int = Field(default=0, description="当前用户问题消耗Token数")
    history_messages_kept: int = Field(default=0, description="历史消息保留条数")
    history_messages_pruned: int = Field(default=0, description="历史消息裁剪丢弃条数")
    retrieval_chunks_kept: int = Field(default=0, description="检索分块保留数量")
    retrieval_chunks_pruned: int = Field(default=0, description="检索分块因超预算裁剪数量")
    # P1.2 Long-term Memory Budget additions
    memory_tokens: Optional[int] = Field(default=0, description="长期记忆纳入Prompt消耗Token数")
    memories_kept: Optional[int] = Field(default=0, description="保留的记忆条数")
    memories_pruned: Optional[int] = Field(default=0, description="因预算裁剪的记忆条数")
    budget_estimation_method: str = Field(default="conservative_heuristic", description="Token估算方法")


class ExecutionInfo(BaseModel):
    mode: str = Field(default="workflow", description="执行模式: workflow / agentic / direct")
    intent: Optional[str] = Field(default=None, description="识别出的核心意图类型")
    execution_path: Optional[str] = Field(default=None, description="实际执行路径")
    route_reason: Optional[str] = Field(default=None, description="路由决策依据")
    retrieved_chunks_count: int = Field(default=0, description="召回分块总数")
    retrieval_duration_ms: float = Field(default=0.0, description="检索阶段耗时(ms)")
    generation_duration_ms: float = Field(default=0.0, description="生成阶段耗时(ms)")
    total_duration_ms: float = Field(default=0.0, description="全链路端到端总耗时(ms)")
    llm_model: str = Field(default="", description="调用的LLM模型名称")
    # M2.2 & M2.3 additions
    ttft_ms: Optional[float] = Field(default=None, description="首字响应延迟(ms)")
    original_query: Optional[str] = Field(default=None, description="用户原始提问")
    rewritten_query: Optional[str] = Field(default=None, description="重写后的检索提问")
    is_rewritten: bool = Field(default=False, description="是否触发了查询重写")
    rewrite_duration_ms: Optional[float] = Field(default=None, description="查询重写耗时(ms)")
    rewrite_tokens_used: Optional[int] = Field(default=None, description="查询重写消耗Token")
    # M2.4 Context Budget additions
    model_context_window: Optional[int] = Field(default=None, description="模型最大上下文窗口")
    estimated_input_tokens: Optional[int] = Field(default=None, description="估算的输入Token数")
    actual_input_tokens: Optional[int] = Field(default=None, description="模型实际统计的输入Token数")
    actual_output_tokens: Optional[int] = Field(default=None, description="模型实际统计的输出Token数")
    reserved_output_tokens: Optional[int] = Field(default=None, description="预留输出Token数")
    history_tokens: Optional[int] = Field(default=None, description="最终纳入Prompt的历史Token数")
    retrieval_tokens: Optional[int] = Field(default=None, description="最终纳入Prompt的证据Token数")
    history_messages_kept: Optional[int] = Field(default=None, description="保留的历史消息条数")
    history_messages_pruned: Optional[int] = Field(default=None, description="裁剪丢弃的历史消息条数")
    retrieval_chunks_kept: Optional[int] = Field(default=None, description="保留的证据分块数")
    retrieval_chunks_pruned: Optional[int] = Field(default=None, description="裁剪丢弃的证据分块数")
    budget_estimation_method: Optional[str] = Field(default=None, description="Token估算方案")
    # M3.2 Hybrid Retrieval & RRF additions
    retrieval_mode: Optional[str] = Field(default="hybrid", description="检索模式: hybrid / dense / bm25")
    dense_duration_ms: Optional[float] = Field(default=None, description="Dense向量检索耗时(ms)")
    bm25_duration_ms: Optional[float] = Field(default=None, description="BM25稀疏检索耗时(ms)")
    rrf_duration_ms: Optional[float] = Field(default=None, description="RRF融合排序耗时(ms)")
    dense_chunks_count: Optional[int] = Field(default=None, description="Dense召回分块数")
    bm25_chunks_count: Optional[int] = Field(default=None, description="BM25召回分块数")
    deduplicated_chunks_count: Optional[int] = Field(default=None, description="去重后候选分块数")
    degradation_status: Optional[str] = Field(default="none", description="检索降级状态: none / dense_failed / bm25_failed / both_failed")
    # M3.3 Cross-Encoder Reranker additions
    reranker_enabled: bool = Field(default=True, description="是否启用 Cross-Encoder 精排")
    reranker_model: Optional[str] = Field(default=None, description="精排模型名称")
    reranker_duration_ms: Optional[float] = Field(default=None, description="精排阶段耗时(ms)")
    reranker_candidates_in: Optional[int] = Field(default=None, description="精排输入候选分块数")
    reranker_candidates_out: Optional[int] = Field(default=None, description="精排输出分块数")
    reranker_degraded: bool = Field(default=False, description="精排是否触发降级回退")
    reranker_degradation_reason: Optional[str] = Field(default=None, description="精排降级原因")
    # P0.3 Ephemeral Attachments additions
    attachment_count: int = Field(default=0, description="当前会话附件总数")
    attachment_strategy: Optional[str] = Field(default=None, description="附件处理策略: full_text / bm25_retrieval / none")
    attachment_duration_ms: Optional[float] = Field(default=None, description="附件检索耗时(ms)")
    attachment_tokens: Optional[int] = Field(default=None, description="附件消耗的Token数")
    attachment_chunks_kept: Optional[int] = Field(default=None, description="保留的附件分块数")
    attachment_chunks_pruned: Optional[int] = Field(default=None, description="裁剪的附件分块数")
    # P1.2 Long-term Memory additions
    memory_used: bool = Field(default=False, description="是否检索并使用了长期记忆")
    retrieved_memory_count: int = Field(default=0, description="语义召回的长期记忆总数")
    injected_memory_count: int = Field(default=0, description="实际注入上下文的长期记忆条数")
    memory_tokens: int = Field(default=0, description="长期记忆消耗的Token数")
    memory_retrieval_duration_ms: Optional[float] = Field(default=None, description="记忆检索耗时(ms)")


class ChatRequest(BaseModel):
    knowledge_base_id: Optional[str] = Field(None, description="目标知识库ID (纯附件问答时可选)")
    query: str = Field(..., min_length=1, description="用户提问内容")
    conversation_id: Optional[str] = Field(None, description="已有会话ID (可选)")
    mode: str = Field(default="workflow", description="执行模式")
    top_k: int = Field(default=4, ge=1, le=20, description="召回分块数")
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
    model_id: Optional[str] = Field(None, description="运行时指定的具体模型配置ID (可选)")


class ChatResponse(BaseModel):
    answer: str = Field(..., description="AI生成的专业回答")
    citations: List[Citation] = Field(default_factory=list, description="真实引用证据列表")
    execution_info: ExecutionInfo = Field(..., description="执行指标与性能追踪")
    conversation_id: str = Field(..., description="会话ID")
    message_id: str = Field(..., description="消息ID")


class RetrievalTestRequest(BaseModel):
    knowledge_base_id: str = Field(..., description="目标知识库ID")
    query: str = Field(..., min_length=1, description="测试检索查询词")
    top_k: int = Field(default=5, ge=1, le=50, description="召回数量")


class RetrievalTestItem(BaseModel):
    chunk_id: str
    document_id: str
    document_name: str
    page_number: int
    similarity_score: float
    content: str
    dense_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    dense_score: Optional[float] = None
    bm25_score: Optional[float] = None
    fusion_score: Optional[float] = None
    reranker_rank: Optional[int] = None
    reranker_score: Optional[float] = None
    reranker_raw_score: Optional[float] = None


class RetrievalTestResponse(BaseModel):
    query: str
    knowledge_base_id: str
    total_retrieved: int
    results: List[RetrievalTestItem]
    duration_ms: float
    dense_duration_ms: Optional[float] = None
    bm25_duration_ms: Optional[float] = None
    rrf_duration_ms: Optional[float] = None
    reranker_duration_ms: Optional[float] = None
    reranker_candidates_in: Optional[int] = None
    reranker_candidates_out: Optional[int] = None
    reranker_model: Optional[str] = None
    reranker_degraded: Optional[bool] = None
    degradation_status: Optional[str] = "none"
