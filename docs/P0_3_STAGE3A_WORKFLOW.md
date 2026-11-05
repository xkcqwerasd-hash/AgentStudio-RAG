# AgentStudio-RAG — P0.3 Stage 3-A: Workflow RAG 与临时附件集成交付报告

## 一、阶段定位与目标达成

在完成 **P0.3 Stage 1（附件基础设施与文件解析）** 和 **P0.3 Stage 2（临时附件检索与上下文预算管理）** 之后，系统已具备会话级附件生命周期管理及轻量级检索能力。

**P0.3 Stage 3-A** 的核心任务是将 Conversation-scoped 临时附件检索能力正式接入 `WorkflowRAGEngine`，实现：
1. **统一问答模式支持**：纯知识库问答、纯临时附件问答（`knowledge_base_id=None`）、知识库与临时附件联合问答。
2. **冻结算法保真**：完全保留 M1～M3.3 核心检索链路（Dense 语义向量 + BM25 稀疏检索 + RRF 倒数排名融合 + Cross-Encoder Reranker），不修改永久知识库表结构。
3. **全局唯一证据与防碰撞引用**：合并 KB 与附件候选片段后，生成全局唯一、连续递增的 `[Ref-1]`, `[Ref-2]` 引用标记，严禁不同来源 ID 碰撞，严禁为 DOCX/TXT 编造虚假物理页码。
4. **统一上下文预算裁剪**：将附件证据与知识库候选证据纳入统一的 `ContextBudgetManager` 进行预算分配，被裁剪的候选片段严格排除在 Prompt 与 Citation 之外。
5. **SSE 流式传输规范增强**：流式链路按规范在适当时机依次触发 `attachment_context_started`、`attachment_retrieval_completed`、`attachment_context_completed`，并在客户端中断时安全持久化中止状态与附件执行指标。

---

## 二、架构设计与核心改动

### 1. 数据契约与模型增强

- [`backend/app/schemas/chat.py`](file:///e:/develop/python%20agentic%20RAG/backend/app/schemas/chat.py):
  - `ChatRequest`: `knowledge_base_id: Optional[str] = Field(None, ...)`，支持纯附件提问无需预先创建或绑定知识库。
  - `Citation`: 新增精准溯源字段 `source_type` (`knowledge_base` / `pdf` / `docx` / `markdown` / `txt`)、`heading_path` (`Optional[Union[List[str], str]]`)、`block_index`、`line_start`、`line_end`、`provenance_label`。
  - `ExecutionInfo`: 新增附件执行度量指标字段 `attachment_count`、`attachment_strategy`、`attachment_duration_ms`、`attachment_tokens`、`attachment_chunks_kept`、`attachment_chunks_pruned`。

- [`backend/app/agent/tools/evidence.py`](file:///e:/develop/python%20agentic%20RAG/backend/app/agent/tools/evidence.py):
  - `EvidenceItem`: 增加统一来源溯源字段，并在 `to_scored_chunk()` 中保证可逆还原为 `AttachmentScoredChunk`。

### 2. 证据展示与引用防伪

- [`backend/app/rag/prompt_builder.py`](file:///e:/develop/python%20agentic%20RAG/backend/app/rag/prompt_builder.py):
  - `PromptBuilder.build_user_prompt`: 依据片段的 `source_type` 自动路由：
    - 知识库片段：输出 `[Ref-X] 来源文件: {doc_name} | 页码: 第 {page} 页 | 相关度: {score}`；
    - 附件片段：调用 `AttachmentBudgetManager._format_chunk_provenance` 输出真实层级（如 `章节: 第一章 > 1.2 | 段落: #3` 或 `行号: 第 10-25 行`），彻底杜绝虚假物理页码。

- [`backend/app/rag/citation.py`](file:///e:/develop/python%20agentic%20RAG/backend/app/rag/citation.py):
  - `CitationExtractor`: 在正向验证 `[Ref-X]` 标签时，同步透传 `source_type`、`heading_path`、`block_index`、`line_start`、`line_end` 和 `provenance_label` 到最终的 `Citation` 实体中。

### 3. WorkflowRAGEngine 同步与流式双路径整合

- [`backend/app/rag/workflow.py`](file:///e:/develop/python%20agentic%20RAG/backend/app/rag/workflow.py):
  - `execute` & `execute_stream`:
    1. **Early Validation**: 校验 `validate_user_query` 防止超长 Query 溢出。
    2. **Conversation & Attachments Loading**: 获取当前会话已解析完成（`parse_status == 'COMPLETED'`）的附件集合，严格隔离会话外数据。
    3. **KB 存在性与状态路由**:
       - 若 `knowledge_base_id` 为空且会话无附件：平稳回退返回友好引导提示，不抛异常。
       - 若指定了 KB 且无附件但 KB 索引为空：返回索引待建立提示。
    4. **Dual Retrieval**:
       - 附件端：调用 `attachment_retrieval_service.prepare_attachment_context` 运行自适应策略（Small Attachment 全量注入 / Multi-Page BM25 检索）。
       - 知识库端：保持既有查询重写、Dense + BM25 混合检索与 Cross-Encoder Reranker 重排链路。
    5. **Candidate Merging**: 将附件候选与知识库候选合并，统一按 `similarity_score` 降序排列。
    6. **Unified Budgeting**: 传入 `budget_manager.allocate_and_assemble` 统一优化组装，自动丢弃低分溢出分块。
    7. **SSE Event Stream**:
       - `attachment_context_started` (会话附件数量与ID列表)
       - `attachment_retrieval_completed` (策略、命中分块数、耗时、Token数)
       - `attachment_context_completed` (总保留片段与策略)
       - `query_rewrite` (若有KB)
       - `retrieval` (统一召回度量)
       - `context_budget` (统一Token预算消耗与裁剪报告)
       - `token`* (LLM 流式字符)
       - `citations` (严格归因列表)
       - `trace` (含附件执行全指标的完整执行追踪)
       - `done` (最终消息ID)
    8. **Cancellation Resilience**: 客户端主动断开连接（`asyncio.CancelledError`）时，保留已生成文本追加 `[回答已被用户中止]`，正确记录附件指标入库并向上重抛中断。

---

## 三、18 项核心集成测试场景矩阵

测试套件 [`backend/tests/test_p0_3_workflow_integration.py`](file:///e:/develop/python%20agentic%20RAG/backend/tests/test_p0_3_workflow_integration.py) 包含 18 项覆盖全部边界和功能维度的端到端集成测试，测试结果全部通过：

| 序号 | 测试用例名称 | 核心验证点 | 测试结果 |
| :--- | :--- | :--- | :--- |
| **01** | `test_scenario_1_unaffected_existing_workflow_no_attachments` | 无附件时现有 Workflow RAG 行为完全兼容，`attachment_count=0`，指标无污染 | **PASSED** |
| **02** | `test_scenario_2_pure_attachment_qa_no_knowledge_base_id` | 纯附件问答（`knowledge_base_id=None`），检索模式为 `attachment`，正常生成回答与引用 | **PASSED** |
| **03** | `test_scenario_3_pure_kb_qa_no_attachments` | 显式指定 KB 且会话中无附件，保持标准 Hybrid 检索与知识库引用 | **PASSED** |
| **04** | `test_scenario_4_attachment_plus_kb_combined_qa` | 知识库与会话附件联合检索，候选池统一排序并准确归因 | **PASSED** |
| **05** | `test_scenario_5_multi_attachment_unified_retrieval` | 跨多个不同类型附件（PDF、DOCX、TXT）的联合统一检索 | **PASSED** |
| **06** | `test_scenario_6_pdf_page_number_attribution_in_citations` | PDF 引用严格对应物理页码，`provenance_label` 标明 `页码: 第 X 页`，不造假 | **PASSED** |
| **07** | `test_scenario_7_docx_heading_path_block_index_attribution_in_citations` | DOCX 引用完整呈现层级章节路径、段落号，行号信息保持透明 | **PASSED** |
| **08** | `test_scenario_8_unique_global_reference_indexing_no_collision` | KB 与附件分块混合送入 Prompt 渲染，`[Ref-1]`, `[Ref-2]` 连续唯一无碰撞 | **PASSED** |
| **09** | `test_scenario_9_unified_budget_allocation_across_kb_and_attachments` | 统一预算池在超预算时公平按相关度分数裁剪低分分块，遵守总 Token 上限 | **PASSED** |
| **10** | `test_scenario_10_budget_pruned_chunks_excluded_from_citations` | 因预算超限被裁剪丢弃的分块，严格被 CitationExtractor 排除，无法成为合法引用 | **PASSED** |
| **11** | `test_scenario_11_cross_conversation_attachment_isolation` | 会话 A 的附件对会话 B 严格隔离不可见，彻底防止越权数据泄漏 | **PASSED** |
| **12** | `test_scenario_12_attachment_deletion_and_status_isolation` | 解析未完成（`FAILED`/`PENDING`）或已物理删除的附件不参与检索 | **PASSED** |
| **13** | `test_scenario_13_sse_attachment_event_sequencing` | SSE 事件流顺序严格符合协议设计：`start` -> `attachment_*` -> `retrieval` -> `context_budget` -> `token`* -> `citations` -> `trace` -> `done` | **PASSED** |
| **14** | `test_scenario_14_graceful_handling_no_kb_and_no_attachments` | 未选知识库且无附件时，同步与流式均优雅输出友好提示信息，不抛 500 异常 | **PASSED** |
| **15** | `test_scenario_15_malicious_prompt_injection_containment` | 附件中包含提示词注入指令（如 DAN 攻击）时，被严格包裹在参考事实边界内，保持助手安全防御 | **PASSED** |
| **16** | `test_scenario_16_multi_turn_history_compatibility_with_attachments` | 包含附件的多轮对话中，历史轮次正常纳入预算剪裁与传递，附件持续可用 | **PASSED** |
| **17** | `test_scenario_17_client_stream_cancellation_saves_partial_state_with_attachments` | 客户端流式中断时正确回滚并持久化 `[回答已被用户中止]`，保留附件执行元数据 | **PASSED** |
| **18** | `test_scenario_18_m1_m3_full_retrieval_regression` | 验证包含 Dense、BM25、RRF、Reranker 在内的完整 M1~M3.3 全流程无任何算法回归 | **PASSED** |

---

## 四、全量回归验证结论

执行全后端测试套件：
```bash
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
```
**测试输出：**
```
........................................................................ [ 29%]
........................................................................ [ 58%]
........................................................................ [ 87%]
...............................                                          [100%]
247 passed, 1 warning in 36.02s
```
- **通过测试数**：247 项全量通过（含 Stage 1 的 22 项、Stage 2 的 22 项、Stage 3-A 的 18 项及 M1~M3.3、M4.1、P0.1、P0.2 的既有测试）。
- **回归破坏数**：0 项。
- **阶段状态**：P0.3 Stage 3-A 研发与测试验证已完全达标关闭。
