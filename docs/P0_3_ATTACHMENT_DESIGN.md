# AgentStudio-RAG — P0.3 聊天附件与临时上下文管理架构设计方案

**设计阶段**：P0.3 架构预研与实施蓝图  
**设计定位**：对话即时附件（Chat Attachments）与临时上下文管理（Ephemeral Context Management）  
**前序版本**：P0.1 统一智能问答工作台、P0.2 模型管理中心  

---

## 一、产品定位与核心目标

在企业实际问答场景中，大量提问针对的是用户手头的**即时临时文档**（如当季财务报表、刚下载的接口规约、待审阅合同草案等）。
要求用户“先创建知识库 → 上传文档 → 等待向量切片入库 → 再发起聊天”存在严重的操作心智阻碍。

**P0.3 核心目标**：
1. **即传即问（Zero-Knowledge-Base Upload）**：用户在 `ChatPage` 工作台无需选择或创建知识库，直接拖入或点击上传 PDF、DOCX、TXT、MD 文件，即可立即基于附件提问；
2. **轻量与深度双模式兼容**：临时附件不仅支持「⚡ 快速问答」，也能被「🧠 深度分析（LangGraph Agent）」作为即时证据读取和推导；
3. **真实可信与页码溯源**：对附件的回答必须同样标注精确来源（如 `[Ref-1] 来源: 2026Q3财报.pdf | 第 4 页`），支持前端右侧 `CitationDrawer` 查看原文；
4. **严格生命周期隔离**：临时附件与当前 `Conversation` 深度绑定，绝不污染全局持久化知识库，会话删除时关联附件全量物理清理。

---

## 二、系统架构与数据流图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ChatPage 统一工作台                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  [附件托盘]  📎 2026Q3财报.pdf (1.2MB, 16页) [✕]   📎 架构说明.docx (45KB) [✕]   │
│  [提问输入]  “请对比财报第4页的净利润数据与去年同期的变化趋势”                │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ POST /conversations/{id}/attachments
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        附件上传与零依赖解析服务                             │
│                                                                             │
│  1. 存储落盘: data/uploads/attachments/{conv_id}/{att_id}_{filename}        │
│  2. 扩展 ParserFactory:                                                     │
│     - PDF  ──► PyMuPDF (fitz) 按页提取文本                                  │
│     - DOCX ──► 内置 zipfile + xml.etree.ElementTree 轻量解析段落与表格文本   │
│     - TXT/MD ──► 纯文本多编码探测解析                                       │
│  3. Token 统计: TokenEstimator 预计算总字符量与 Token 消耗                  │
│  4. 数据库登记: conversation_attachments 表                                │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ 发起聊天提问 (带 conversation_id)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    自适应上下文预算调度与双模执行引擎                       │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ 动态分流策略 (Adaptive Chunking & Token Budgeting)                    │  │
│  │  - 小附件 (< 4000 tokens): 完整注入 Prompt 作为【临时参考文档】       │  │
│  │  - 大附件 (> 4000 tokens): 内存/轻量 SQLite 临时切片 (500 tokens)     │  │
│  │    通过 BM25 / 本地向量按提问召回 Top-K 证据块注入预算池              │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
│                                      │                                      │
│           ┌──────────────────────────┴──────────────────────────┐           │
│           ▼                                                     ▼           │
│   ⚡ 快速问答 (Workflow RAG)                         🧠 深度分析 (Agentic)   │
│   - PromptBuilder 装配附件证据                       - 注入 AgentState      │
│   - CitationExtractor 验证 [Ref-X]                   - EvidenceRegistry 纳管 │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 三、数据库模型与存储设计

### 3.1 数据库表设计：`conversation_attachments`
新增 `ConversationAttachment` 模型（[backend/app/models/conversation.py](file:///e:/develop/python%20agentic%20RAG/backend/app/models/conversation.py)）：

```python
class ConversationAttachment(Base):
    __tablename__ = "conversation_attachments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(32), nullable=False)  # pdf / docx / markdown / txt
    file_size = Column(Integer, nullable=False)     # 字节数
    file_path = Column(String(512), nullable=False) # 物理存储相对路径
    total_pages = Column(Integer, nullable=False, default=1)
    extracted_text = Column(Text, nullable=False)   # 提取的全文纯文本
    pages_json = Column(Text, nullable=False, default="[]") # 结构化分页文本 [{"page": 1, "text": "..."}]
    char_length = Column(Integer, nullable=False, default=0)
    token_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    conversation = relationship("Conversation", back_populates="attachments")
```

### 3.2 物理文件与元数据双层存储策略
- **分离原则**：
  - **物理文件**：统一存放于 `data/uploads/attachments/{conversation_id}/{attachment_id}_{filename}`，避免大二进制文件存入 SQLite/PostgreSQL 导致数据库膨胀；
  - **结构化元数据**：`pages_json` 存储带页码的文本切片，方便后续免重新解析即时装配上下文；
- **生命周期级联清理**：
  - 用户调用 `DELETE /api/v1/conversations/{id}/attachments/{att_id}`：删除 DB 记录并同步删除物理文件；
  - 用户删除会话 `DELETE /api/v1/conversations/{id}`：ORM 级联删除全部附件记录，并由事件钩子清空 `data/uploads/attachments/{id}/` 目录；
  - **强隔离约束**：临时附件绝不关联 `KnowledgeBase`，绝不写入 `Document` 或 `DocumentChunk` 持久化知识库表。

---

## 四、重点技术问题深入研究与技术决策

### 4.1 是否复用现有 ParserFactory？
- **决策结论**：**100% 复用并平滑扩展**。
- **技术理由**：
  - 现有 `backend/app/ingestion/parsers/` 下的 `BaseParser`、`ParsedDocument` 和 `ParsedPage` 已经具备完善的数据结构与页码追踪能力；
  - 现有 `PDFParser`（PyMuPDF）解析速度极快且完全稳定；`TextParser` 具备多编码自适应兼容；
  - 扩展后的 `ParserFactory` 同时为持久化知识库入库和临时对话附件解析提供统一底层支持，避免两套解析逻辑造成文本清洗口径不一致。

### 4.2 DOCX 解析器如何零依赖加入？
- **决策结论**：**基于 Python 标准库 `zipfile` + `xml.etree.ElementTree` 实现 `DocxParser`，零新增第三方依赖**。
- **技术依据**：
  - `.docx` 文件本质是一个 ZIP 压缩包，其文字内容全部结构化保存在 `word/document.xml` 中；
  - 核心逻辑仅需提取段落 `<w:p>` 与文本块 `<w:t>`，即可获得干净、按段落组织的纯文本；
  - 无需引入庞大的 `python-docx` 库（以及其依赖的 `lxml`），在保持系统极致轻量化和跨平台运行的同时，具备毫秒级解析性能。

### 4.3 临时附件采用数据库存储还是文件系统？
- **决策结论**：**“物理文件系统 + 结构化元数据与分页文本落库”的双层架构**。
- **对比分析**：
  - 若纯存数据库 BLOB：多附件上传会导致 SQLite/PostgreSQL 数据文件急剧膨胀，备份与迁移缓慢；
  - 若纯存文件系统：多轮对话每次都需重新读取文件、重新解析，IO 开销大且无法利用外键做级联一致性保证；
  - **双层架构收益**：解析一次后，结构化分页文本保存在数据库 `pages_json` 中，单次查询只需数十毫秒即可直接装配为 Prompt，兼顾高性能与存储整洁。

### 4.4 多附件内容分块与 Token Budget 预算自适应分配
- **决策结论**：**基于 Token 预算的双轨自适应调度机制（Dual-Track Adaptive Scheduling）**。
- **调度算法**：
  1. 计算当前所有已挂载附件的总 Token 消耗 $T_{\text{att}}$；
  2. **小附件轨道（$T_{\text{att}} \le 4000$ tokens）**：
     - 直接将附件文本按照 `[Ref-X] 来源: {filename} (第 P 页)` 格式完整编入 Prompt 的 `【会话临时参考文档】` 区域；
     - 零召回失真，模型具备全局视图；
  3. **大附件/多附件轨道（$T_{\text{att}} > 4000$ tokens）**：
     - 使用系统已有的 `RecursiveCharacterSplitter` 对附件分页文本进行快速切片（chunk_size=500, overlap=50）；
     - 采用 BM25 对当前用户 `query` 发起即时轻量检索，筛选出相关度最高的前 Top-K（如 6 个）分块；
     - 将这 6 个分块注入 `RETRIEVAL_TOKEN_BUDGET` 预算池中，并严格遵守 `ContextBudgetManager` 的 `MAX_QUERY_TOKENS` 与 `CONTEXT_SAFETY_MARGIN`，确保大附件永远不会把上下文撑爆。

### 4.5 如何将附件证据纳入 Citation Verification？
- **决策结论**：**无缝复用现有 `ScoredChunk` 统一契约与 `CitationExtractor`**。
- **实现机制**：
  - 每个附件切片在组装时抽象为标准 `ScoredChunk` 对象：
    - `chunk_id`: `att_chunk_{attachment_id}_{chunk_index}`
    - `document_id`: `attachment_id`
    - `document_name`: `attachment.file_name`（前端展示文件名）
    - `page_number`: `page.page_number`（真实原始页码）
    - `similarity_score`: 1.0（全量模式）或 BM25 归一化得分（大附件模式）
  - `PromptBuilder` 自动将这些分块格式化为 `[Ref-1] 来源文件: {doc_name} | 页码: 第 {page} 页`；
  - 模型在生成回答时严格遵循标注规则，输出 `[Ref-1]`；
  - `CitationExtractor` 无需修改即可自动提取，前端 `CitationDrawer` 点击即可滑出查看对应的附件原文和页码！

### 4.6 Workflow 与 Agent 如何共享同一套附件上下文？
- **决策结论**：**在请求解析层统一抽象为“会话临时证据集（Session Ephemeral Evidence）”**。
- **共享实现**：
  - **对于 Workflow**：将解析出来的附件 `ScoredChunk` 列表直接传递给 `WorkflowRAGEngine`，作为与知识库召回片段平级的参考依据；若用户未绑定知识库，则仅基于附件进行问答；
  - **对于 Agent**：
    - 在 `initial_messages` 或 System Prompt 中注入附件概览；
    - 在运行开始时，将附件分块直接注册进当前 Run 的 `EvidenceRegistry`；
    - Agent 既可以直接基于前序证据思考，也可以在决策时调用 `search_knowledge_base` 补充查询背景知识库，实现“手头附件 + 企业底座知识”的复合多跳分析。

### 4.7 SSE 如何报告附件解析、切片与上下文装配状态？
在现有 SSE 事件协议中新增轻量事件：
- `attachment_loaded` 事件：
  ```json
  {
    "event": "attachment_loaded",
    "data": {
      "attachments_count": 2,
      "total_pages": 18,
      "total_tokens": 3200,
      "mode": "full_injection"
    }
  }
  ```
- 前端 `AgentTimelineCard` 或工作台状态栏捕获此事件后，即时展示微动效：“已加载 2 篇会话附件（18页，3200 tokens）作为即时参考依据”。

---

## 五、API 接口规划 (API Specification)

```
POST   /api/v1/conversations/{id}/attachments            # 上传临时附件 (multipart/form-data)
GET    /api/v1/conversations/{id}/attachments            # 查询当前会话全部已挂载附件列表
DELETE /api/v1/conversations/{id}/attachments/{att_id}   # 移除特定附件
GET    /api/v1/conversations/{id}/attachments/{att_id}/preview # 预览附件提取的纯文本与分页
```

- **上传响应示例**：
  ```json
  {
    "id": "att-4fae9b12-9c32",
    "conversation_id": "conv-8821",
    "file_name": "2026Q3财报.pdf",
    "file_type": "pdf",
    "file_size": 1248500,
    "total_pages": 16,
    "char_length": 18450,
    "token_count": 3120,
    "created_at": "2026-11-02T17:30:00Z"
  }
  ```

---

## 六、前端工作台交互规划 (ChatPage UI Layout)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             ChatPage 聊天工作台                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  [消息流视窗]                                                               │
│   ... (用户与助手对话气泡，包含 [Ref-1] 附件引用)                           │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  [附件挂载托盘 (Attachment Tray)]                                           │
│   ┌──────────────────────────────┐  ┌──────────────────────────────┐        │
│   │ 📄 2026Q3财报.pdf (16页) [✕]  │  │ 📘 架构说明.docx (8页)  [✕]  │  + 上传  │
│   └──────────────────────────────┘  └──────────────────────────────┘        │
├─────────────────────────────────────────────────────────────────────────────┤
│  [输入控制区]                                                               │
│   [📎 点击上传附件]                                                         │
│   ┌─────────────────────────────────────────────────────────┐  ┌─────────┐  │
│   │ 请输入您关于附件或知识库的问题... (Enter发送)             │  │ 发送提问│  │
│   └─────────────────────────────────────────────────────────┘  └─────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

1. **输入框托盘**：选中的附件以胶囊徽章（Pill Badge）形式浮现在输入框上方，显示图标、文件名、页数和删除按钮；
2. **零知识库模式**：顶部知识库下拉框允许选择“无知识库 (纯附件问答)”，当未选知识库时系统依然可以顺畅发起问答；
3. **拖拽支持**：支持直接将文件拖拽至输入区完成即时解析与挂载。

---

## 七、开发边界与实施路线图 (Implementation Roadmap)

### 7.1 本轮边界
本轮严格执行**只读与架构设计**，未修改任何业务代码，未安装新包，未执行数据库迁移。

### 7.2 实施三步走计划
1. **Stage 1（解析基础设施与模型）**：
   - 实现 `DocxParser`（内置 `zipfile` + `xml`）；
   - 新增 `ConversationAttachment` 数据库模型与迁移；
   - 编写附件上传、解析、删除的后端 REST API 与单元测试。
2. **Stage 2（上下文调度与引擎接入）**：
   - 扩展 `PromptBuilder` 与 `ContextBudgetManager` 支持附件上下文；
   - 打通 Workflow RAG 与 LangGraph Agent 的附件读取链路；
   - 保证 `CitationExtractor` 对附件与知识库证据的混合引用核验。
3. **Stage 3（前端工作台与联调验收）**：
   - 在 `ChatPage` 中开发附件上传胶囊栏与文件操作；
   - 支持多轮会话附件元数据回显；
   - 全链路集成测试与生产构建验收。
