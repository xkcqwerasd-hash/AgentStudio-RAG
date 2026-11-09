# AgentStudio-RAG 项目问题与缺陷归档追踪档案 (Project Issue Archive)

**创建日期**：2026-11-06  
**维护原则**：
1. **真实可溯**：每一个问题均具备具体的测试样本 ID、调用链、Trace 日志与客观证据，严禁主观臆断；
2. **状态严谨**：严格区分状态流转，未经真实全链路对照实验验证的，严禁标记为彻底解决：
   $$\text{已发现} \longrightarrow \text{已定位} \longrightarrow \text{已修改} \longrightarrow \text{已通过回归测试} \longrightarrow \text{已通过真实实验验证}$$
3. **技术可叙述**：每个问题沉淀出技术原理、权衡逻辑与工程面试话术，服务于技术能力沉淀。

---

## 缺陷状态定义规范

| 状态标识 | 准入定义与门禁条件 |
| :--- | :--- |
| **`[已发现]`** | 在 Benchmark 评测、冒烟测试或用户交互中复现异常现象，已记录对应样本 ID 与错误表现。 |
| **`[已定位]`** | 通过日志、断点、Trace 探针或源码分析定位到具体代码行与物理机制，能够稳定复现并给出因果证据。 |
| **`[已修改]`** | 已编写针对性修复代码或配置补丁，但尚未执行自动化测试验证。 |
| **`[已通过回归测试]`** | 本地离线单元测试（pytest）或轻量 Mock 冒烟测试全部通过，证明基础函数逻辑无回归。 |
| **`[已通过真实实验验证]`** | 在真实网络 Socket、真实数据库、真实外部大模型 API 下完成两轮严密对照实验（Baseline vs Final），指标与日志证实缺陷彻底根治。 |

---

## 缺陷归档详单

### ISS-001：IntentRouter 意图路由技术关键词误分流导致知识库 RAG 被绕过

- **发现时间**：2026-11-06（第一轮 30 题 Benchmark 评测）
- **当前解决状态**：**`[已通过真实实验验证]`**
- **对应测试样本**：`GEN-09`（HTTP 连接池）、`GEN-10`（JWT 鉴权规范）、`GEN-13`（连接池配置）、`GEN-15`（日志与熔断）
- **发现指标与 Trace**：
  - 评测得分全部为 `1 / 1 / 1 / 1` 或 `1 / 1 / 1 / 2`；
  - `kept_chunks_count: 0`，`retrieval_ms: None`；
  - Trace 显示请求未进入 `WorkflowRAGEngine`，而是被分流至 `ExecutionPath.DIRECT_LLM` 或附件提取。
- **涉及代码与调用链**：
  - `backend/app/routing/intent_router.py` -> `IntentRouter.route()`
  - 调用链：`POST /api/v1/chat/stream` $\rightarrow$ `chat.py:168` $\rightarrow$ `intent_router.route()` $\rightarrow$ `matches_generic_tech()`
- **根本原因与证据**：
  - `intent_router.py` 中的 `GENERIC_TECH_PATTERNS` 包含了 `HTTP`、`JWT`、`并发`、`算法` 等正则匹配；
  - 旧逻辑在用户已经明确指定 `knowledge_base_id` 的前提下，仍然先执行通用技术问题匹配，导致企业专有文档中的技术规范被错误降级为纯大模型直连，大模型无底库上下文只能幻觉作答。
- **方案比对与选择理由**：
  - *方案 A（彻底删除技术关键词）*：会导致在没有选择知识库的纯 Chat 模式下，无法正确分流技术问答；
  - *方案 B（知识库优先路由防护，最终采纳）*：只要请求显式携带 `knowledge_base_id` 且 `mode != "direct"`，一律无条件直通 `WorkflowRAGEngine`；通用技术关键词匹配仅在“未指定知识库”时作为兜底策略生效。
- **实际修改内容**：
  - 重构 `backend/app/routing/intent_router.py` 中的判定优先级，增加对明确指定知识库的硬性保护；完善单元测试 `test_intent_router.py` 覆盖 8 种复合边界。
- **真实对照实验验证结果**：
  - Baseline V1（修复前）：GEN-09（1分）、GEN-10（1分）、GEN-13（1分）、GEN-15（1分）；
  - E2.1 Final（修复后）：GEN-09（**5分**）、GEN-10（**5分**）、GEN-13（**3.25分**）、GEN-15（**5分**），全量回归走通 RAG 并命中黄金事实。
- **技术原理与面试阐述**：
  > “在企业多意图路由设计中，用户显式意图（Explicit Intent，如用户手动挂载了特定知识库）的优先级必须高于启发式模式推断（Heuristic Regex Matching）。旧系统用通用正则强行拦截带有 HTTP、JWT 的技术提问，引发了典型的意图覆盖漏洞。修复方案确立了‘显式知识库绑定优先’的确定性契约，从根本上杜绝了隐式降级。”

---

### ISS-002：未命中知识库时降级直连大模型导致合规拒答题产生幻觉

- **发现时间**：2026-11-06（第一轮 30 题 Benchmark 评测）
- **当前解决状态**：**`[已通过真实实验验证]`**
- **对应测试样本**：`GEN-26`（超出底库范围的备份系统设计）、`GEN-30`（超出底库范围的 OCR 协议）
- **发现指标与 Trace**：
  - Baseline V1 中 Rejection Accuracy 仅为 **66.67% (4/6)**；GEN-26 和 GEN-30 得分仅为 `1 / 1 / 1 / 2`；
  - 输出文本长达 1500~1700 字符，大模型凭借互联网公开常识洋洋洒洒编造技术规范，未触发拒答。
- **涉及代码与调用链**：
  - `backend/app/routing/intent_router.py` 与 `backend/app/rag/prompt_builder.py`
- **根本原因与证据**：
  - GEN-26 包含“备份日志”等词汇，被误判为附件缺失提问，绕过了 RAG 工作流；
  - 绕过 RAG 之后请求降级至直连大模型，脱离了 RAG 系统的约束提示词（System Prompt 中关于“底库无相关信息时明确说明未找到”的强制拒答指令）。
- **方案比对与选择理由**：
  - 确保超出底库边界的查询依然规范走入 `WorkflowRAGEngine`，经过向量 + BM25 检索后保留 0 个有效 Chunk 或低分 Chunk，进而触发 RAG Prompt 内部的确定性安全拒答模板。
- **真实对照实验验证结果**：
  - Baseline V1：拒答率 66.67%（GEN-26、GEN-30 编造答案）；
  - E2.1 Final：拒答率达到 **100.00% (6/6)**，两题均准确输出“参考资料中未提及相关信息”，获得 5/5/5/5 满分。
- **技术原理与面试阐述**：
  > “RAG 系统的合规拒答防线（Rejection / Out-of-Domain Detection）严重依赖完整的工作流闭环。若路由模块在检索前擅自将长尾问题旁路分流至通用大模型，实际上是瓦解了 RAG 的检索防御机制。必须让不可回答的问题同样走完检索管道，依靠空上下文或低置信度评分触发确定性拒答。”

---

### ISS-003：ASGITransport 内部缓冲导致 Client TTFT 首字延迟统计严重失真

- **发现时间**：2026-11-05（GEN-01 冒烟测试 Stage 1）
- **当前解决状态**：**`[已通过真实实验验证]`**
- **对应测试样本**：`GEN-01`
- **发现指标与 Trace**：
  - ASGITransport 测得 Client TTFT 为 **10,384 ms**，而总响应时延为 **10,412 ms**；
  - 首字延迟几乎等于总耗时，客户端表现为“长时间卡死后一次性收到全部内容”。
- **涉及代码与调用链**：
  - `eval/runners/run_generation_eval.py` -> `httpx.ASGITransport`
- **根本原因与证据**：
  - `httpx.ASGITransport` 运行在进程内内存管道中，其底层对异步生成器（`async for chunk in response.aiter_raw()`）存在迭代块缓冲，未实现物理 TCP Socket 的即时分包刷新（Flush），抹平了流式 SSE 的首字到达时效。
- **方案比对与选择理由**：
  - 保留 ASGITransport 用于纯单元测试逻辑校验；
  - 评测 Runner 重构为在评测进程内拉起独立 Uvicorn 绑定本地临时 TCP 端口（如 127.0.0.1:8001），使用标准 `httpx.AsyncClient` 经真实 TCP Socket 进行网络推流测量。
- **真实对照实验验证结果**：
  - ASGITransport 测得 TTFT：10,384 ms；
  - 真实 TCP Socket 测得 TTFT：**5,560.78 ms**（精准捕获到 1.54s 检索 + 3.42s LLM Prefill 的物理耗时）。
- **技术原理与面试阐述**：
  > “在流式系统（SSE / WebSocket）性能评测中，严禁使用进程内伪传输层（如 ASGI / WSGI In-memory Transport）测量首字延迟。内存传输缺乏 TCP 层的 Nagle 算法模拟与流控刷新机制，极易因应用层缓冲引发‘首字即尾字’的测量失真。工业级评测必须使用真实 TCP Loopback 通信。”

---

### ISS-004：Benchmark 黄金事实与底层真实数据库事实错位导致模型被误判跑题

- **发现时间**：2026-11-06（E2.1.3 只读分析阶段）
- **当前解决状态**：**`[已通过真实实验验证]`**
- **对应测试样本**：`GEN-22`（异步连接池参数）、`GEN-24`（超时重试机制）
- **发现指标与 Trace**：
  - 两题在 Baseline V1 中 Correctness 均为 **1 分**，但 Faithfulness 却高达 **5 分** 和 **4 分**；
  - 日志显示模型完全忠实于检索到的真实底库 Chunk，但被 LLM-as-a-Judge 判定为跑题。
- **涉及代码与调用链**：
  - `eval/datasets/benchmark_generation_v1.jsonl` vs 真实底库 Markdown 原文
- **根本原因与证据**：
  - `GEN-22`：旧 Benchmark 题干询问 `keepalive_timeout` 与 `connection_timeout`（基准制定者凭空想象的参数），而底库真实文档只定义了 `min_idle=10`, `max_connections=100`, `acquire_timeout_ms=3000ms`；
  - `GEN-24`：旧 Benchmark 题干询问指数退避 200/400/800ms，而底库真实文档规定的是“只读幂等 GET 重试 2 次，间隔 500ms，非幂等 POST 禁止重试”。
- **方案比对与选择理由**：
  - 严格读取 `backend/data/storage/eval_benchmark.db` 对应的原始文档，对齐客观事实；
  - 重新定稿 Benchmark V1.0 Final，修正两题题干与黄金答案，并计算 SHA256 固化。
- **真实对照实验验证结果**：
  - 修正后，GEN-22 Correctness 由 1 分升至 **5 分**；GEN-24 Correctness 由 1 分升至 **5 分**。
- **技术原理与面试阐述**：
  > “评估 LLM/RAG 系统时，必须防范‘评测集污染与黄金事实幻觉’。当模型真实检索到底库却得分极低时，不能盲目归咎于生成模型，必须回溯底库数据源（Grounding Truth Verification）。评测集的准确性直接决定了技术迭代的方向。”

---

### ISS-005：DeepSeek 思考模式与 Token 预算脱节导致长上下文流式输出截断

- **发现时间**：2026-11-06（E2.1 Final 审计阶段）
- **当前解决状态**：**`[已通过真实实验验证]`**
- **对应测试样本**：`GEN-11`（多跳故障转移与熔断降级问答）
- **发现指标与 Trace**：
  - Baseline V1 生成答案仅 43 字符截断，得分 1/1/1/1；Final 生成 326 字符并在 `### 2` 处再次截断，得分 3/4/4/2；
  - `server_ttft_ms: 14522.20 ms`（首字响应延迟高达 14.5 秒，模型内部进行了长时间思维链推理）；
  - `performance.output_tokens: 2048`（**刚好达到系统旧配置的兜底上限 `LLM_MAX_TOKENS = 2048`**）；
  - **历史数据说明**：经核验 E2.1 历史基线文件（`eval/reports/e2_1_final_generation_benchmark.json`），当时系统尚未记录 `finish_reason` 与 `reasoning_tokens` 字段，判定截断的客观证据为 `output_tokens == 2048` 及答案文本在关键编号处突断。
- **涉及代码与调用链**：
  - `backend/app/rag/workflow.py:986` -> `generate_stream()` 未显式传递预算参数，导致请求回退至 `settings.LLM_MAX_TOKENS`（2048），而 `ContextBudgetManager` 已规划预留 `reserved_output_tokens = 4096`，上下游预算严重脱节；
  - `backend/app/llm/openai_client.py:187` -> 缺省采用 2048 作为 API 请求体 `max_tokens`；且流式过程未保存 `finish_reason` 与官方 usage 元数据；
  - `backend/app/schemas/chat.py` -> `ExecutionInfo` 原先缺少 `finish_reason`, `is_truncated`, `generation_status`, `reasoning_tokens`。
- **根本原因与证据**：
  - 核心脱节：RAG 上下文预算管理模块（`ContextBudgetManager`）规划的 `reserved_output_tokens` 为 4096，但在 `WorkflowRAGEngine.execute_stream()` 调用 `generate_stream()` 时，未将该规划预算传入，直接穿透回退到了全局兜底配置 `LLM_MAX_TOKENS = 2048`；
  - 深度思考消耗：GEN-11 四跳逻辑推理引发 DeepSeek-R1 内部生成了大量 reasoning tokens（首字等待 14.5 秒），与最终正文共同挤占了发给 API 的 2048 输出预算，正文流式输出至 326 字符时触发服务端 `finish_reason=length` 强制截断；
  - 状态盲区：旧代码未对 `finish_reason` 进行捕获，发生截断或网络中断时，系统无法感知生成完整度。
- **实施修改方案**：
  1. **Token 预算动态贯通与上限保护**：
     - 在 `workflow.py` 调用 `generate_stream()` 时，动态提取 `plan.budget_info.reserved_output_tokens`（4096）；
     - 严格施加 `min(planned_budget, settings.MAX_OUTPUT_TOKENS)` 上限约束，缺失时安全回退 `settings.LLM_MAX_TOKENS`（2048）；
     - 全局 `LLM_MAX_TOKENS = 2048` 配置保持不变，避免波及非 RAG 流程。
  2. **请求级流式生成状态感知**：
     - `generate_stream()` 引入请求级 `response_metadata: Optional[Dict[str, Any]]`，并发隔离，绝不依赖全局共享实例状态；
     - 识别并记录 `finish_reason`（`stop` / `length` / `content_filter`），当为 `length` 时置 `is_truncated = True`, `generation_status = "truncated"`；
     - 网络异常时保留已生成的累积文本（`accumulated_answer`），落库并标记 `generation_status = "interrupted"`，不误判为正常完成；
     - 不向前端暴露或存储 `reasoning_content` 思维链正文，仅在官方 usage 存在时记录 `reasoning_tokens`；
     - 在 `ExecutionInfo` 和 SSE `trace` 事件中同步输出生成状态与用量。
- **真实实验验证结果（2026-11-09 在线验证）**：
  - 发起 1 次真实 DeepSeek 在线调用（报告归档于 `eval/reports/gen11_final_verification_result.json`）：
    1. **Payload 预算**：实际发送 HTTP JSON 请求体确认 `max_tokens = 4096`；
    2. **生成完整状态**：`finish_reason = stop`，`generation_status = "completed"`，`is_truncated = False`，成功接收 `[DONE]` 结束符；
    3. **Token 用量**：`prompt_tokens = 1178`，`completion_tokens = 486`（未触及 4096 上限，充裕输出）；
    4. **正文完整度**：最终正文达到 **843 字符**（历史 326 字符，提升 **+158.6%**），**彻底消除了在 `### 2` 处的突断**；
    5. **四跳逻辑全覆盖**：完整表述“30秒滑动窗口/5次失败/50%失败率触发 CLOSED→OPEN”、“OPEN 状态 100% 拦截主模型请求并返回降级兜底”、“执行 Failover 转移至 Secondary 模型 Qwen-2.5-72B 及本地 Llama-3.1 兜底”、“60秒冷静期自愈恢复半开试探”与“边界缺失项说明”；
    6. **事实引用准确性**：成功抽取 3 条关联引用标注（`Ref-1`、`Ref-2`、`Ref-4`），事实溯源无幻觉；
    7. **响应时延**：`Server TTFT: 1763.40 ms`，`Client TTFT: 7812.87 ms`，`Client Total: 10301.12 ms`（10.3 秒完成全部高密度技术推理）。
- **离线测试与回归结果**：
  - 专项测试集 `backend/tests/test_gen11_streaming_hardening.py`（12 项测试 100% 通过）；
  - 全库回归：全量 496 项单元/集成测试 100% 通过（0 失败，0 错误）。

---

### ISS-006：本地 CPU Cross-Encoder Reranker 推理延迟瓶颈

- **发现时间**：2026-11-06（E2.1 性能时延看板审计）
- **当前解决状态**：**`[已定位]`**（待 E2.2 专项优化）
- **对应测试样本**：全量 30 题
- **发现指标与 Trace**：
  - 检索阶段中：稠密向量耗时 ~15ms，BM25 耗时 ~3ms，RRF 耗时 ~0.07ms；
  - **`reranker_duration_ms`: P50 为 1543.15 ms**，占据全检索耗时的 **98.2%**。
- **涉及代码与调用链**：
  - `backend/app/retrieval/reranker.py` -> `LocalONNXReranker.rerank()`
  - 实际加载模型：`Xenova/bge-reranker-base`（ONNX INT8 量化版）
- **根本原因与证据**：
  - Cross-Encoder 需要将 Query 与每个候选 Chunk 进行成对字符串拼接，逐个输入 Transformer 编码器计算全交互注意力（All-to-all Attention）。在 CPU 单线程/轻量多线程环境下，候选集为 20 时计算量较大。
- **规划优化方向（E2.2）**：
  - 评估 Top-K 前置过滤（将候选池从 20 缩减至 10~12）；
  - 引入异步线程池与 ONNX Runtime 并发执行；或在有 GPU 环境时开启 TensorRT 加速。

---

### ISS-007：工具调用参数校验错误反馈欠缺语义引导且无有界自修复上限（P0-1）

- **发现时间**：2026-11-06（E2.2 Phase 1 问题复现矩阵测试）
- **当前解决状态**：**`[已通过回归测试]`**
- **对应测试样本**：`TC-03a` 至 `TC-03f`（专项测试集 `backend/tests/test_p0_1_param_validation_hardening.py`）
- **发现指标与 Trace**：
  - 修复前：模型连续传递非法参数时，系统无参数修复重试上限，`tool_messages` 产生 4 次连续报错并机械消耗 4 步直到 `max_steps`；报错内容为 Python 原始字典 `val_err.errors()`，包含内部输入与报错网址，存在敏感参数泄漏风险。
  - 修复后：
    1. 结构化错误指引：自动从 Pydantic `args_schema` 提取字段类型、必填状态、默认值与描述，区分“缺少必填参数”、“类型不符”和“校验失败”，不暴露 Traceback，不回显用户原始输入；
    2. 有界自修复截断：采用 `max_consecutive_param_errors=2`，第一次报错允许一次自修正机会，第二次连续报错直接阻断后续工具执行并禁止下一轮工具调用（`tools_param=None, tool_choice='none'`）；
    3. 状态重置机制：只要参数通过 Schema 校验（即使工具抛出运行时异常或超时），计数器立即重置为 0；
    4. 批次消息配对完整性：单条 AIMessage 内达到上限时，被阻断工具产生 `status: blocked` 的标准 ToolMessage，保证 `tool_call_id` 100% 配对。
- **涉及代码与修改清单**：
  - `backend/app/agent/tools/schemas.py`：`ToolResult` 新增 `error_type: Optional[str] = None` 字段；
  - `backend/app/agent/tools/base.py`：新增 `_format_validation_error`，提取 Pydantic Schema 规范；`run()` 捕获 `ValidationError` 标记 `error_type="param_validation"`；
  - `backend/app/agent/runtime/state.py`：`AgentState` 增加 `consecutive_param_errors: int`；
  - `backend/app/agent/runtime/runtime.py`：`ainvoke` 与 `astream_events` 初始状态均初始化 `consecutive_param_errors=0`；
  - `backend/app/agent/runtime/nodes.py`：`agent_node` 在达到上限时置 `tools_param=None`；`tool_node` 精确累加/重置计数并在批次超限时生成 blocked ToolMessage；
  - `backend/app/agent/runtime/routing.py`：超限时安全路由至 `force_stop_node`。
- **自动化测试回归验证**：
  - 6 项新增 P0-1 专项测试（TC-03a ~ TC-03f）全部 100% 通过；
  - 全套 101 项测试（82 项原套件 + 13 项复现矩阵 + 6 项 P0-1）全部通过（耗时 17.39s，0 外部 API 调用，0 Token 消耗）。

---

### ISS-008：连续相同工具+相同参数的无效调用缺乏防震荡防御机制（P0-2）

- **发现时间**：2026-11-06（E2.2 Phase 1 问题复现矩阵测试）
- **当前解决状态**：**`[已通过回归测试]`**
- **对应测试样本**：`TC-09a` 至 `TC-09f`（专项测试集 `backend/tests/test_p0_2_duplicate_call_defense.py`）及 `test_e2_2_tool_calling_hardening.py`
- **发现指标与 Trace**：
  - 修复前：模型在连续 3 轮交互中发出完全相同的工具调用 `calculator_add(a=1, b=1)`，底座工具被机械执行了 3 次（`call_count=3`），在未召回结果时极易陷入复读机式空耗死循环；
  - 修复后：
    1. 规范化指纹机制：`compute_tool_fingerprint()` 对参数键升序排序、剔除 None 值、剥离字符串首尾空白，实现跨调用语义等价归一化（如 `{"query":"a", "k":1}` 与 `{"k":1, "query":"a "}` 归一为同一指纹）；
    2. 无意义执行拦截：检测到当前 Run 历史已执行相同指纹时，立即阻断底层 `_run()` 调用，直接返回 `status: duplicate_blocked` 提示模型调整参数或总结现有信息；
    3. 避免误拦截正常业务：仅判定 `tool_name + canonical_args` 完全相同者为重复，同一工具使用不同参数（如多次检索不同关键词）完全放行，不受影响；
    4. 连续重复有界熔断：引入 `consecutive_duplicate_calls` 计数器，上限设为 2，超限后 `agent_node` 禁用后续工具调用（`tools_param=None, tool_choice='none'`），防止无限重试；当模型主动调整参数时，计数器立即重置为 0；
    5. 消息契约完整性：批次并发调用中含重复项时，被阻断工具产生对应 `ToolMessage`，保证 `tool_call_id` 1:1 配对。
- **涉及代码与修改清单**：
  - `backend/app/agent/runtime/state.py`：`AgentState` 增加 `consecutive_duplicate_calls: int` 与 `executed_tool_fingerprints: List[str]`；
  - `backend/app/agent/runtime/runtime.py`：初始状态重置为 `0` 与 `[]`，确保 Run 级别隔离；
  - `backend/app/agent/runtime/nodes.py`：实现 `compute_tool_fingerprint()`；在 `tool_node` 中实施指纹比对、阻断拦截、Trace 记录与新调用计数重置；在 `agent_node` 达到上限时剥夺工具；
  - `backend/app/agent/runtime/routing.py`：超限时双重防御路由至 `force_stop_node`。
- **自动化测试回归验证**：
  - 6 项新增 P0-2 专项测试（TC-09a ~ TC-09f）全部 100% 通过；
  - 全套 107 项测试（82 项原套件 + 13 项复现矩阵 + 6 项 P0-1 + 6 项 P0-2）全部 100% 通过（耗时 14.61s，0 外部 API 调用，0 Token 消耗）。

---

### ISS-009：畸形参数与 invalid_tool_calls 被 routing 静默丢弃且部分异常无 Trace（P0-3）

- **发现时间**：2026-11-06（E2.2 Phase 1 问题复现矩阵测试）
- **当前解决状态**：**`[已通过回归测试]`**
- **对应测试样本**：`TC-05a` 至 `TC-05g`（专项测试集 `backend/tests/test_p0_3_malformed_json_and_trace_hardening.py`）及 `test_e2_2_tool_calling_hardening.py`
- **发现指标与 Trace**：
  - 修复前：
    - 大模型由于 JSON 格式错误被客户端解析为 `invalid_tool_calls` 时，`tool_messages` 产生数量为 0，请求被悄无声息地绕过 `tool_node`，直接进入最终汇总（违反 OpenAI/LangChain 工具消息配对契约）；
    - 下一轮通过 `messages_to_openai_payload` 发送给模型时，Assistant 消息内缺少对 `invalid_tool_calls` 的声明，导致 `ToolMessage` 携带未声明的孤立 `tool_call_id`，会引发 OpenAI API HTTP 400 校验拦截；
    - 模型输出带 Markdown 代码块围栏 ` ```json ... ``` ` 时直接抛出 `json.JSONDecodeError`；
    - 在 `nodes.py:206`（JSON 字符串解析错误）和 `nodes.py:248`（未注册工具）发生时，未向 `traces` 列表中追加 trace 事件，导致前端/可观测系统丢失错误时间线。
  - 修复后：
    1. 路由与消息配对感知：`route_agent_decision` 增强对 `invalid_tool_calls` 感知，将含有工具调用意图的请求正确路由至 `tool_node`，生成标准 `ToolMessage` 回传语法错误给模型，保证 `tool_call_id` 100% 配对；
    2. 序列化孤立 ID 消除与协议对齐：`messages_to_openai_payload` 将 `invalid_tool_calls` 同步在 Assistant 消息中作为 `tool_calls` 声明，并在缺失 ID 时就地赋值保证 ID 绝对一致，消除孤立 `tool_call_id`；同时保持 `tool_calls` 与 `invalid_tool_calls` 先后顺序与 `ToolMessage` 完全一致；
    3. 宽松 Markdown 代码块剥离：实现 `clean_and_parse_json`，鲁棒兼容 ` ```json ... ``` ` 围栏与首尾空白，双层防护（LLM 客户端解析层 + Runtime 执行层），绝不猜测或修改语义；
    4. 有界格式错误熔断：引入独立的 `consecutive_json_errors` 计数器（与参数校验错误及重复调用计数器隔离），上限设为 2，连续两次输出坏 JSON 后自动剥夺工具调用权限（`tools_param=None, tool_choice='none'`）并促使模型根据现有事实总结，防止无限死循环；
    5. Trace 探针全量补齐：为 `tool_malformed_json_error`、`tool_arguments_json_error`、`tool_not_registered` 补齐结构化 Trace 事件，记录耗时、调用 ID、工具名称与详细报错。
- **涉及代码与修改清单**：
  - `backend/app/agent/tools/schemas.py`：实现 `clean_and_parse_json(raw: Any) -> Any`；
  - `backend/app/llm/openai_client.py`：在 `generate_with_tools` 中调用 `clean_and_parse_json` 宽容解析参数；在 `messages_to_openai_payload` 中完整序列化 `invalid_tool_calls` 并确保 ID 严格对齐；
  - `backend/app/agent/runtime/state.py`：`AgentState` 增加 `consecutive_json_errors: int`；
  - `backend/app/agent/runtime/runtime.py`：`ainvoke` 与 `astream_events` 初始状态均初始化 `consecutive_json_errors=0`；
  - `backend/app/agent/runtime/routing.py`：`route_agent_decision` 感知 `invalid_tool_calls`，超限时安全路由至 `force_stop_node`；
  - `backend/app/agent/runtime/nodes.py`：`agent_node` 达到格式错误上限时禁用工具；`tool_node` 保持严格顺序，就地补齐缺失 ID，遍历并回传 `invalid_tool_calls` 错误消息，使用 `clean_and_parse_json` 解析字符串参数，补齐 `tool_malformed_json_error`、`tool_arguments_json_error`、`tool_not_registered` 的 Trace 记录。
- **自动化测试回归验证**：
  - 7 项新增 P0-3 专项测试（TC-05a ~ TC-05g）全部 100% 通过；
  - 13 项 E2.2 工具加固测试全部 100% 通过；
  - 全套 114 项回归测试全部通过，0 外部 API 调用，0 Token 消耗。

---

## 缺陷维护总览表

| 缺陷编号 | 缺陷名称 | 严重度 | 发现时间 | 当前状态 | 验证方式 |
| :---: | :--- | :---: | :---: | :---: | :--- |
| **ISS-001** | IntentRouter 意图路由技术关键词误分流 | **P0 (阻塞)** | 2026-11-06 | **`[已通过真实实验验证]`** | 30题 Benchmark 真实对比 (GEN-09,10,13,15) |
| **ISS-002** | 知识库未命中请求降级直连导致拒答题幻觉 | **P0 (阻塞)** | 2026-11-06 | **`[已通过真实实验验证]`** | 30题 Benchmark 真实对比 (GEN-26,30) |
| **ISS-003** | ASGITransport 缓冲导致 Client TTFT 统计失真 | **P1 (严重)** | 2026-11-05 | **`[已通过真实实验验证]`** | GEN-01 冒烟测试 V2 (真实 TCP 5.5s 对比) |
| **ISS-004** | Benchmark 黄金事实与真实数据库错位 | **P1 (严重)** | 2026-11-06 | **`[已通过真实实验验证]`** | 数据集定稿 V1.0 Final + 30题全量验证 |
| **ISS-005** | DeepSeek 思考模式与 Token 预算脱节导致流式输出截断 | **P2 (主要)** | 2026-11-06 | **`[已通过真实实验验证]`** | GEN-11 真实 API 在线验证 (843字符无截断, finish_reason=stop) |
| **ISS-006** | 本地 CPU Cross-Encoder Reranker 推理延迟高 | **P2 (主要)** | 2026-11-06 | **`[已定位]`** | 性能看板 P50 1543ms 占比 98% |
| **ISS-007** | 参数校验错误反馈欠缺语义且无有界重试上限 | **P0 (阻塞)** | 2026-11-06 | **`[已通过回归测试]`** | TC-03a~f 自动化测试通过 (6/6 100%) |
| **ISS-008** | 连续相同工具+相同参数缺乏防震荡防御机制 | **P0 (阻塞)** | 2026-11-06 | **`[已通过回归测试]`** | TC-09a~f 自动化测试通过 (6/6 100%) |
| **ISS-009** | 畸形参数/invalid_tool_calls 被静默丢弃与Trace缺失 | **P0 (阻塞)** | 2026-11-06 | **`[已通过回归测试]`** | TC-05a~f 自动化测试通过 (6/6 100%) |
