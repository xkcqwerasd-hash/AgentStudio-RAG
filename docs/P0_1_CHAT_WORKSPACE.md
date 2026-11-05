# AgentStudio-RAG — P0.1 统一智能问答工作台交付报告

## 一、产品定位与架构设计

### 1.1 产品定位
AgentStudio-RAG 正式定位为**开箱即用的企业级智能知识助手（Enterprise AI Assistant）**。
- **无需编排配置**：用户无需学习 Dify 式复杂低代码画布、无需手动拖拽节点或注册 Python 工具；
- **内置双核引擎**：系统开箱即内置「快速问答」（Workflow RAG 检索引擎）与「深度分析」（LangGraph Agent 自主执行引擎）；
- **极简用户心智**：用户只需创建/选择知识库、配置模型 API Key，即可在统一工作台提问并获得高质量带引用可溯源的回答。

### 1.2 统一工作台架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      AgentStudio ChatPage 工作台                        │
├────────────────────────────────┬────────────────────────────────────────┤
│          左侧会话侧边栏        │               右侧问答主区             │
│  - 历史会话列表                │  [顶部导航栏]                          │
│  - 新建对话                    │   - 知识库选择器 (Select)              │
│  - 会话模式标识 (⚡/🧠)         │   - 模式切换器 (Segmented: ⚡快速/🧠深度) │
│  - 删除/重命名会话             │   - 模型状态与清空操作                 │
│                                ├────────────────────────────────────────┤
│                                │  [消息流主视窗]                        │
│                                │   - 用户提问气泡                       │
│                                │   - AgentTimelineCard (执行步骤折叠卡) │
│                                │   - Assistant Markdown 渲染消息        │
│                                │   - 证据来源角标 [1] 与 CitationDrawer │
│                                ├────────────────────────────────────────┤
│                                │  [输入控制区]                          │
│                                │   - 附件上传占位 (P0.2 预留)           │
│                                │   - 多行自适应输入框                   │
│                                │   - 发送/中止流式生成按钮              │
└────────────────────────────────┴────────────────────────────────────────┘
```

---

## 二、双执行模式机制与时序差异

| 维度 | ⚡ 快速问答 (Workflow RAG) | 🧠 深度分析 (LangGraph Agentic) |
| :--- | :--- | :--- |
| **底层引擎** | `WorkflowRAGEngine` (M1~M3.3) | `LangGraphAgentRuntime` (M4.1) |
| **API 端点** | `POST /api/v1/chat/completions/stream` | `POST /api/v1/agents/{agent_id}/runs/stream` |
| **执行逻辑** | 固定流水线：QueryRewrite → HybridSearch (BGE+BM25) → RRF → Cross-Encoder → ContextBudget → LLM 生成 | 自主决策循环：Agent → Decision → Tool Call (`search_knowledge_base`) → Observation → Synthesis → Answer |
| **适用场景** | 明确的事实检索、高吞吐、单步问答、秒级低延迟响应 | 复杂多步逻辑推导、跨多篇文档综合分析、需自我验证检索效果的深度任务 |
| **前端呈现** | 直接流式吐字，右侧展开 CitationDrawer | 先展现 `AgentTimelineCard` 实时动态步骤，再展现流式回答与引用证据 |

---

## 三、SSE 事件映射与 AgentTimelineCard

### 3.1 SSE 统一协议适配
前端适配层 `sendAgentRunStream`（[frontend/src/api/chat.ts](file:///e:/develop/python agentic RAG/frontend/src/api/chat.ts)）接收后端 SSE 流事件并进行实时响应状态机映射：

| 后端 SSE 事件 | 负载核心字段 | 前端响应动作 |
| :--- | :--- | :--- |
| `run_started` | `run_id`, `conversation_id`, `agent_id` | 锁定当前会话 ID，初始化 Timeline 状态与步骤容器 |
| `step_start` | `step`, `node` | 在 Timeline 中追加步骤卡，显示 `agent` / `tools` 节点加载状态 |
| `tool_call_start` | `tool_call_id`, `tool_name`, `tool_args` | 在当前步骤下挂载工具调用，展示工具名（如 `知识库语义检索`）及参数解析 |
| `tool_call_end` | `tool_call_id`, `status`, `duration_ms`, `observation_snippet` | 记录执行耗时（毫秒）、成功状态与观测数据摘要 |
| `evidence_emitted` | `evidence_id`, `evidence_index`, `title`, `snippet` | 收集结构化证据，同步更新全局 Evidence 供 CitationDrawer 溯源 |
| `token` | `delta` | 将增量文本实时追加到 Markdown 消息气泡中 |
| `run_completed` | `total_duration_ms`, `total_tokens`, `tool_calls_count` | 标记 Timeline 状态为 `completed`，并持久化到消息历史中 |
| `run_failed` | `error` | 标记 Timeline 状态为 `failed`，呈现错误告警 |

### 3.2 AgentTimelineCard 组件设计
- **文件路径**：[frontend/src/components/AgentTimelineCard.tsx](file:///e:/develop/python agentic RAG/frontend/src/components/AgentTimelineCard.tsx)
- **视觉风格**：采用与 Ant Design 5 主题融为一体的毛玻璃卡片（微浅灰色背景、微细圆角边框）。
- **折叠交互**：
  - 执行过程中：默认展开，显示当前活跃步骤的 Spin 动画与耗时；
  - 执行完成：默认折叠为收敛摘要栏（“🧠 深度思考完成 · 耗时 1.82s · 1 次工具调用 · 3 篇参考证据”），点击可平滑展开全部审计细节；
  - 工具调用展开：支持展开查看入参格式化 JSON 与检索返回的文档片段片段。

---

## 四、会话持久化与零重复设计

### 4.1 统一数据持久化
- `backend/app/api/v1/agent.py` 与 `backend/app/api/v1/chat.py` 严格共享底层 `Conversation` 与 `Message` 表结构；
- 当 Agent 模式收到请求时：
  1. 若 `conversation_id` 为空，自动以 `mode="agentic"` 创建新会话，首事件 `run_started` 返回其 ID，前端自动写入 URL 或会话状态；
  2. 若 `conversation_id` 存在，自动拉取历史 `Message` 记录组装为多轮 LangGraph `initial_messages`；
  3. 执行完毕时，在流终态一次性写入用户消息与助手消息，并将 `agent_timeline`（包含耗时、工具调用、步骤信息）保存在 `Message.execution_info_json` 中；
  4. 刷新页面或切换会话时，历史消息直接从 `execution_info_json` 还原 `AgentTimelineCard`，杜绝重复插入或漏记。

### 4.2 跨模式无缝交互验证
- 用户第一轮在「⚡ 快速问答」提问，模型写入 Workflow 答案与引用；
- 用户第二轮在同一会话切换为「🧠 深度分析」提问，Agent Runtime 能完整继承上一轮对话上下文作为决策依据；
- 双向切换平滑无冲突，会话记录始终保持绝对线性有序。

---

## 五、自动化测试与验收结果

### 5.1 新增自动化集成测试
创建集成测试套件 [backend/tests/test_p0_1_workspace.py](file:///e:/develop/python agentic RAG/backend/tests/test_p0_1_workspace.py)，覆盖五大核心工作台场景：
1. `test_default_agent_auto_creation_and_retrieval`: 验证内置默认 Agent 的自愈与幂等初始化，支持 `agent_id="default"` 与 `DEFAULT_AGENT_ID`；
2. `test_agent_stream_with_default_agent_and_auto_conversation`: 验证缺省 `conversation_id` 时的自动创建机制与 SSE 流全量事件下发；
3. `test_agent_stream_dynamic_knowledge_base_override`: 验证工作台下拉切换知识库时，对默认 Agent 绑定知识库的动态覆盖能力；
4. `test_multi_turn_cross_mode_continuity`: 验证 Turn 1 (Workflow) → Turn 2 (Agentic) 的跨执行模式多轮会话上下文延续；
5. `test_agent_timeline_summary_in_message_execution_info`: 验证执行完成时 `agent_timeline` 正确存入 `Message.execution_info_json` 并可反序列化还原。

### 5.2 全量测试验证
- **后端测试**：
  ```bash
  backend\.venv\Scripts\python.exe -m pytest backend/tests -q
  # 输出: 176 passed in 21.63s
  ```
  176 项单元与集成测试 **100% 全部通过**（171 项已有测试 + 5 项 P0.1 测试）。
- **前端构建**：
  ```bash
  cd frontend && npm run build
  # 输出: 3100 modules transformed, built in 3.63s, 0 errors, 0 warnings
  ```
  TypeScript 类型检查与生产打包 **0 错误通过**。

---

## 六、已知局限与 P0.2 交接说明

### 6.1 本阶段边界确认
- 本阶段已严格遵守承诺，**未实现**低代码画布、**未引入** Redis/Celery、**未改动** M1~M3.3 核心检索算法；
- 聊天输入框左下角附件上传入口为 UI 占位（Tooltip 明确提示“多格式附件解析将在 P0.2 开放”）。

### 6.2 P0.2 规划承接
P0.2 将聚焦于增强工作台的输入与模型生态：
1. **多格式文档即时上传与解析**（PDF、Word、Markdown 临时对话附件索引）；
2. **多模型接入与配置中心**（DeepSeek、OpenAI、Ollama 本地大模型动态切换与健康检测）。
