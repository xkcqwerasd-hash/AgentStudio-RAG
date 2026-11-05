# AgentStudio-RAG — P0.2 模型管理中心交付报告

## 一、产品定位与设计概述

AgentStudio-RAG 定位为**开箱即用的企业级智能知识助手**。用户无需修改源码、修改 `.env` 或了解底层 Agent 编排细节，即可在 UI 界面自由配置与切换模型。

P0.2 阶段完成了完整的模型配置中心、运行时请求级模型解析切换、本地推理引擎探测与安全加密体系，让系统具备企业级多模型管理能力。

---

## 二、数据库模型设计 (Data Models)

采用服务商（`ModelProvider`）与模型规格（`ModelConfig`）双层实体架构，避免单服务商单模型的僵化设计。

```
┌─────────────────────────────────┐
│          ModelProvider          │
├─────────────────────────────────┤
│ id: UUID (PK)                   │
│ name: VARCHAR(128)              │
│ provider_type: VARCHAR(32)      │
│ base_url: VARCHAR(512)          │
│ api_key_ciphertext: TEXT        │◄─── Fernet AES-128-CBC + HMAC-SHA256 加密存储
│ enabled: BOOLEAN                │
│ created_at / updated_at         │
└────────────────┬────────────────┘
                 │ 1:N (Cascade Delete)
                 ▼
┌─────────────────────────────────┐
│           ModelConfig           │
├─────────────────────────────────┤
│ id: UUID (PK)                   │
│ provider_id: UUID (FK)          │
│ model_name: VARCHAR(128)        │◄─── 真实请求标识 (如 deepseek-chat, llama3:8b)
│ display_name: VARCHAR(128)      │◄─── 前端友好展示名 (如 DeepSeek-V3 官方)
│ model_type: VARCHAR(32)         │
│ capabilities_json: TEXT         │◄─── {"supports_streaming": true, "supports_tool_calling": true, ...}
│ is_default: BOOLEAN             │◄─── 全局唯一默认模型标识
│ enabled: BOOLEAN                │
│ created_at / updated_at         │
└─────────────────────────────────┘
```

- **文件位置**：[backend/app/models/model_provider.py](file:///e:/develop/python%20agentic%20RAG/backend/app/models/model_provider.py)
- **级联删除**：删除 Provider 时自动级联删除其下属全部 ModelConfig；
- **默认自愈与兼容**：数据库初次启动且无配置时，自动从系统现有环境变量初始化默认 DeepSeek Provider 与 ModelConfig，保障原有环境零断层平滑过渡。

---

## 三、安全加密架构 (Security Architecture)

依据需求规范，系统对模型密钥执行了最高等级的安全约束：

1. **落盘即加密（Encryption at Rest）**：
   - 采用标准 `Fernet` 对称加密算法（AES-128-CBC + HMAC-SHA256 签名，防篡改）；
   - 主密钥加载逻辑：
     - 优先从环境变量 `MODEL_ENCRYPTION_KEY` 读取；
     - 若未配置，自动在持久化存储目录受控生成持久化密钥文件 `data/storage/.model_master_key`（POSIX 下限制为 0600 权限）；
     - **严禁降级**：绝不在数据库中存储任何明文 API Key。
2. **内存脱敏与响应安全**：
   - 普通查询 API（`GET /models/providers`）对 API Key 严格脱敏（如 `sk-96****4142`），且通过布尔字段 `has_api_key: true` 告知前端密钥状态；
   - 严禁在日志中打印完整 API Key；
   - 前端更新服务商时支持保密回传：留空或省略 `api_key` 则自动继承数据库既有加密密文，无需用户每次重新输入。
- **文件位置**：[backend/app/core/security.py](file:///e:/develop/python%20agentic%20RAG/backend/app/core/security.py)

---

## 四、请求级模型解析与运行时切换机制 (Runtime Model Resolution)

系统彻底打破了过去基于全局 `settings` 单例 LLM 对象的僵化设计，构建了**请求级模型动态解析工厂**（Request-Level Model Factory）：

```
用户请求 (ChatRequest / AgentRunRequest)
   │
   ├─► 携带可选 model_id
   │      │
   │      ├─ 存在 model_id ──► 校验 ModelConfig 是否有效且启用
   │      │
   │      └─ 未传 model_id ──► 查询全局默认模型 (is_default == True)
   │
   ▼
[resolve_llm_client]
   │
   ├─► 读取关联 Provider 记录并解密 API Key
   ├─► 提取模型能力规格 (capabilities)
   ├─► 创建请求隔离的 OpenAILLMClient(api_key, base_url, model)
   │
   ▼
[能力边界安全校验 validate_tool_calling_capability]
   │
   ├─ 快速问答模式 (Workflow RAG) ──► 注入 WorkflowRAGEngine(db, llm_client, llm_model)
   │
   └─ 深度分析模式 (LangGraph Agent) ──► 检查 supports_tool_calling
         ├─ True  ──► 注入 LangGraphAgentRuntime(tool_registry, llm_client)
         └─ False ──► 立即拦截并返回 400 明确建议，杜绝静默失败
```

- **隔离性保障**：不同请求实例化专属客户端对象，多用户或多并发会话使用不同模型、不同 Base URL 与密钥时互不污染；
- **双模完全支持**：Workflow 快速问答与 LangGraph 深度分析共用同一套模型配置与解析逻辑；
- **调用模型溯源**：在响应结果 `execution_info.llm_model` 中真实记录本次实际执行的模型名称。
- **文件位置**：[backend/app/llm/factory.py](file:///e:/develop/python%20agentic%20RAG/backend/app/llm/factory.py)

---

## 五、模型连接测试与本地服务探测

为提升模型运维效率与体验，在 [backend/app/llm/manager.py](file:///e:/develop/python%20agentic%20RAG/backend/app/llm/manager.py) 中内置了探测与测试引擎：

1. **OpenAI 兼容真实连通性测试（`POST /api/v1/models/test`）**：
   - 携带 5-token 超微载荷向目标模型发起极简聊天请求；
   - 精准测量真实往返时延（`latency_ms`）；
   - 对 401（未授权/密钥错误）、404（模型不存在/URL路径不当）、429（超频限流）、连接拒绝及超时进行人性化翻译。
2. **Ollama 本地服务一键探测（`POST /api/v1/models/probe/ollama`）**：
   - 探测本地 `/api/tags`，提取已下载模型的名称、参数量（如 8.0B）、模型架构（如 llama/qwen2）、量化等级（如 Q4_0）与文件体积；
   - 前端支持一键批量将探测结果转为可用模型配置。
3. **vLLM 本地推理服务探测（`POST /api/v1/models/probe/vllm`）**：
   - 支持通过标准 `/v1/models` 端点自动获取托管的所有模型 ID 列表。

---

## 六、REST API 端点清单

| 路由路径 | 请求方法 | 描述说明 |
| :--- | :--- | :--- |
| `/api/v1/models/providers` | `GET` | 列出全部服务商及各下属模型（API Key 已脱敏） |
| `/api/v1/models/providers` | `POST` | 创建服务商，支持同时附带初始模型及密钥加密 |
| `/api/v1/models/providers/{id}` | `GET` / `PUT` / `DELETE` | 查看、修改、删除指定服务商（级联删除下属模型） |
| `/api/v1/models/providers/{id}/models` | `POST` | 在指定服务商下新增模型配置 |
| `/api/v1/models/configs/{id}` | `PUT` / `DELETE` | 修改模型属性/能力，或删除模型 |
| `/api/v1/models/configs/{id}/set-default` | `POST` | 将指定模型设为全局默认模型（原子取消其他默认） |
| `/api/v1/models/active` | `GET` | 供问答工作台快速获取可用模型精简列表 |
| `/api/v1/models/test` | `POST` | 测试已保存或表单中模型的真实连通性与时延 |
| `/api/v1/models/probe/ollama` | `POST` | 探测本地 Ollama 实例中已就绪的模型 |
| `/api/v1/models/probe/vllm` | `POST` | 探测本地 vLLM 实例中托管的模型 |

- **文件位置**：[backend/app/api/v1/models.py](file:///e:/develop/python%20agentic RAG/backend/app/api/v1/models.py)

---

## 七、前端交互实现

1. **模型管理中心独立页面**（[frontend/src/pages/ModelsPage.tsx](file:///e:/develop/python%20agentic%20RAG/frontend/src/pages/ModelsPage.tsx)）：
   - 服务商卡片式管理，展示名称、服务类型、Base URL、密钥状态、下属模型表格；
   - 服务商预设模板：DeepSeek 官方、Ollama 本地、vLLM 本地、硅基流动、通用 OpenAI 兼容；
   - 逐模型「测试连接」按钮，支持 Spin 状态与即时耗时结果 Tag 提示；
   - 一键设为默认模型，自动联动标记 `★ 全局默认`；
   - 「探测 Ollama 本地模型」弹窗，自动检测 `http://localhost:11434` 并支持一键批量导入。
2. **工作台即时切换模型**（[frontend/src/pages/ChatPage.tsx](file:///e:/develop/python%20agentic%20RAG/frontend/src/pages/ChatPage.tsx)）：
   - 在聊天工作台 Header 顶部（知识库与模式切换器旁）集成模型选择器；
   - 选项显示模型名称、默认标签与 Tool Calling 能力标识；
   - 选择不支持 Tool Calling 的模型并在深度分析模式提问时，前端立即给予友善阻止提示，指引用户切换模型或切换到快速问答模式。
3. **全局导航集成**（[frontend/src/App.tsx](file:///e:/develop/python%20agentic%20RAG/frontend/src/App.tsx)）：
   - 顶部导航栏增加「模型管理中心」选项卡，与知识库管理、检索质量调试台保持一致设计规范。

---

## 八、自动化测试与验收结果

### 8.1 P0.2 专属自动化测试套件
新增 [backend/tests/test_p0_2_models.py](file:///e:/develop/python%20agentic%20RAG/backend/tests/test_p0_2_models.py)，全面覆盖 13 项关键验收指标：
1. `test_provider_and_model_crud`: 服务商及下属模型的增删改查；
2. `test_api_key_encryption_and_masking`: 密文落盘验证、API Key 接口响应脱敏验证；
3. `test_connection_test_endpoint`: 端点连接测试与耗时统计；
4. `test_direct_connectivity_logic`: 底层真实连通性测试逻辑；
5. `test_ollama_and_vllm_probing`: Ollama `/api/tags` 与 vLLM `/v1/models` 接口探测；
6. `test_workflow_and_agent_dynamic_model_switching`: Workflow 与 Agentic 运行时动态模型切换与追踪溯源；
7. `test_concurrent_requests_model_isolation`: 双请求并发解析独立客户端与隔离密钥；
8. `test_default_model_switching`: 默认模型切换原子排他性；
9. `test_invalid_api_key_and_network_error`: 401 密钥失效及网络超时降级友好提示；
10. `test_unsupported_tool_calling_safety_check`: 深度分析拦截无 Tool Calling 模型的安全边界；
11. `test_legacy_deepseek_regression`: 未指定 model_id 时的系统默认后向兼容。

### 8.2 全量测试与生产构建验证
- **后端回归测试**：
  ```bash
  backend\.venv\Scripts\python.exe -m pytest backend/tests -q
  # 输出: 187 passed, 1 warning in 26.55s
  ```
  187 项单元与集成测试 **100% 全部通过**（176 项原有测试 + 11 项 P0.2 新增测试）。
- **前端生产构建**：
  ```bash
  cd frontend && npm run build
  # 输出: ✓ 3102 modules transformed, built in 10.32s, 0 errors
  ```
  TypeScript 类型检查与 Vite 打包 **0 错误顺利通过**。

---

## 九、已知局限与 P0.3 交接边界

### 9.1 本阶段边界确认
- **模型类型范围**：本阶段专注大语言对话/推理模型（Chat LLM），已配置的 BGE Embedding 与 Cross-Encoder Reranker 继续保持既有优秀检索架构，未被替换或修改；
- **非生成多模态**：模型能力字段中预留了 `supports_vision` 扩展槽，但本阶段未开发图像输入；
- **附件解析**：聊天输入框左下角的附件上传入口保持 UI 占位状态，将在 P0.3 正式支持。

### 9.2 严格遵守停止要求
本阶段工作已全部圆满交付完成，**已严格止步于 P0.2 边界**，未开发多模态、未引入 MCP/Redis，等待正式验收。
