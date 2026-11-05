# AgentStudio-RAG — P3.1：GitHub 仓库发布准备与开源交付报告

> **报告日期**：2026年11月5日  
> **目标版本**：`v1.3.0-rc.1` (`RC-Phoenix-2026`)  
> **目标仓库**：`AgentStudio-RAG`  
> **核心定位**：全栈企业级 Agentic RAG 智能知识问答平台（求职作品集与开源发布就绪）  
> **执行原则**：**只做发布准备与安全合规整理，不自动 Push 远程，不自动创建公开仓库，不重写历史，等待用户明确授权。**

---

## 一、Git 仓库当前状态

1. **Git 初始化状态**：
   - 检查确认：在执行本阶段前，工作区根目录下未初始化 Git 仓库（无 `.git` 文件夹）。
   - 当前操作：已在本地安全初始化全新的 Git 仓库，并将默认主分支规范命名为 `main`。
2. **分支与提交历史**：
   - 当前分支：`main`。
   - 提交历史：`No commits yet`（纯净全新工作区，尚未创建任何 Commit）。
3. **未提交的修改与跟踪状态**：
   - 经由配置完善的 `.gitignore` 规则过滤后，工作区内仅呈现合规的源码、配置文件、官方文档与核心测试脚本，所有敏感与非受控文件均已被精确排除。
4. **远程仓库 (Remote) 配置**：
   - 执行 `git remote -v` 确认：**未配置任何远程仓库地址**（零外部网络连接风险）。
5. **历史版本 Tag**：
   - 执行 `git tag -l` 确认：**本地不存在任何历史 Git Tag**，为后续规范打标 `v1.3.0-rc.1` 提供了纯净环境。

---

## 二、敏感文件排查与安全审计结果

系统通过自动化敏感信息扫描脚本（`scripts/scan_secrets.py`）与文件树递归排查，完成了全系统的安全合规审计：

| 审计项目 | 检查结果与实际状态 | 安全防护措施与隔离手段 | 风险评级 |
| :--- | :--- | :--- | :---: |
| **`.env` 与生产配置** | 根目录存在用户实际使用的 `.env`（含真实 DeepSeek Key）。 | `.gitignore` 已将 `.env` 及 `.env.*` 全面排除，仅保留安全模板 `.env.example`。 | **SAFE (已隔离)** |
| **`SERVER_AUTH_SECRET`** | 业务代码中无硬编码值；`docker-compose.yml` 强制通过环境变量注入。 | 强制校验机制已生效：使用占位符启动 Server 模式立即拒启（Exit 1）。 | **SAFE (已隔离)** |
| **`MODEL_ENCRYPTION_KEY`** | 本地曾生成 `.model_master_key` 位于 `data/storage/`。 | 已通过 `**/.model_master_key*` 规则在 `.gitignore` 中绝对屏蔽。 | **SAFE (已隔离)** |
| **大模型 API Key 泄露排查** | 排查发现 `scripts/test_p2_3_suite.py` 曾包含 fallback 字符串。 | **已立即修复**：改由 `os.getenv("OPENAI_API_KEY", "")` 读取，全代码库无明文 Key。 | **RESOLVED (已消除)** |
| **SQLite 数据库文件** | 本地存在测试生成的 `agentstudio_rag.db` 与 `stage5_*.db`。 | 已通过 `*.db`, `*.sqlite*` 规则在 `.gitignore` 中全面屏蔽。 | **SAFE (已隔离)** |
| **PostgreSQL 备份与数据** | 本地 `backups/` 目录存有历史 `database.sql` 与压缩包。 | 已将 `backups/`, `*.sql`, `*.tar.gz`, `*.zip` 写入 `.gitignore` 全面排除。 | **SAFE (已隔离)** |
| **用户上传文档与切块** | 本地 `data/uploads/` 存有测试上传的文件碎片。 | 已通过 `data/`, `backend/data/storage/`, `backend/data/uploads/` 彻底排除。 | **SAFE (已隔离)** |
| **本地模型权重缓存** | 容器曾下载 BGE 与 ONNX 模型权重至 `/app/models_cache`。 | 宿主机无冗余文件，同时 `.gitignore` 与 `.dockerignore` 均已排除 `models_cache/`。 | **SAFE (已隔离)** |
| **截图隐私信息审计** | 审计 `docs/screenshots/` 目录下的 11 张官方高清演示截图。 | 用户名均为演示脱敏账号（`admin`, `user_b`, `usr_local_default`），无手机号或个人信息。 | **SAFE (合规)** |

---

## 三、Git 历史风险评估

- **核心结论**：**历史零泄露风险 (Zero Historical Leak Risk)**。
- **原因剖析**：由于本项目此前在开发阶段未曾初始化本地 Git 仓库或向远程推送历史 Commit，因此**不存在任何敏感密钥、凭据或私人文件被意外“存入历史版本”的情况**。
- **无需高危操作**：无需使用 `git filter-repo`、`git filter-branch` 或 BFG Repo-Cleaner 等复杂且不可逆的历史重写工具。
- **首发安全性保障**：只要首次提交（Initial Commit）基于当前已生效的 `.gitignore` 执行，整个仓库在推送到 GitHub 时即可确保 100% 绝对纯净。

---

## 四、`.gitignore` 规则整理与强化

当前根目录 `.gitignore` 已根据开源工程最佳实践完成全面升级，精确分层如下：

```gitignore
# ==============================================================================
# AgentStudio-RAG — Git Ignore Configuration
# ==============================================================================

# Python runtime, bytecode & caches
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
.venv/
backend/.venv/
venv/
ENV/

# Testing & Code Quality
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/
.ruff_cache/

# Environment Variables & Secrets (CRITICAL SECURITY)
.env
.env.*
!.env.example
*.local
**/.model_master_key*

# Backups, Database Dumps & Archives
backups/
backups_test/
*.sql
*.tar.gz
*.zip

# Node.js & Frontend Build Artifacts
node_modules/
frontend/node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
dist/
frontend/dist/

# Runtime Data, User Uploads & Storage (Persisted via Docker Volumes)
data/
backend/data/storage/
backend/data/uploads/
backend/data/benchmark/
**/storage/*
!**/storage/.gitkeep
**/uploads/*
!**/uploads/.gitkeep
*.db
*.db-journal
*.db-wal
*.db-shm
*.sqlite*

# Local Model Weights & Cache
models_cache/
backend/models_cache/
**/models_cache/

# Logs & Temporary Files
*.log
**/*.log
scratch/
temp/
tmp/
edge_*

# Root temporary testing screenshots & test artifacts
/*.png
test_*.pdf

# IDE & OS Specific
.idea/
.vscode/
*.swp
*.swo
.DS_Store
Thumbs.db
Desktop.ini
```

---

## 五、建议提交的文件清单 (Staging Whitelist)

在执行 Initial Commit 时，建议收录以下核心资产（共 6 大类别）：

```
[根目录配置与编排]
├── .dockerignore
├── .env.example
├── .gitignore
├── Dockerfile.backend
├── Dockerfile.frontend
├── docker-compose.local.yml
├── docker-compose.yml
├── README.md
├── start-dev.bat
└── start-dev.ps1

[网关与反向代理]
└── docker/
    └── nginx/nginx.conf

[FastAPI 后端业务源码与测试]
└── backend/
    ├── app/ (核心业务: agent, api, attachments, core, db, llm, memory, models, rag, retrieval, routing, schemas, services)
    ├── benchmark/ (M6 检索基准测试集与标注数据)
    ├── create_admin.py
    ├── entrypoint.sh
    ├── pyproject.toml
    ├── requirements.txt
    ├── scripts/ (离线评测与知识库填充脚本)
    └── tests/ (完整的单元测试与集成测试套件)

[React 18 前端控制台源码]
└── frontend/
    ├── index.html
    ├── nginx.conf
    ├── package.json
    ├── package-lock.json
    ├── tsconfig.json
    ├── vite.config.ts
    └── src/ (页面组件、主题配置、API 客户端与类型定义)

[技术文档库与官方高清截图]
└── docs/
    ├── ARCHITECTURE.md (系统技术架构白皮书)
    ├── DEPLOYMENT.md (生产部署与运维手册)
    ├── DEMO_GUIDE.md (5~8 分钟实操演练指南)
    ├── P2_3_RELEASE_CANDIDATE_REPORT.md (P2.3 RC 终审报告)
    ├── P3_1_GITHUB_RELEASE_PREPARATION.md (本文档)
    ├── 历史阶段里程碑报告 (P0, M1-M6, P1.1-P1.3)
    └── screenshots/ (11 张官方高清 1920x1080 界面截图)

[运维自动化与质量检验脚本]
└── scripts/
    ├── backup.ps1 / backup.sh
    ├── restore.ps1 / restore.sh
    ├── scan_secrets.py (密钥防泄露审计脚本)
    ├── test_p2_3_db_migration.py (数据库无损迁移验证)
    ├── test_p2_3_server_security.py (多租户安全与 IDOR 验证)
    └── test_p2_3_suite.py (7 大核心业务集成验证套件)
```

---

## 六、README 优化与升级结果

根目录 `README.md` 已全面重构并升级为高水准的开源求职主页标准，重点涵盖了以下 13 大维度：

1. **统一英文标准简介**：
   > `A full-stack Agentic RAG platform built with FastAPI, LangGraph, React and Docker, featuring hybrid retrieval, long-term memory, multi-user authentication and local/server deployment.`
2. **实际运行界面展示 (Showcase)**：精选 5 张代表性高清截图嵌入表格，直观呈现流式问答工作台、真实页码引用抽屉、知识库状态流、模型管理及多租户隔离。
3. **Mermaid 系统架构全景图**：清晰描摹 Client → Gateway → Core Engine → Retrieval Layer → Persistence Layer 五层架构。
4. **三态执行引擎深度对比矩阵**：横向对比 Direct LLM、Workflow RAG 与 LangGraph Agent 在使用场景、检索开销、时延、引用溯源与幻觉控制上的差异。
5. **高精混合检索流程图解**：清晰展现 Dense + BM25 并行召回、RRF (k=60) 融合打分与 ONNX Cross-Encoder 深度重排序的完整技术流。
6. **跨会话长期记忆机制**：详解异步提炼、500 Tokens 预算池管理与动态注入原理。
7. **Local / Server 双模式对比**：解释 SQLite 免登录与 PostgreSQL 多租户 JWT 的适用场景。
8. **Docker Compose 极速启动指引**：分别提供 Local 与 Server 的一行命令启动与停止指南。
9. **大模型配置说明**：覆盖 DeepSeek、OpenAI、Ollama 本地私有化集群配置与 Fernet 凭据对称加密。
10. **清晰的代码目录树**：标注每一个顶级目录与子模块的职责。
11. **技术栈清单与选型考量**：详述选用 Python 3.12, FastAPI, SQLAlchemy 2.0, FastEmbed, ONNX Runtime, PyMuPDF, LangGraph 的工程考量。
12. **测试与部署验证数据**：列出 P2.3 RC 在 7 大功能与 5 大生产专项测试中的真实测试通过记录。
13. **已知限制与边界说明**：客观说明图片扫描件需要前置 OCR、单机向量规模与 PyMuPDF 依赖边界，避免浮夸宣传。

---

## 七、开源许可证待确认事项 (License Decision)

根据您的指示，**我不擅自为您创建或声明具体的开源许可证文件**。在此列出经过法务与技术合规分析后的关键信息供您决策：

### 1. 常见可选开源许可证对比

| 许可证选项 | 特点与优势 | 适用考量 | 推荐指数 |
| :--- | :--- | :--- | :---: |
| **MIT License** | 极其宽松、行业普及度最高，允许任意商用、修改与再分发，仅保留原作者署名。 | 最利于技术求职与作品集展示，降低招聘方与社区开发者的使用顾虑。 | ⭐⭐⭐⭐⭐ (首选推荐) |
| **Apache License 2.0** | 类似 MIT，但增加了明确的**专利权授权条款**与商标限制，代码修改声明更严格。 | 若未来希望以该系统为基础申请技术专利或成立商业化实体，建议采用。 | ⭐⭐⭐⭐ |

### 2. 第三方代码与依赖组件的合规提示
- **PyMuPDF (fitz)**：
  - PyMuPDF 采用 **AGPLv3** 与商业授权的双重许可模式。
  - **合规现状**：作为开源项目或求职作品集代码分享，完全兼容合法；但若第三方企业将 AgentStudio-RAG 作为专有商业闭环 SaaS 售卖且不开源自身代码，根据 AGPL 条款可能受到传染性约束。
  - **建议处理**：在 README 中已保留明确说明。若您未来希望彻底规避 AGPL 商业传染性，可在后续演进中将默认 PDF 解析器无缝切换为 MIT 兼容的 `pypdf`。
- **BAAI/bge-small-zh-v1.5**：MIT / Apache 2.0，允许自由使用与商用。
- **Xenova/bge-reranker-base**：Apache 2.0，允许自由使用。
- **Ragent（参考启发项目）**：MIT License，系统为完全独立的 Python 原生重构，无代码直接借用，致谢已在 README 规范呈现。

---

## 八、Release Candidate 规范草稿 (`v1.3.0-rc.1`)

以下为准备在 GitHub Releases 发布的标准 Markdown 文档草稿：

```markdown
# AgentStudio-RAG v1.3.0-rc.1 — Release Candidate

AgentStudio-RAG 是基于 Python 的企业级 Agentic RAG 智能知识问答系统。参考开源项目 Ragent 进行全栈重新架构与深度重构，旨在提供一个兼顾个人开箱即用与企业级多租户生产交付的可靠解决方案。

### 🚀 核心系统亮点
- **三态执行引擎**：Direct LLM (零检索直答) / Workflow RAG (确定性溯源) / LangGraph ReAct Agent (多步状态图推理)。
- **高精混合检索系统**：Dense Vector (BGE) + BM25 关键词双路召回，通过 RRF (k=60) 融合打分，并由轻量级 ONNX Cross-Encoder 深度重排。
- **跨会话长期记忆**：自动提炼用户画像与约束事实，跨会话向量召回，受控 Token 预算管理。
- **单会话临时附件系统**：会话级即时文档上传，PyMuPDF 内存解析，单会话精准问答，与主知识库物理隔离。
- **双部署模式原生支持**：
  - **Local 模式**：内置 SQLite 与轻量嵌入模型，免登录开箱即测。
  - **Server 模式**：PostgreSQL 16 独立数据库，JWT 身份认证，PBKDF2 密码加盐，严格防御水平越权 (IDOR)。
- **生产级容器化架构**：提供完整的 Dockerfile 与两套独立 Docker Compose 编排，支持无外网离线脱机运行。

### 📊 验证与测试结论
- 经全流程集成验收套件 (`scripts/test_p2_3_suite.py`) 验证，7 大业务模块与 5 大生产专项（离线前向推理、平滑停机退出码 0、历史老库 Schema 无损迁移、全量数据备份与还原）全部达成 100% 通过。

### 📦 快速体验
```bash
git clone https://github.com/your-username/AgentStudio-RAG.git
cd AgentStudio-RAG
cp .env.example .env
# 填入大模型 API 密钥 (DeepSeek / OpenAI / Ollama)
docker compose -f docker-compose.local.yml up -d
# 访问: http://localhost:8080
```
```

---

## 九、Private GitHub 仓库安全上传实操步骤

为确保万无一失，**强烈建议首次上传时先在 GitHub 创建 Private（私有）仓库**，在私有环境下完成在线预览与最终确认后，再一键切换为 Public。

### 步骤 1：本地建立首次纯净提交 (Initial Commit)
在本地终端执行：
```bash
# 1. 将所有通过 .gitignore 过滤后的合规源码与文档暂存
git add .

# 2. 检查暂存区，确认绝对无 .env、.db、backups、models_cache 等文件
git status

# 3. 提交本地初始版本
git commit -m "feat: initial commit for AgentStudio-RAG v1.3.0-rc.1"

# 4. 创建发布候选标签 (Release Candidate Tag)
git tag -a v1.3.0-rc.1 -m "Release Candidate v1.3.0-rc.1 (RC-Phoenix-2026)"
```

### 步骤 2：在 GitHub 上创建目标私有仓库
1. 登录您的 GitHub 账号，访问 `https://github.com/new`。
2. **Repository name**：填写 `AgentStudio-RAG`。
3. **Description**：填写推荐简介：
   `A full-stack Agentic RAG platform built with FastAPI, LangGraph, React and Docker, featuring hybrid retrieval, long-term memory, multi-user authentication and local/server deployment.`
4. **Visibility**：选择 **Private**（私有）。
5. **Initialize this repository with**：**不要勾选**任何 Add README, Add .gitignore, Choose a license（本地已有完整规范）。
6. 点击 **Create repository**。

### 步骤 3：绑定远程仓库并安全推送
```bash
# 1. 添加远程源 (替换为您实际的 GitHub 用户名)
git remote add origin https://github.com/<your-github-username>/AgentStudio-RAG.git

# 2. 安全推送到 main 分支并同步 tags
git push -u origin main --tags
```

---

## 十、开源公开 (Public) 前的最终核对清单

当您在 GitHub 内部完成私有仓库确认，准备将仓库切换为公开（Public）前，只需完成以下最后 4 项确认：

- [ ] **确认并添加 `LICENSE` 文件**：若您同意采用 MIT License，在根目录放置标准的 `LICENSE` 文件后提交。
- [ ] **GitHub 仓库主页装饰**：
  - 在 GitHub 仓库顶栏右侧点击 ⚙️ 设置，勾选 **Releases** 与 **Discussions**。
  - 在 **About** 中添加标签（Topics）：`fastapi`, `langgraph`, `rag`, `react`, `docker`, `hybrid-search`, `llm`, `vector-search`。
- [ ] **创建正式 Release 页面**：
  - 点击 GitHub 右侧 **Releases** -> **Draft a new release**。
  - 选择现有标签 `v1.3.0-rc.1`。
  - 粘贴本报告第八节提供的 **Release Notes** 草稿并发布。
- [ ] **切换仓库公开属性**：
  - 进入仓库 `Settings` -> 滚动至最底部 `Danger Zone` -> 点击 `Change repository visibility` -> 选择 `Make public`。

---

## 终态声明与停步确认

**严格遵照用户指令：**
- **当前未向任何远程地址推送代码。**
- **未私自创建远程 GitHub 仓库。**
- **未擅自选择开源许可证。**
- **全系统已安全规整完毕，等待您的授权指令！**
