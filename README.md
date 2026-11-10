# AgentStudio-RAG — 基于 Python 的企业级 Agentic RAG 智能知识问答系统

[![Version](https://img.shields.io/badge/Release_Candidate-v1.3.0--rc.3-blue.svg)](docs/PROJECT_FINAL_TECHNICAL_SUMMARY.md)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React 18](https://img.shields.io/badge/React-18.3+-61DAFB.svg?logo=react&logoColor=black)](https://react.dev/)
[![Docker Ready](https://img.shields.io/badge/Docker-Local_%7C_Server-2496ED.svg?logo=docker&logoColor=white)](docs/DEPLOYMENT.md)
[![License: All Rights Reserved](https://img.shields.io/badge/License-All_Rights_Reserved-red.svg)](#十二版权声明与开源说明-copyright--license-notice)

> **A full-stack Agentic RAG platform built with FastAPI, LangGraph, React and Docker, featuring hybrid retrieval, long-term memory, multi-user authentication and local/server deployment.**  
> **版本标识**：`v1.3.0-rc.3` (`RC-Phoenix-2026`)

AgentStudio-RAG 是参考 Java 生态企业级开源项目 [Ragent](https://github.com/nageoffer/ragent) 进行重新架构设计与深度重构的 **Python 原生企业级 Agentic RAG 智能知识问答系统**。

本项目摒弃简单的“玩具级 API 调用”和“黑盒积木编排”，聚焦于**真实企业级交付与稳定性工程**：覆盖三态执行路径、混合多路召回与重排序、跨会话长期记忆、单会话临时附件即时解析、多租户 RBAC 权限隔离、全容器化双模式部署（Local 单机免登录 / Server 多租户服务模式）。

---

## 📸 实际运行界面 (System Showcase)

| 智能问答工作台 (流式 Markdown / 真实页码引用溯源) |
| :---: |
| ![智能对话工作台](docs/screenshots/p1_3_stage5_local_workbench_1920x1080.png) |

| 知识库文档管理与切块索引状态流水线 | 多模型管理与网络连通性实时探测 |
| :---: | :---: |
| ![知识库文档管理](docs/screenshots/p1_3_stage5_local_kb_1920x1080.png) | ![模型配置管理](docs/screenshots/p1_3_stage5_local_models_1920x1080.png) |

| Server 模式多租户安全登录 | Server 模式跨租户水平越权严格隔离 |
| :---: | :---: |
| ![多用户安全登录](docs/screenshots/p1_3_stage5_server_login_1920x1080.png) | ![跨租户隔离](docs/screenshots/p1_3_stage5_user_b_isolation_1920x1080.png) |

---

## 一、系统架构全景 (System Architecture)

```mermaid
flowchart TD
    subgraph ClientLayer["前端接入层 (Frontend & Client)"]
        UI["React 18.3 + TypeScript + Ant Design"]
        Workbench["智能对话工作台 (流式 SSE / 真实引用溯源)"]
        KBPanel["知识库与文档管理"]
        AgentPanel["LangGraph Agent 控制台"]
        ModelPanel["多模型与凭据配置中心"]
        MemoryPanel["长期记忆管理看板"]
    end

    subgraph GatewayLayer["接入与安全网关 (Gateway & Security)"]
        Nginx["Nginx 1.27 反向代理 (Gzip / SSE 无缓冲流式透传)"]
        AuthMiddleware["双模式认证中间件 (Local 免登录 / Server JWT RBAC)"]
        IDORGuard["多租户数据隔离与 IDOR 防御守卫"]
    end

    subgraph CoreEngine["FastAPI 业务核心与执行引擎 (Core Services)"]
        Router["智能意图路由 (Direct / RAG / Agentic 显式绑定与分流)"]
        DirectChat["Direct LLM 执行引擎 (0 检索开销直答)"]
        WorkflowRAG["Workflow RAG 确定性引擎 (防幻觉 / 结构化引用定位)"]
        LangGraphAgent["加固 LangGraph ReAct Agent (有界自修复 / 参数防震荡)"]
        AttachService["单会话附件引擎 (PyMuPDF 内存解析 / 跨页检索)"]
        MemoryService["跨会话长期记忆系统 (异步提炼 / 预算控制 / 动态注入)"]
        BudgetManager["ContextBudgetManager (统一 Token 预算规划与下传)"]
    end

    subgraph RetrievalLayer["高精混合检索系统 (Hybrid Retrieval)"]
        DenseRetriever["Dense Vector 向量召回 (BAAI/bge-small-zh-v1.5)"]
        SparseRetriever["BM25 关键词精确检索 (TechnicalBM25Tokenizer)"]
        RRFFusion["Reciprocal Rank Fusion (RRF k=60 倒数排名融合)"]
        Reranker["Cross-Encoder 重排序 (ONNX Xenova/bge-reranker-base)"]
    end

    subgraph StorageLayer["持久化与存储层 (Data & Persistence)"]
        SQLiteDB["SQLite 3 (Local 模式默认，开箱即用)"]
        PGDB["PostgreSQL 16 (Server 模式默认，多租户隔离)"]
        DocStorage["文件与附件持久化卷 (agentstudio_data)"]
        ModelCache["本地模型权重缓存卷 (agentstudio_models)"]
    end

    ClientLayer -->|HTTP REST / SSE 流式| GatewayLayer
    GatewayLayer --> CoreEngine
    CoreEngine --> RetrievalLayer
    CoreEngine --> StorageLayer
    RetrievalLayer --> StorageLayer
```

---

## 二、三态执行引擎深度对比 (Execution Engines Matrix)

针对不同类型的用户提问，系统提供三条差异化的执行路径，兼顾**极速响应**与**复杂推理**：

| 维度指标 | 1. Direct LLM 极速直答 | 2. Workflow RAG 确定性检索 | 3. LangGraph Agent 循环推理 |
| :--- | :--- | :--- | :--- |
| **适用场景** | 日常问候、通用常识、纯代码编写、翻译等无需企业私有数据的通用问题。 | 严格基于企业规章制度、技术规范、产品手册的问答，要求有据可查、可精确定位引用。 | 需要“多步自查”、“跨工具调用”、“检索后二次计算”的复杂综合任务。 |
| **检索与工具开销**| **0 检索开销**，完全跳过向量与关键词检索。 | **单次混合检索**（Dense + BM25 + RRF + ONNX Rerank）。 | **多轮动态调用**，由加固的 ReAct 循环自主决定调用频次与工具参数。 |
| **响应时延表现** | **极快**（0 检索与中间件开销，时延取决于上游大模型 API 响应速度与网络 Prefill 耗时）。 | **适中**（30 题基准实测：混合检索挂钟 P50 约 1.44s，客户端感知首字时延 Client TTFT P50 约 4.14s，端到端总时延 P50 约 5.78s）。 | **依推理轮数而定**（多步流式输出，每轮工具调用包含单次检索及参数校验自修复耗时）。 |
| **引用与溯源** | 无引用标注（纯大模型通用能力回答）。 | 严格附带 `[Ref-X]` 真实页码与原文片段展开抽屉。 | 步骤折叠面板中清晰展示每一步的 Thought / Action / Observation 轨迹。 |
| **幻觉控制机制** | 依赖大模型自身对齐水平。 | **强防幻觉 Prompt**：检索证据不足时主动拒答，严禁臆造。 | **自省自纠**：在状态图循环中核验中间工具返回结果。 |

---

## 三、混合多路检索与消融实验 (Hybrid Retrieval & Ablation)

单用稠密向量检索（Dense）容易在专业术语、数字代号上发生语义漂移；单用关键词检索（BM25）则无法理解近义词与语义意图。系统实现了严谨的企业级检索闭环：

```
用户提问 (Query)
    │
    ├───> [Dense 语义向量检索] ───> BAAI/bge-small-zh-v1.5 (512维向量，余弦相似度 Top-20)
    │
    └───> [BM25 关键词精确检索] ───> 倒排索引精确匹配特定专有名词/代号 (Top-20)
                │
                ▼
      [Reciprocal Rank Fusion (RRF)] ───> score = Σ 1 / (60 + rank_i) 倒数排名融合打分
                │
                ▼
      [Cross-Encoder 深度重排序] ───> Xenova/bge-reranker-base (ONNX Runtime 本地轻量推理)
                │
                ▼
      输出 Top-K 高相关高置信度切块 (附带准确物理页码与原文高亮)
```

### 检索架构消融实验客观对比 (E2.1 Benchmark)

依据 [`eval/reports/retrieval_ablation_report.md`](eval/reports/retrieval_ablation_report.md) 实际量化测试结果（20 条覆盖数值、精确专有名词、架构概念与同义长句的代表性样本，3 篇企业架构技术规范，共计 16 个真实文档分块）。下表展示全量 20 题宏平均（Macro Average）量化指标：

| 方案代号 | 检索方案名称 | Recall@1 | Recall@5 | MRR@10 | nDCG@10 | 检索 P50 延迟 | 检索 P95 延迟 | 核心特性与工程权衡 |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **A** | Dense Only (BGE-small-zh) | 82.50% | 100.00% | 0.9417 | 0.9547 | 11.64 ms | 13.86 ms | 语义泛化强，但专有名词和精确参数易漂移。 |
| **B** | BM25 Only (Lucene Positive IDF) | 92.50% | 100.00% | 1.0000 | 0.9964 | **0.37 ms** | **0.57 ms** | 字面精确匹配极快（<1ms），依赖词频统计，对开放语义泛化存在理论局限。 |
| **C** | Dense + BM25 + RRF ($k=60$) | 87.50% | 100.00% | 0.9750 | 0.9797 | 11.47 ms | 12.96 ms | 双路互补无参数融合，大幅提升召回稳定性。 |
| **D** | **Hybrid + Cross-Encoder Reranker** | **92.50%** | **100.00%** | **1.0000** | **0.9972** | 1333.78 ms | 1434.55 ms | **全面置顶核心答案，全量排序指标最优**（方案 D 检索阶段累计耗时（分阶段计时求和）P50 约 1.33s）。 |

> [!NOTE]
> 实验表明，在无 GPU 的本地 CPU（INT8 量化）环境下，方案 D 检索阶段累计耗时（分阶段计时求和，公式为 `rrf_dt + rerank_res.duration_ms`）P50 约 1333.78ms（P95 约 1434.55ms，包含双路并行召回、RRF 融合与 20 组候选分块 Cross-Encoder 重排全流程），换取了最高的深度语义对齐指标（全量 Recall@5 达到 100%，Macro nDCG@10 达到 0.9972）。上表统计口径为方案 D 检索阶段累计耗时（分阶段计时求和），既不是 Cross-Encoder 单独推理耗时，也不是单次连续端到端挂钟耗时；在高吞吐生产场景中，建议为其配备专用 GPU 节点或部署 TensorRT / ONNX-GPU 加速推理。

---

## 四、跨会话长期记忆机制 (Long-Term Memory)

系统具备跨会话连续认知能力，主要技术特点：
1. **异步提炼 (Async Extraction)**：会话结束后后台异步分析多轮对话，提炼出 `USER_PROFILE`（用户画像与技能背景）、`IMPORTANT_FACT`（用户明确告知的约束事实）、`TOPIC_PREFERENCE`（话题偏好）。
2. **上下文 Token 预算管理 (Budget Allocation)**：采用专用 Token 预算池（默认 500 Tokens），绝不挤占知识库文档检索或模型输出窗口。
3. **安全生命周期**：相同内容触发精确事实去重，仅递增使用频次与更新时间戳；用户可在控制台实时查看、置顶、编辑或物理注销记忆。

---

## 五、Local / Server 双部署模式架构

| 部署模式 | 适用场景 | 数据库 | 认证方式 | 特点与工程落地 |
| :--- | :--- | :--- | :--- | :--- |
| **Local 模式** | 个人开发者、单机私密研究、离线功能演示。 | SQLite 3 (`aiosqlite`) | 自动免登录 (`usr_local_default`) | **开箱即用，零配置**。无需安装数据库服务，不折腾账号，数据完全保存在本地磁盘与独立 Docker 卷。 |
| **Server 模式** | 团队协作、私有化原型与多租户测试部署。 | PostgreSQL 16 (`asyncpg`) | JWT Bearer Token + PBKDF2 密码加盐 | **多租户逻辑强隔离、容器化服务栈**。基于 SQLAlchemy 异步驱动严格防御水平越权（IDOR），集成 Nginx 容器提供生产反向代理与无缓冲流式传输。<br>*(注：当前为单机 Docker 容器化多租户栈，非分布式高可用集群)*。 |

---

## 六、Docker Compose 快速启动指南

### 1. 方式 A：Local 单人模式（开箱即用，免登录）

```bash
# 1. 克隆代码仓库
git clone https://github.com/xkcqwerasd-hash/AgentStudio-RAG.git
cd AgentStudio-RAG

# 2. 复制环境配置模板并填入您的大模型 API 密钥
cp .env.example .env
# 编辑 .env：填入 OPENAI_API_KEY / OPENAI_BASE_URL (支持 DeepSeek, OpenAI, Ollama)

# 3. 一键启动 Local 容器栈
docker compose -f docker-compose.local.yml up -d

# 4. 访问工作台: http://localhost:8080
```

### 2. 方式 B：Server 多租户服务模式（PostgreSQL 16 + JWT 认证 + Nginx 网关）

```bash
# 1. 复制环境配置模板
cp .env.example .env

# 2. 编辑 .env 配置服务端高强度安全密钥与数据库密码：
# DEPLOYMENT_MODE=server
# SERVER_AUTH_SECRET=your_custom_generated_64_char_hex_secret
# POSTGRES_PASSWORD=your_strong_postgres_password_123

# 3. 启动 Server 容器栈 (PostgreSQL 16 + FastAPI Backend + Nginx/React Frontend)
docker compose up -d

# 4. 初始化首位系统管理员账号 (CLI 工具)
docker compose exec backend python create_admin.py create -u admin -p "AdminPass123!"

# 5. 访问系统: http://localhost:8080 (进入登录/注册界面)
```

---

## 七、大模型配置说明 (Model Providers)

系统兼容各类 OpenAI 规范的 API 接口，并已内置对主流模型厂商的深度适配：

- **DeepSeek**（推荐）：`OPENAI_BASE_URL=https://api.deepseek.com`，模型名称 `deepseek-chat`。
- **OpenAI 原生**：`OPENAI_BASE_URL=https://api.openai.com/v1`，模型名称 `gpt-4o-mini` 或 `gpt-4o`。
- **本地私有化 Ollama**：`OPENAI_BASE_URL=http://host.docker.internal:11434/v1`，模型名称 `qwen2.5:7b` 或 `llama3.1:8b`。
- **安全保障**：所有通过前端或后台添加的模型 API Key，落库前均经过主加密密钥（Fernet 对称加密算法）加密存储，绝不在前台明文输出。

---

## 八、项目目录结构 (Repository Layout)

```
AgentStudio-RAG/
├── backend/                       # Python FastAPI 后端服务工程
│   ├── app/
│   │   ├── agent/                 # LangGraph ReAct 状态循环图与工具节点 (含防震荡/自修复)
│   │   ├── api/v1/                # RESTful 接口路由 (chat, kb, auth, models, memories)
│   │   ├── attachments/           # 单会话临时附件管理与 PyMuPDF 解析服务
│   │   ├── core/                  # 配置中心 (config.py)、身份认证、SSRF 防御、日志
│   │   ├── db/                    # SQLAlchemy 异步驱动与数据库自动 Schema 迁移
│   │   ├── llm/                   # 大模型适配器、本地 BGE Embedding 与连接池
│   │   ├── memory/                # 跨会话长期记忆提炼服务与 Token 预算控制器
│   │   ├── models/                # SQLAlchemy ORM 实体数据模型定义
│   │   ├── rag/                   # Workflow RAG 流水线、引用定位与防幻觉 Prompt 构建
│   │   ├── retrieval/             # 密集向量存储、BM25 稀疏检索、RRF 融合打分、ONNX Reranker
│   │   └── routing/               # 智能意图路由分类器与 Direct LLM 执行引擎
│   ├── tests/                     # 自动化单元测试与加固回归测试套件 (496 项测试)
│   ├── create_admin.py            # 管理员初始化命令行工具
│   ├── entrypoint.sh              # 生产容器入口脚本 (DDL 自动迁移 / Worker 分配)
│   └── requirements.txt           # 后端核心依赖清单
├── frontend/                      # React 18 + TypeScript + Vite 前端工程
│   ├── src/
│   │   ├── api/                   # 前后端交互 API 客户端与 SSE 流式事件监听
│   │   ├── components/            # UI 组件 (侧边栏、引用展开抽屉、记忆面板、附件管理)
│   │   ├── pages/                 # 主页面 (ChatPage, KnowledgeBasePage, ModelsPage, LoginPage)
│   │   └── types.ts               # 前端 TypeScript 接口类型规范
│   ├── nginx.conf                 # 前端 Nginx 生产反向代理配置文件
│   └── package.json               # 前端依赖配置
├── eval/                          # 评测基准套件与量化实验报告
│   ├── datasets/                  # 标准化评测数据集 (黄金事实基准)
│   └── reports/                   # 检索消融与端到端 RAG 生成质量评测报告
├── docker/
│   └── nginx/nginx.conf           # 生产级反向代理与网关配置 (无缓冲流式透传)
├── docs/                          # 官方技术文档库
│   ├── PROJECT_FINAL_TECHNICAL_SUMMARY.md # 项目技术收官与架构总结报告
│   ├── PROJECT_ISSUE_ARCHIVE.md   # 项目缺陷追踪与归档档案 (ISS-001 ~ ISS-009)
│   ├── ARCHITECTURE.md            # 系统技术架构白皮书
│   ├── DEPLOYMENT.md              # 生产部署与运维实战手册
│   └── screenshots/               # 系统实际运行高清截图
├── Dockerfile.backend             # 后端轻量生产镜像构建文件 (模型内置缓存, 非 root 权限)
├── Dockerfile.frontend            # 前端多阶段静态分发镜像文件
├── docker-compose.local.yml       # Local 模式编排配置 (SQLite, 零外部依赖)
├── docker-compose.yml             # Server 模式编排配置 (PostgreSQL 16, JWT, Nginx 网关)
└── README.md                      # 本文档
```

---

## 九、技术栈清单与各模块职责

| 技术层次 | 选型技术 / 工具 | 版本约束 | 核心职责与工程价值 |
| :--- | :--- | :--- | :--- |
| **运行时环境** | Python | 3.12+ | 现代化类型注解支持，异步并发事件循环性能优化。 |
| **Web 异步网关**| FastAPI + Pydantic v2 | 0.115+ / 2.8+ | 极速异步接口响应，严格的入参出参运行时校验与自动 OpenAPI 文档生成。 |
| **数据持久化 ORM**| SQLAlchemy + asyncpg / aiosqlite | 2.0.30+ | 全异步 ORM 抽象，一套模型代码自适应兼容 SQLite 与 PostgreSQL。 |
| **本地稠密向量计算**| FastEmbed + NumPy | 0.9+ / 1.26+ | 本地 CPU 极速执行 `BAAI/bge-small-zh-v1.5`，512 维向量提取，无需 GPU 外部依赖。 |
| **深度语义重排序**| ONNX Runtime | 1.20+ | 轻量化本地推理 `Xenova/bge-reranker-base`，极致降低镜像体积与跨环境部署依赖。 |
| **PDF 结构化解析**| PyMuPDF (fitz) | 1.24+ | C 语言底层 PDF 高保真解析，准确提取原始物理页码，支持中英文混排。 |
| **智能体状态图**| LangGraph + LangChain Core | 0.2.20+ / 0.3+ | 状态机循环推理图，原生支持 ReAct 循环推演、工具调度与状态持久化。 |
| **前端交互控制台**| React + TypeScript + Ant Design | 18.3+ / 5.6+ / 5.21+ | 企业级中文控制台规范，流式打字渲染与原文溯源展开抽屉。 |
| **构建分发与网关**| Vite + Nginx Alpine | 5.4+ / 1.27 | 秒级前端热开发，生产静态文件分发与无缓冲流式 SSE 透传。 |

---

## 十、测试验证与工程加固成果 (Verification & Hardened Engineering)

系统建立了**“离线确定性测试 $\rightarrow$ 独立沙箱评测 $\rightarrow$ 真实 API 在线单点验证”**的三层质量验证体系：

### 1. 三层质量验证防线

```
┌────────────────────────────────────────────────────────┐
│ Level 3: 真实 API 在线单点穿透验证 (Real API Verification)│
│ • 针对长文本截断 (GEN-11) 发起受控真实 DeepSeek 在线调用      │
│ • Payload max_tokens=4096 真实贯通，输出 843 字符无突断       │
│ • finish_reason=stop 正常完结 (相比历史 326 字符增长 158.6%)  │
└───────────────────────────▲────────────────────────────┘
                            │ (严格离线门禁准入)
┌───────────────────────────┴────────────────────────────┐
│ Level 2: 独立沙箱量化评测 (Isolated DB & TCP Benchmark) │
│ • 检索消融评测 (20题)：Hybrid+Reranker 达成 Recall@5 100% │
│ • 生成质量评测 (30题)：真实 TCP Socket 测得 TTFT P50 4.14s │
│ • 事实忠实度 3.92/5.0，程序化引用准确度 4.17/5.0         │
└───────────────────────────▲────────────────────────────┘
                            │ (自动化全绿灯门禁)
┌───────────────────────────┴────────────────────────────┐
│ Level 1: 离线自动化测试套件 (496 passed in 60.42s)     │
│ • 496 项单元与集成测试 100% 通过 (0 Token 消耗，0 外部依赖) │
│ • 重点覆盖：Agent 运行时加固、流式长文本预算保护、IDOR 隔离等 │
└────────────────────────────────────────────────────────┘
```

### 2. 核心加固工程成果亮点

详细技术复盘请参考技术总结文档 [`docs/PROJECT_FINAL_TECHNICAL_SUMMARY.md`](docs/PROJECT_FINAL_TECHNICAL_SUMMARY.md)：

- **P0-1 参数错误有界自修复**：Pydantic Schema 校验失败时自动生成字段类型与必填语义引导，设置 `max_consecutive_param_errors=2` 上限，超限熔断剥夺工具调用权限，避免盲目空耗 Token；
- **P0-2 参数指纹防震荡**：基于 `tool_name + canonical_args`（键排序、剔除 None、剥离空白）构建规范化指纹，连续 2 次相同工具与参数自动拦截并引导模型自省反思；
- **P0-3 畸形 JSON 容错与 100% 协议对齐**：双层剥离 Markdown 代码块包裹，宽容解析；无论工具调用成功、参数错误还是被拦截，均生成对应 `ToolMessage`，保证 `tool_call_id` 严格 1:1 配对，彻底解决 OpenAI HTTP 400 协议报错；
- **动态 Token 预算贯通**：`ContextBudgetManager` 动态预留输出空间（`reserved_output_tokens=4096`），并通过请求级局部变量安全下传至底层 HTTP Payload，杜绝上下游预算脱节与并发状态污染。

### 3. 容器化集成验收概览 (P2.3 RC Automated Suite)

在真实 Docker 容器环境下运行官方自动化测试套件，验证结论如下：

```
======================================================================
AGENTSTUDIO-RAG P2.3 RC VERIFICATION SUMMARY (SERVER & LOCAL MODES)
======================================================================
CAPABILITY                | STATUS   | DETAILS
----------------------------------------------------------------------
Health & Auth Mode        | PASS     | Mode=server & local, DB=Connected
Direct LLM                | PASS     | Answer len=116, Time=3.59s (0 Retrieval Overhead)
Workflow RAG              | PASS     | Citations=1, Keyword 'RC-Phoenix-2026' Verified
LangGraph Agent           | PASS     | 9 Agent State Events, Tool Execution Verified
Attachment System         | PASS     | Upload, Parsing, Context Search, Deletion Clean
Long-term Memory          | PASS     | Extraction, Vector Query, Update, Deletion Clean
Model Management          | PASS     | Active=1, Latency=1822ms, Credential Encrypted
Multi-tenant IDOR Guard   | PASS     | Cross-User Resource Access Blocked (HTTP 404)
Offline Model Inference   | PASS     | docker run --network none (0 Network Calls)
Graceful Shutdown         | PASS     | SIGTERM handled in 30s window (ExitCode: 0)
Schema Auto-Migration     | PASS     | Legacy DB schema upgraded with 0 data loss
Backup & Restore          | PASS     | Full PostgreSQL snapshot & uploads restored
======================================================================
```

---

## 十一、已知限制与工程边界 (Known Limitations & Trade-offs)

1. **PDF 文档解析范围**：当前内置 PyMuPDF 引擎基于文本层直接提取。若上传的 PDF 为纯手机拍照或无文本层的扫描图片，需前置经过 OCR 工具（如 PaddleOCR / Tesseract）转换为带文本层的双层 PDF 后再行上传解析。
2. **单机内存向量计算规模**：当前采用 FastEmbed 与 NumPy 内存余弦相似度计算，在评测集与中小规模文档库（数千分块级别）中验证了毫秒级向量相似度计算。对于十万分块以上的大规模私有文档库，建议在生产环境中扩展接入独立外置向量数据库（如 Qdrant / Milvus / pgvector 并启用 HNSW 索引）。
3. **CPU 重排与检索累计耗时**：在无独立 GPU 的本地 CPU 环境下，方案 D 检索阶段累计耗时（分阶段计时求和，公式为 `rrf_dt + rerank_res.duration_ms`）P50 约为 1.33s（P95 约为 1.43s，涵盖双路并行召回、RRF 融合与 20 组候选分块 Cross-Encoder 深度打分）。在生产高并发场景下，建议为 Cross-Encoder 配备专用 GPU 节点或启用轻量级小模型与 TensorRT 加速以缩短检索总耗时。
4. **第三方依赖许可证说明**：PDF 解析组件 PyMuPDF 采用双重授权协议（AGPLv3 / 商业授权）。本项目作为技术展示与学习交流参考；若在商业化场景或闭源商业衍生中使用，建议使用者结合自身法务与合规要求，审慎评估并遵守其许可证条款，或选择替换为 Apache-2.0 兼容的解析库（如 `pypdf`）或采购商业授权。

---

## 十二、版权声明与开源说明 (Copyright & License Notice)

- **版权归属 (Copyright Notice)**：本项目（AgentStudio-RAG）的全部原创源代码、架构设计、技术文档及评测基准数据著作权均归原作者所有，**保留所有权利 (All Rights Reserved)**。
- **展示性质与许可状态 (Portfolio Showcase & No License Granted)**：
  - 本项目作为原作者个人技术能力、工程架构实践与技术交流的作品集公开展示。
  - **本项目当前未授予开源软件使用许可（No Open Source License Granted）**。除依据 GitHub 服务条款（GitHub Terms of Service）所允许的在 GitHub 平台内查看代码及进行 Fork 等平台原生交互操作外，原作者未向任何第三方授予超出该范围的使用、修改、分发、复制、重新编译、嵌入集成或商业运营许可。
  - 未经原作者书面明确授权，请勿将本项目的原创代码、业务逻辑、Prompt 编排或衍生工程用于任何商业化软件产品、闭环 SaaS 服务售卖或营利性培训咨询。
- **第三方组件许可证 (Third-Party Licenses)**：
  - 本项目使用的所有第三方开源库、依赖框架及预训练模型（如 FastAPI, LangGraph, React, Ant Design, FastEmbed, ONNX Runtime, PyMuPDF, BGE 等）均严格遵循各自官方发布的开源许可证。
  - 相关第三方组件的知识产权与版权归各自权利人所有。关于特定依赖组件（如 PyMuPDF 的 AGPLv3 / 商业双重许可）的商业使用，请使用者自行遵守对应开源协议或向权利人获取相应授权。
- **致谢与启发 (Acknowledgements)**：特别鸣谢开源社区优秀项目 [Ragent](https://github.com/nageoffer/ragent) 在企业级知识问答系统架构设计上的宝贵启发。

---
*AgentStudio-RAG Team · Release Candidate v1.3.0-rc.3 · 2026*
