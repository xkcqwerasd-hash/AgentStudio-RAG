# AgentStudio-RAG — 产品实施路线图 (Product Implementation Roadmap)

> **版本**：v1.0 (战略重塑版)  
> **制定日期**：2026-11-02  
> **核心定位**：开箱即用的企业级智能知识助手（基于 Python + LangGraph + BGE/RRF/Reranker）  
> **指导原则**：严格基于已有 M1～M4.1 成果，不推翻已有架构，不开发 Dify 拖拽画布，专注于极简体验与可靠知识推理。

---

## 一、路线图规划全景

```
+---------------------------------------------------------------------------------------+
|                 AgentStudio-RAG 开箱即用智能知识助手演进全景                           |
+---------------------------------------------------------------------------------------+
|  [P0: 核心工作台与能力打通] (当前立即启动)                                            |
|   1. 统一聊天工作台 (Chat Workbench)：双模式切换 (⚡ Workflow 快速 / 🧠 Agentic 深度)  |
|   2. Agent 思考与工具调用折叠时间线：前端渲染思维链、工具调用入参、耗时与 Observation   |
|   3. 聊天临时附件上传与即时解析：支持 PDF/Word/TXT，会话级临时上下文注入 (不污染知识库) |
|   4. 多模型注册与运行时切换：云端 API (DeepSeek等) 与本地模型 (Ollama) 动态选择与探查   |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|  [P1: 企业级生产增强] (次优先级)                                                      |
|   1. 会话记忆长上下文摘要压缩 (Summary Memory)：超预算时自动提炼历史，替代物理硬截断   |
|   2. MCP 标准工具协议接入 (Model Context Protocol)：挂载企业外部数据与服务             |
|   3. 聊天图片多模态理解 (Vision)：支持用户上传截图与报表图片并结合知识库问答           |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|  [P2: 后续演进与明确排除] (暂缓或排除)                                                 |
|   - 暂缓：Human-in-the-loop 人工审批拦截流 (仅在接入写操作/危险工具时启用)            |
|   - 暂缓：生态化 Skills 插件市场                                                      |
|   - [明确排除]：Dify 式低代码拖拽工作流画布编辑器 (不符合开箱即用定位)                |
|   - [明确排除]：非必要引入 Redis、RocketMQ、Kafka 等重型中间件                        |
+---------------------------------------------------------------------------------------+
```

---

## 二、P0 核心任务详细实施设计 (立即执行阶段)

### 任务 1：统一智能问答工作台与双执行模式整合 (Dual-Mode Integration)

#### 1. 业务目标
用户在单一聊天界面中，既可以享受毫秒级响应、确定性高的**快速检索模式 (Workflow RAG)**，也可以在面对复杂对比、多步推导问题时切换到**深度推理思考模式 (Agentic ReAct)**，无需创建或配置任何 Agent。

#### 2. 后端接口设计 (`/api/v1/chat/completions/stream`)
将原有的 `chat.py` 扩充为统一智能会话分流网关：
- **请求参数扩充**：
  ```python
  class UnifiedChatRequest(BaseModel):
      knowledge_base_id: Optional[str] = Field(None, description="挂载的目标知识库ID (可选)")
      query: str = Field(..., min_length=1, description="用户提问")
      conversation_id: Optional[str] = Field(None, description="会话ID")
      mode: Literal["workflow", "agentic"] = Field(default="workflow", description="执行模式")
      model: Optional[str] = Field(None, description="指定的LLM模型名称")
      temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
      top_k: int = Field(default=4, ge=1, le=20)
      attachment_ids: Optional[List[str]] = Field(default=None, description="本次提问关联的聊天临时附件ID")
  ```
- **核心分流逻辑**：
  - `mode == "workflow"`：复用已有的 `WorkflowRAGEngine`，执行 `Query Rewriting -> Hybrid Retrieval -> RRF -> Context Budget -> LLM Stream -> Citation Verification`。
  - `mode == "agentic"`：在内部直接调用出厂内置的 `LangGraphAgentRuntime` 单例，挂载默认内置工具集（`search_knowledge_base` 及后续临时附件工具）。
- **统一 SSE 事件规范**：
  前端采用统一的事件监听器，事件集标准化归并：
  - 公共事件：`start`, `citations`, `final_answer` (或 `token`), `trace`, `done`, `error`
  - Workflow 特有：`query_rewrite`, `retrieval`, `context_budget`
  - Agentic 特有：`agent_step_completed`, `tool_call_started`, `tool_call_completed`

#### 3. 前端界面设计
- **顶部模式切换胶囊 (Mode Switcher)**：
  在 ChatPage 顶部标题旁或输入框上方放置精美的 Ant Design `Segmented` 或 `Radio.Group` 切换器：
  - `⚡ 快速检索 (Workflow)`：提示“单跳精准检索，极速响应，低消耗”
  - `🧠 深度思考 (Agentic)`：提示“多步自主推理，多轮工具调用，解决复杂问题”
- **执行过程折叠时间线组件 (AgentTimelineCard)**：
  - 在 AI 回答气泡上方渲染一个紧凑的可折叠面板：
    - 未展开时展示简要状态：“🧠 已完成 2 步思考，调用 1 次知识库检索 (耗时 1.2s)”
    - 点击展开后展示详细时间线：
      - `步骤 1 (模型分析)`：识别用户意图，生成检索查询词；
      - `步骤 1 (工具执行)`：调用 `search_knowledge_base`，入参 `{"query": "..."}`，耗时 180ms，命中 4 条真实分块；
      - `步骤 2 (最终答复)`：基于证据综合生成专业结论。

---

### 任务 2：聊天即时附件上传与临时上下文注入 (Chat Ephemeral Attachments)

#### 1. 业务目标
用户在聊天提问时，可以随时上传一份或多份临时文档（PDF、Word、TXT、Markdown），提出“请帮我总结这份附件的第3条要求”或“结合知识库比对这份报价单”。
**附件不写入持久化知识库，不污染全局向量索引，仅在会话生命周期内有效**。

#### 2. 后端数据结构与解析扩展
1. **轻量化附件模型**：
   在 `backend/app/models/conversation.py` 中新增 `ChatAttachment` 表：
   ```python
   class ChatAttachment(Base):
       __tablename__ = "chat_attachments"
       id: Mapped[str] = mapped_column(String(36), primary_key=True)
       conversation_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True)
       file_name: Mapped[str] = mapped_column(String(255), nullable=False)
       file_type: Mapped[str] = mapped_column(String(32), nullable=False) # pdf, docx, txt, md
       file_size: Mapped[int] = mapped_column(Integer, nullable=False)
       file_path: Mapped[str] = mapped_column(String(512), nullable=False)
       extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
       created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
   ```
2. **新增 Word (.docx) 解析器**：
   在 `backend/app/ingestion/parsers/` 下新增 `docx_parser.py`（使用轻量 `python-docx` 库），并注册到 `ParserFactory`。
3. **临时上下文组装机制 (Ephemeral Context Injection)**：
   - 当用户发送请求带 `attachment_ids` 时：
     - 若文档较小（< 3000 tokens）：直接作为高优先级专属 Context 注入 Prompt 顶层，标记来源为 `[临时附件: 文件名]`；
     - 若文档较大（长篇 PDF/Word）：通过内存 BM25 进行即时检索召回高分片段后注入，确保不超出 Token Budget。

#### 3. 前端附件交互
- 在聊天输入框左下角新增“📎 添加附件”图标按钮，支持文件选择和拖拽放置；
- 上传后以蓝色小徽章（Tag/Chip）展示在输入框上方，显示文件名与大小，支持一键点击“✖”移除；
- 发送后，用户消息气泡上方常驻显示该条消息所引用的附件标识。

---

### 任务 3：多模型注册表与运行时模型切换 (Multi-Model Registry & Switching)

#### 1. 业务目标
支持用户在前端下拉框自由选择不同的模型，既支持高性价比云端 API（DeepSeek-V3 / SiliconFlow / 阿里百炼），也支持本地已安装的 Ollama 模型（如 Qwen2.5、Llama3 等），开箱即连。

#### 2. 后端设计
1. **模型元数据注册表 (`app/llm/registry.py`)**：
   内置预置提供商配置（支持通过环境变量或设置扩展）：
   - `deepseek-chat` (DeepSeek-V3)
   - `deepseek-reasoner` (DeepSeek-R1)
   - `local-ollama` (动态从本地探测)
2. **本地 Ollama 模型自动探测接口 (`GET /api/v1/models`)**：
   - 请求本地 `http://localhost:11434/api/tags`（超时 1.5s），若探测成功，自动将本地所有离线模型（如 `qwen2.5:7b`）合并输出到模型列表中并打上 `[本地]` 标签；
   - 若本地 Ollama 未启动，仅返回已配置的云端可用模型列表，保证健壮性。
3. **运行时模型分发**：
   在 `OpenAILLMClient` 中支持根据请求传入的 `model` 和相应的 `base_url` 实例化或切换调用目标。

#### 3. 前端交互
- 顶部导航栏右侧新增“模型切换器”下拉选择框（Select），例如：
  - `⚡ DeepSeek-V3 (云端智能推荐)`
  - `🧠 DeepSeek-R1 (深度逻辑推理)`
  - `💻 Qwen2.5:7b (本地 Ollama)`
- 用户切换后即时生效，会话记忆该偏好。

---

## 三、P1 增强与扩展能力实施设计 (次优先演进)

### 1. 会话记忆长上下文摘要压缩 (Summary Memory)
- **背景**：当前多轮对话超出 `HISTORY_TOKEN_BUDGET` 时采用生硬的丢弃最早轮次策略。
- **方案**：引入后台轻量异步任务，当轮次超过阈值时，调用 LLM 生成一段紧凑的《历史会话背景与核心共识摘要》（< 300 tokens），替代被丢弃的原始多轮文本，既保证长程信息不丢失，又严控 Token 成本。

### 2. MCP 标准工具协议接入 (Model Context Protocol)
- **背景**：企业内部可能已有 MCP Server（如查 ERP 订单、查 GitHub 仓库、查企业数据库）。
- **方案**：
  - 引入官方 `mcp` Python SDK；
  - 在 `ToolRegistry` 中实现 `MCPToolAdapter`，支持配置连接到外部 `stdio` 或 `sse` 模式的 MCP Server，将其远程工具暴露为标准的 OpenAI Function Calling Schema，无缝挂载给 LangGraph Agent Runtime。

### 3. 聊天图片多模态理解 (Vision)
- **背景**：用户可能直接在聊天框粘贴架构图、错误弹窗截图或报表照片。
- **方案**：
  - 支持上传 `.png`, `.jpg`, `.jpeg`；
  - 后端在 `messages_to_openai_payload` 中支持将图片转为 Base64 或本地文件 URL 格式组装为 `image_url`，分发给支持多模态的云端模型（如 Qwen-VL、GPT-4o）。

---

## 四、P2 暂缓功能与明确不开发边界

### 1. 暂缓功能
- **Human-in-the-loop (人工审批确认中断)**：
  目前知识库助手的所有内置工具（检索知识库、检索附件、文档分析）均为**只读幂等工具**，不存在数据删除或敏感写操作。过早引入审批弹窗只会严重打断用户问答体验。待后续接入外部写操作 MCP 时再启用。
- **自定义 Skills 编排市场**：
  开箱即用产品优先打磨系统内置的核心知识决策与检索能力，暂缓开放用户自制 Prompt Playbooks 市场。

### 2. 明确排除开发（坚决不做的边界）
- **❌ 坚决不开发 Dify 式低代码画布**：
  不写节点拖拽、连线引擎、分支判断配置器，不强迫用户“先学会搭 Agent 才能用系统”。
- **❌ 坚决不盲目引入重型运维中间件**：
  不引入 Redis（会话和状态直接利用 SQLite/Postgres + 内存即可满足数千并发需求）、不引入 RocketMQ/Kafka。保持系统“单机 `python main.py` 即可极速启动并完整运行”的轻量化优势。

---

## 五、P0 阶段研发执行计划与验收指标

| 阶段 | 核心任务模块 | 关键技术动作 | 验收指标 (Definition of Done) |
| :---: | :--- | :--- | :--- |
| **P0-1** | **执行模式统一网关** | 改造 `chat.py` 支持 `mode="workflow" / "agentic"`，打通两套底层引擎 | 单一端点同时支持两类模式流式推送，100% 兼容历史会话 |
| **P0-2** | **前端双模式与工具时间线** | ChatPage 增加模式切换胶囊，实现可折叠 Agent 时间线组件 | 前端平滑切换模式，Agent 运行时清晰展示步骤、工具名与耗时 |
| **P0-3** | **聊天临时附件体系** | 新增 `chat_attachments` 表，实现 Word (.docx) 解析与临时上下文注入 | 用户可在输入框上传 PDF/Word/TXT，AI 回答应答附件内容且不污染知识库 |
| **P0-4** | **多模型与本地 Ollama 适配** | 实现 `GET /api/v1/models`，探查本地 Ollama，支持请求级指定模型 | 前端可下拉切换模型，若本地 Ollama 启动能自动识别并成功调用 |
| **P0-5** | **全量集成与回归测试** | 编写端到端联合测试集，全量回归现有 171 项测试 | 所有新用例与既有 171 项测试 100% 通过，前端生产构建 0 报错 |

---

## 六、总结

本实施路线图确立了 AgentStudio-RAG 作为**开箱即用企业知识助手**的产品航标：
通过充分释放底层已有强大的 BGE/BM25/RRF/Reranker 混合检索能力与 LangGraph Agent 执行力，补齐前端双模式问答工作台、工具调用时间线、聊天即时附件和多模型切换能力，打造一款体验媲美一流商业产品、架构坚实且极易部署的企业级 AI 知识中枢。
