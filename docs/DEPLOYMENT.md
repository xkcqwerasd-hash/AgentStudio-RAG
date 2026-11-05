# AgentStudio-RAG — 生产部署与运维实战手册 (Deployment Guide)

> **版本**：`v1.3.0-RC1`  
> **适用范围**：面向系统管理员、DevOps 工程师与终端用户的全场景部署指南，覆盖单机免登录体验与企业级多人高可用生产交付。

---

## 一、部署前置要求与环境规格 (Prerequisites)

### 1. 硬件规格建议

| 部署形态 | 最低配置 | 推荐生产配置 | 存储与网络要求 |
| :--- | :--- | :--- | :--- |
| **Local 模式 (个人单机)** | 2 核 CPU，2GB 内存 | 4 核 CPU，8GB 内存 | 10GB 磁盘，可访问外部大模型 API（如 DeepSeek/OpenAI）或本地 Ollama |
| **Server 模式 (团队生产)**| 4 核 CPU，8GB 内存 | 8 核 CPU，16GB 内存 | 50GB+ 高速 SSD，建议配置 100Mbps 稳定带宽 |

### 2. 软件运行依赖
- **操作系统**：Linux (Ubuntu 20.04+, Debian 11+, CentOS 8+ / RHEL 8+), macOS (Apple Silicon / Intel), Windows 10/11 (WSL2 或 Docker Desktop 4.25+)
- **容器环境**：Docker Engine 24.0+, Docker Compose v2.20+
- **网络端口占用确认**：
  - `8080`：前端与网关对外 Web 访问端口（可于 Compose 文件中自定义映射）
  - `5432`：PostgreSQL 生产数据库（仅在容器内部虚拟网桥暴露，严禁直接映射公网）

---

## 二、Local 单人模式部署（开箱即用，免登录）

Local 模式是为个人本地研究、隐私问答与单机演示设计的极简部署方案。

### 1. 一键启动流程
```bash
# 1. 进入代码仓库根目录
cd agentstudio-rag

# 2. 准备环境变量配置文件
cp .env.example .env

# 3. 配置您的大模型密钥（编辑 .env 文件）
# OPENAI_API_KEY=sk-your-actual-api-key
# OPENAI_BASE_URL=https://api.deepseek.com
# LLM_MODEL=deepseek-chat

# 4. 启动 Local 容器编排栈
docker compose -f docker-compose.local.yml up -d
```

### 2. 验证与访问
- 浏览器打开：`http://localhost:8080`
- 系统将直接进入智能问答工作台，顶栏标有绿色 **「本地免登模式」** 徽章。
- 所有聊天记录、知识库和临时附件均自动持久化保存在 Docker 卷 `agentstudio_local_data` 中。

### 3. 服务停止与重启
```bash
# 暂停运行（保留数据）
docker compose -f docker-compose.local.yml stop

# 彻底关闭容器（保留数据卷）
docker compose -f docker-compose.local.yml down

# 彻底清理包括数据卷在内的全部本地数据（谨慎操作！）
docker compose -f docker-compose.local.yml down -v
```

---

## 三、Server 多人生产模式部署（企业多租户，高可用）

Server 模式采用标准的微服务生产拓扑结构：独立 PostgreSQL 16 数据库、Nginx 生产反向代理网关、JWT 严格鉴权与 PBKDF2 密码保护。

```mermaid
flowchart LR
    Client["终端浏览器 (User / Admin)"] -->|HTTP / HTTPS:8080| Nginx["Nginx 网关反向代理"]
    
    subgraph DockerBridge["Docker 隔离网桥 (agentstudio_net)"]
        Nginx -->|/api/* 转发:8000| Backend["FastAPI Backend (Uvicorn 4 Workers)"]
        Nginx -->|/* 静态分发:80| Static["React SPA 静态资源"]
        Backend -->|TCP:5432 内部连接| PG["PostgreSQL 16 Alpine"]
        Backend --> StorageVol[("数据持久卷 agentstudio_data")]
        PG --> PGVol[("数据库持久卷 agentstudio_pgdata")]
        Backend --> ModelVol[("模型缓存卷 agentstudio_models")]
    end
```

### 1. 生成高强度生产安全密钥
在部署前，必须生成唯一的随机密钥注入生产环境，禁止使用出厂默认字符串：
```bash
# 在 Linux / macOS 终端生成 32 位安全随机密钥：
openssl rand -hex 32
```

### 2. 编辑生产环境 `.env` 文件
```ini
# ==============================================================================
# AgentStudio-RAG — 生产环境配置 (.env)
# ==============================================================================
PROJECT_NAME=AgentStudio-RAG
DEBUG=false

# 部署模式与安全密钥
DEPLOYMENT_MODE=server
SERVER_AUTH_SECRET=replace_with_your_own_generated_64_char_hex_secret_here_000000
MODEL_ENCRYPTION_KEY=replace_with_your_own_generated_fernet_base64_encryption_key==

# 用户注册策略：初始化管理员后建议置为 false 阻止公开注册
SERVER_ALLOW_REGISTRATION=true
AUTO_FIRST_USER_ADMIN=true

# 生产数据库连接配置 (与下文 PostgreSQL 服务一致)
POSTGRES_USER=agentstudio
POSTGRES_PASSWORD=Super_Strong_Production_Password_2026!
POSTGRES_DB=agentstudio_rag
DATABASE_URL=postgresql+asyncpg://agentstudio:Super_Strong_Production_Password_2026!@postgres:5432/agentstudio_rag

# Uvicorn 异步工作进程数（建议设为 CPU 核心数 * 1 ~ 2）
UVICORN_WORKERS=4

# 默认大模型端点（用户亦可在网页端随时添加其他模型配置）
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-your-actual-api-key
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
```

### 3. 一键构建并启动服务栈
```bash
docker compose up -d --build
```

### 4. 检查服务健康状态
```bash
docker compose ps
```
正常输出应显示 `agentstudio-postgres`、`agentstudio-backend` 与 `agentstudio-frontend` 三个容器状态均显示为 `(healthy)`。

### 5. 初始化首位系统管理员 (CLI)
若配置中未启用自动首位管理员，可通过系统内置的安全 CLI 命令在后端容器内初始化管理员账号：
```bash
docker compose exec backend python create_admin.py create -u admin -p "SuperSecurePassword123!" -e admin@yourdomain.com
```

---

## 四、数据持久化与卷管理 (Volume Management)

系统严格遵循无状态容器设计原则，所有易变与持久化数据完全存放在独立命名的 Docker 卷中：

| 命名卷名称 | 挂载目标路径 | 存储数据类型与关键作用 |
| :--- | :--- | :--- |
| `agentstudio_pgdata` | `/var/lib/postgresql/data` | PostgreSQL 16 数据库集群物理数据表与索引文件。 |
| `agentstudio_data` | `/app/data` | 用户上传的原始知识库文档、单会话临时附件及派生索引元数据。 |
| `agentstudio_models` | `/app/models_cache` | 本地预下载的 BGE 语义嵌入权重与 ONNX 重排序模型文件。 |

> **注意事项**：执行容器版本升级 (`docker compose pull && docker compose up -d`) 不会影响命名卷中的数据。严禁在未经全量备份的情况下执行带有 `-v` 参数的清理命令。

---

## 五、备份与恢复标准操作流程 (Backup & Recovery)

系统自带完善的跨平台自动化备份脚本，在 Windows 与 Linux 环境下均可无缝执行。

### 1. 执行全量冷/热备份
备份脚本将自动完成 PostgreSQL 逻辑快照（`pg_dump --clean`）与用户上传文档目录的无损打包：

```powershell
# Windows (PowerShell):
powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1 -Mode server -BackupRoot .\backups
```

```bash
# Linux / macOS (Bash):
chmod +x ./scripts/backup.sh
./scripts/backup.sh --mode server --backup-root ./backups
```

备份完成后，`backups/` 目录下将生成包含当前时间戳的归档目录（如 `20261105_105640_server/`），内部包含：
- `database.sql`：包含重建所有数据表、主外键约束与索引的 SQL 脚本
- `uploads_data.tar.gz`：所有用户文档与附件资产压缩包
- `manifest.json`：备份系统版本与生成时间戳

### 2. 灾难恢复操作
当需要回滚数据或迁移至新服务器时，执行对应的恢复脚本：

```powershell
# Windows (PowerShell):
powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -BackupDir .\backups\20261105_105640_server -Mode server
```

```bash
# Linux / macOS (Bash):
chmod +x ./scripts/restore.sh
./scripts/restore.sh --backup-dir ./backups/20261105_105640_server --mode server
```

---

## 六、无外网隔离环境运行指南 (Offline Deployment)

针对金融、政企与内网机房等完全与互联网物理隔离的生产场景：

1. **镜像内置模型预载**：
   `Dockerfile.backend` 在镜像构建时已内置固化 `BAAI/bge-small-zh-v1.5` 与 `Xenova/bge-reranker-base` 权重至 `/app/models_cache`。
2. **纯脱机验证**：
   可通过以下命令验证镜像在完全断网状态下的自主推理能力：
   ```bash
   docker run --rm --network none agentstudio-rag-backend:latest python -c "
   from fastembed import TextEmbedding
   from app.retrieval.reranker import LocalONNXReranker
   emb = TextEmbedding(model_name='BAAI/bge-small-zh-v1.5')
   vecs = list(emb.embed(['离线测试']))
   print('Offline Embedding Success: dim=', len(vecs[0]))
   reranker = LocalONNXReranker()
   scores = reranker.compute_score('测试', ['测试文档'])
   print('Offline Reranker Success: score=', scores[0])
   "
   ```
3. **离线大模型连接**：
   在隔离内网中，将 `.env` 中的 `OPENAI_BASE_URL` 指向局域网内私有部署的 Ollama、vLLM 或 LocalAI 服务（例如 `http://192.168.1.100:11434/v1`），即可实现 100% 完全断网下的本地智能化知识问答闭环。

---

## 七、日志审计与故障排查 (Troubleshooting)

### 1. 容器日志实时追踪
```bash
# 追踪后端业务实时日志 (包含流式问答与错误堆栈)
docker compose logs -f backend

# 追踪网关请求分发与访问记录
docker compose logs -f frontend

# 追踪数据库连接池与事务日志
docker compose logs -f postgres
```

### 2. 常见故障自查清单

| 故障现象 | 根本原因定位 | 标准解决方案 |
| :--- | :--- | :--- |
| **启动后报错 `Refusing to start with missing or default secret`** | 未配置 `SERVER_AUTH_SECRET` 或使用了出厂占位符。 | 打开 `.env`，填入自定义强随机密钥后执行 `docker compose up -d` 重启。 |
| **问答接口抛出 500 且提示 `can't subtract offset-naive and offset-aware datetimes`** | 驱动传递给 PostgreSQL 的时间戳对象带有时区标记。 | 确保运行最新 `v1.3.0-RC1` 镜像，后端全系统已统一采用 `utc_now()` 规整时间戳。 |
| **上传超大文档（>20MB）提示 413 Payload Too Large** | Nginx 网关或后端配置限制上传大小。 | 在 `docker/nginx/nginx.conf` 中调整 `client_max_body_size` 参数，并在 `.env` 中提升 `MAX_UPLOAD_SIZE_MB`。 |
| **流式打字效果消失，变成一次性吐出整段回答** | 反向代理开启了 HTTP 缓冲区导致 SSE 事件积压。 | 确保使用官方提供的 Nginx 配置，其中已对 `/api/` 路由设置 `proxy_buffering off;` 与 `proxy_cache off;`。 |

---
*AgentStudio-RAG Production Deployment & Operations Manual · 2026*
