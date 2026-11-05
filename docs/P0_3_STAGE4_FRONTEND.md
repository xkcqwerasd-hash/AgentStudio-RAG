# AgentStudio-RAG — P0.3 Stage 4：聊天附件 UI 与双模式端到端联调交付报告

**阶段状态**：已全面完成并闭环验收（100% Pass）。  
**适用范围**：AgentStudio-RAG 企业级智能知识助手统一问答工作台。

---

## 一、新增与修改文件清单

| 层次 | 文件路径 | 状态 | 说明 |
| :--- | :--- | :---: | :--- |
| **API** | [`frontend/src/api/attachment.ts`](file:///e:/develop/python%20agentic%20RAG/frontend/src/api/attachment.ts) | **新增** | 附件上传、列表、解析详情与删除的 HTTP 客户端封装 |
| **API** | [`frontend/src/api/chat.ts`](file:///e:/develop/python%20agentic%20RAG/frontend/src/api/chat.ts) | **更新** | 允许 `knowledge_base_id` 可选，支持纯附件无知识库问答 |
| **Types** | [`frontend/src/types.ts`](file:///e:/develop/python%20agentic%20RAG/frontend/src/types.ts) | **更新** | 新增 `ConversationAttachment`、`ConversationAttachmentDetail`，扩展 `Citation` 与 `AgentToolCallItem` 溯源与统计字段 |
| **Component** | [`frontend/src/components/AttachmentPreviewDrawer.tsx`](file:///e:/develop/python%20agentic%20RAG/frontend/src/components/AttachmentPreviewDrawer.tsx) | **新增** | 附件解析文本抽屉，支持分块结构预览与全文复制 |
| **Component** | [`frontend/src/components/CitationDrawer.tsx`](file:///e:/develop/python%20agentic%20RAG/frontend/src/components/CitationDrawer.tsx) | **更新** | 来源溯源根据 `source_type` 精准渲染（DOCX 显示章节/段落、TXT 显示行号、PDF 显示页码、MD 显示章节） |
| **Component** | [`frontend/src/components/AgentTimelineCard.tsx`](file:///e:/develop/python%20agentic%20RAG/frontend/src/components/AgentTimelineCard.tsx) | **更新** | 扩展临时附件工具调用呈现：检索模式、目标文件、引用分配 `[Ref-X]`、Token 消耗统计 |
| **Page** | [`frontend/src/pages/ChatPage.tsx`](file:///e:/develop/python%20agentic%20RAG/frontend/src/pages/ChatPage.tsx) | **更新** | 附件按钮、拖拽上传、附件卡片条、页面刷新恢复、双模式纯附件与混合问答 |
| **Test** | [`backend/tests/test_p0_3_stage4_e2e_integration.py`](file:///e:/develop/python%20agentic%20RAG/backend/tests/test_p0_3_stage4_e2e_integration.py) | **新增** | 涵盖场景 A ~ E 的端到端 API 与安全性自动化测试套件 |

---

## 二、前端附件组件与交互设计

### 1. 输入框集成与拖拽上传
- **位置**: 聊天输入框顶部嵌入附件快捷工具栏。
- **添加附件按钮**: 包含纸夹图标，点击触发原生文件选择器，支持 `multiple` 同时选择多个文件。
- **文件格式白名单**: `.pdf`、`.docx`、`.txt`、`.md`。
- **拖拽体验 (`onDragOver`, `onDrop`)**: 拖拽文件至输入框区域时，呈现半透明高亮遮罩提示（"松开鼠标即可添加聊天附件"）。
- **客户端防错校验**:
  - 单文件体积不得超过 20MB。
  - 单会话附件总数不得超过 10 个。
  - 非白名单扩展名即刻给出精准拒绝提示，不向后端发送无效网络请求。

### 2. 附件卡片显示与状态指示
- 在输入框上方以横向滚动卡片条形式呈现当前会话的附件列表。
- 每个附件卡片包含：
  - **格式图标**: PDF (红)、DOCX (蓝)、Markdown (紫)、TXT (绿)。
  - **文件名**: 自动截断并带有 Tooltip 悬浮完整文件名。
  - **文件大小**: 自动格式化为 B / KB / MB。
  - **状态指示**:
    - `COMPLETED`: 绿色角标 `✓ 已就绪`。
    - `PARSING` / `PENDING`: 蓝色动画 `解析中...`。
    - `FAILED`: 红色警示 `✗ 解析失败`，悬浮呈现后端返回的真实 `error_message`。
  - **操作按钮**:
    - 👁️ 查看解析详情：打开 `AttachmentPreviewDrawer`。
    - ✕ 删除附件：带有二次确认 Popconfirm，删除后物理清理服务器文件。

### 3. 附件解析文本抽屉 `AttachmentPreviewDrawer`
- 遵循企业级知识助手轻量预览标准（不引入庞大的富文本编辑器或在线 PDF 渲染引擎）。
- **结构化分块 Tab**:
  - PDF: 呈现 `第 X 页` 及该页提取的文本片段。
  - DOCX: 呈现 `章节: A > B | 段落 #N` 及段落内容。
  - Markdown: 呈现 `章节: Header` 及段落内容。
  - TXT: 呈现 `行号: 第 M-N 行` 及代码/配置行文本。
- **提取全文 Tab**:
  - 显示完整字符数、预估 Token 消耗，提供一键复制全文功能。

---

## 三、Conversation 生命周期与持久化

### 1. 空白会话直接上传附件（零伪造 ID）
- 当用户处于新建的空白聊天中（尚未发送文本、尚未分配 `currentConvId`）直接上传附件时：
  1. 前端先行请求 `POST /api/v1/conversations` 创建实际会话实体，默认标题为 `关于 xxx 的问答`。
  2. 拿到后端生成的真实 UUID `conversation_id`。
  3. 即刻挂载该 `conversation_id`，并使用真实 ID 调用 `POST /api/v1/conversations/{id}/attachments` 上传文件。
  4. 自动将新会话同步插入左侧会话历史列表最前列。
  5. 后续用户输入问题时，无缝复用该会话，绝不使用客户端伪造临时 ID。

### 2. 会话切换与防状态污染
- 用户在左侧历史列表中切换会话时：
  1. 若当前存在正在执行的流式生成，安全触发 `abortController.abort()` 中止流。
  2. 并行请求 `getConversationMessages(id)` 与 `getAttachments(id)`。
  3. 使用当前会话对应的附件列表与消息列表原子更新状态，避免旧请求网络响应迟滞导致跨会话数据污染。
  4. 自动根据会话元数据恢复对应的执行模式（快速问答 / 深度分析）与目标知识库挂载状态。

### 3. 页面刷新恢复 (Refresh Recovery)
- 页面加载时自动执行 `loadConversations(true)`。
- 若存在历史会话，自动定位并激活首个历史会话，同步拉取其已解析附件列表与历史消息记录，保证刷新后附件与聊天上下文完整恢复。

---

## 四、双模式端到端集成 (Dual Execution Modes)

### 1. ⚡ 快速问答 (Workflow RAG)
- **底层驱动**: `WorkflowRAGEngine`。
- **纯附件提问**:
  - 用户可在顶部知识库选择器中选择 `不绑定知识库 (纯附件问答)` 或清空选择。
  - 提问时传入 `knowledge_base_id: undefined`。
  - 后端自动仅针对当前会话有效附件运行 BM25 检索或全文直读，按 Token 预算注入上下文并流式生成回答。
- **混合问答**:
  - 同时选择知识库并上传补充附件时，双轨预算分配器并行组织知识库候选块与附件证据，统一参与回答生成。

### 2. 🧠 深度分析 (LangGraph Agent Runtime)
- **底层驱动**: `LangGraphAgentRuntime` + ReAct 状态图。
- **自主决策**:
  - 当会话包含附件时，后端动态授权 `search_conversation_attachments` 工具并在 SystemMessage 中注入附件清单。
  - Agent 在推理循环中自主判断调用知识库检索还是附件检索。
  - 支持同一个 Agent Run 中多步连续调用不同工具，并在 `AgentTimelineCard` 中逐一展现各工具参数、耗时与命中的证据编号。

---

## 五、CitationDrawer 统一来源溯源

统一了永久知识库分块与临时附件分块的出处展示，杜绝模型幻觉：

1. **PDF 来源**: 显示真实原始页码（如 `页码: 第 2 页`），标签标记为 `PDF 来源`。
2. **DOCX 来源**: 显示真实的章节层级与段落编号（如 `章节: 架构规范 > 接口定义 | 段落 #4`），标签标记为 `DOCX 附件`，**严格杜绝将逻辑块序号误标为真实页码**。
3. **Markdown 来源**: 显示标题层级（如 `章节: 部署流程 > 金丝雀发布`），标签标记为 `Markdown 附件`。
4. **TXT 来源**: 显示行号范围（如 `行号: 第 1-15 行`），标签标记为 `TXT 附件`。
5. **引用一致性**:
   - 气泡下标与抽屉卡片均使用后端验证通过的 `[Ref-X]` 全局序号。
   - 历史消息中重新点击 Ref 标签，精准定位并高亮对应证据卡片。

---

## 六、测试验证矩阵与工程验收

### 1. 前端 TypeScript 编译与构建检查
```bash
cmd /c "cd frontend && npm run build"
> tsc && vite build
✓ 3104 modules transformed.
✓ built in 11.10s (dist/index.html, dist/assets/index-CnbqWa37.js)
```
- **编译状态**: **0 Errors, 0 Warnings**。

### 2. Stage 4 端到端集成测试套件 (`test_p0_3_stage4_e2e_integration.py`)
```bash
pytest backend/tests/test_p0_3_stage4_e2e_integration.py -v
============================== 5 passed in 7.60s ==============================
```
- **测试用例 1 (Scenario A)**: 纯 PDF 问答双模式（Workflow 与 Agentic 无 KB 运行）-> **PASS**
- **测试用例 2 (Scenario B)**: PDF + DOCX 跨格式对比与精准出处校验（PDF页码 vs DOCX章节/段落）-> **PASS**
- **测试用例 3 (Scenario C)**: 附件 + 企业知识库混合检索与统一全局 Ref 递增 -> **PASS**
- **测试用例 4 (Scenario D)**: 完整附件生命周期（上传、列表、解析详情、物理目录清理）-> **PASS**
- **测试用例 5 (Scenario E)**: 边界与安全拦截（空文件、恶意格式、超 20MB、超 10 个附件、跨会话 404 越权）-> **PASS**

### 3. 全量后端回归测试
```bash
pytest backend/tests -q
============================== 280 passed, 1 warning in 38.06s ==============================
```
- **总用例数**: **280 个全量测试全部 PASS (100% 通过率)**。
- **零回归**: 包含 M1-M3 基础检索体系、M4.1 Agent 运行时、P0.1 问答工作台、P0.2 模型中心、P0.3 阶段全部特性。

### 4. 真实 DeepSeek 原生 Function Calling 在线联调
```bash
python backend/scripts/verify_p0_3_agent_attachment_deepseek.py
================================================================================
  [SUCCESS] DeepSeek Function Calling 实测全部通过！多工具注册与自主路由完整验证！
================================================================================
```
- 场景 A（附件问题）：DeepSeek 自主调用 `search_conversation_attachments` (耗时 3034ms)。
- 场景 B（证据回传）：DeepSeek 结合 Observation 准确生成事实并标注 `[Ref-1]` (耗时 4698ms)。
- 场景 C（知识库问题）：DeepSeek 自主调用 `search_knowledge_base` (耗时 5257ms)。

---

## 七、产品定位与设计边界确认

本阶段严格遵循产品定位要求：
1. **开箱即用企业级助手**：用户只需在问答框直接添加附件并提问，无需编排任何 Agent 或注册工具。
2. **零 Dify 式工作流设计器**：未引入任何画布拖拽或流程编辑节点。
3. **双模式无缝协同**：纯知识库、纯附件、附件+知识库在统一界面下自适应切换。
4. **轻量预览架构**：通过结构化解析文本实现秒级预览，未引入冗余依赖。

---

## 八、P0.3 阶段整体总结

至此，**P0.3（聊天附件与临时知识检索基础设施）四个阶段已全部圆满交付**：
- **Stage 1**：附件存储、安全校验、物理清理与 REST API。
- **Stage 2**：Conversation-scoped 内存临时检索引擎与 Token 预算管理。
- **Stage 3-A**：Workflow RAG 双轨附件检索与 SSE 流式集成。
- **Stage 3-B**：LangGraph Agent Runtime 附件工具、统一 EvidenceRegistry 与多来源 Citation 溯源。
- **Stage 4**：前端输入框附件上传/拖拽、附件管理、解析文本预览抽屉、双模式前端交互、全链路端到端验收。

**P0.3 现已正式完整闭环。**
