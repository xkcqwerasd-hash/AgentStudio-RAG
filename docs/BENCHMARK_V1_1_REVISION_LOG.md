# AgentStudio-RAG Benchmark 数据集 V1.1 修订说明文档

> **版本**：Benchmark Generation Dataset v1.1  
> **修订日期**：2026-11-06  
> **基准版本文件**：[`eval/datasets/benchmark_generation_v1.jsonl`](file:///e:/develop/python%20agentic%20RAG/eval/datasets/benchmark_generation_v1.jsonl)  
> **修订版本文件**：[`eval/datasets/benchmark_generation_v1_1.jsonl`](file:///e:/develop/python%20agentic%20RAG/eval/datasets/benchmark_generation_v1_1.jsonl)  
> **底库数据库**：[`backend/data/storage/eval_benchmark.db`](file:///e:/develop/python%20agentic%20RAG/backend/data/storage/eval_benchmark.db) (只读，SHA-256: `5118ca5fb8d9f136fe63b743b3afe9428962ec688d4b8ec84e2ee596bdfbe3d0`)

---

## 一、修订背景与原则

在第一轮全量 30 题正式评测（E2.1.2）及深度根因分析（E2.1.3，详见 [`docs/E2_1_3_FAILURE_ANALYSIS.md`](file:///e:/develop/python%20agentic%20RAG/docs/E2_1_3_FAILURE_ANALYSIS.md)）中，我们发现有 3 道测试题存在**标尺与事实脱节**的问题：
1. **GEN-17**：题目假定底库包含“心跳探测间隔 5 秒，连续 3 次失败判定下线”，但对 `eval_benchmark.db` 的完整穿透审计证实底库所有 16 个分块中均未出现该句，底库仅记载了 `ModelHealthStore` 的核心聚合职责（QPS、P95 延迟、错误率、熔断状态及 Prometheus `/metrics` 暴露）。
2. **GEN-22**：参考答案中编写了 `keepalive_timeout=60s` 与 `connection_timeout=2000ms`，但底库分块实际记载的参数为 `min_idle=10`、`max_connections=100` 与 `acquire_timeout_ms=3000ms`。
3. **GEN-24**：参考答案中编写了“重试 3 次，间隔 200/400/800ms”，而底库分块明确规定“仅针对幂等的 GET 查询在超时后自动重试 2 次，避让间隔 initial_interval_ms 为 500 毫秒；非幂等的 POST 严格禁止重试”。

为此，在 E2.1.4 阶段启动数据集版本化升级，制定 **V1.1 版本**：
- **保留历史不可变性**：原始 `benchmark_generation_v1.jsonl` 原样封存，不覆盖历史评测报告；
- **最小化变更**：题号总量（30 题）保持严格不变，其余 27 道题内容与元数据完全保持不变；
- **真实物理对齐**：仅修订 GEN-17、GEN-22、GEN-24，使其 100% 忠实于 `eval_benchmark.db` 物理底库事实。

---

## 二、数据集整体结构对比

| 题目分类 (Category) | 题目数量 | V1 状态 | V1.1 状态 | 修订题目 ID |
| :--- | :---: | :---: | :---: | :---: |
| **single_fact** (单跳事实抽取) | 10 | 10 | 10 | 无修订 (100% 一致) |
| **multi_hop** (多跳跨文档推理) | 8 | 8 | 8 | **GEN-17** |
| **parametric** (精确参数提取) | 6 | 6 | 6 | **GEN-22**, **GEN-24** |
| **unanswerable** (知识库无依据拒答) | 6 | 6 | 6 | 无修订 (100% 一致) |
| **总计** | **30** | **30** | **30** | **3 题修订，27 题完全一致** |

---

## 三、3 道题目详细修订对照表

### 1. GEN-17 (Multi-hop: 跨文档多证据联合问答)

- **涉及黄金证据分块**：
  - `8990eb8f-59b0-4c06-ad11-ae20f4d2adbd` (`high_availability_resilience.md`, Page 5, Chunk 4)
  - `3001dce5-d1d7-44ad-9070-c8cbafe8af81` (`compliance_and_audit.md`, Page 4, Chunk 3)
- **底库物理事实核对**：
  - `high_availability_resilience.md` Page 5 原文：“ModelHealthStore 核心职责如下：1. 指标汇聚：定时汇总各模型节点的实时请求量（QPS）、P95 响应延迟、错误率（4xx/5xx）以及当前的实时熔断状态；2. 状态判定：基于各节点的滑动窗口统计数据，向网关路由层提供实时的可用节点列表；3. 指标暴露：通过 /metrics 端点向 Prometheus 暴露模型维度的监控指标”。
  - `compliance_and_audit.md` Page 4 原文：“必须在后台全生命周期记录多步执行的 Trace 元数据，并将其冷备份存储保存期限设定为不少于 180 天”。
- **对比详情**：

| 字段 | Benchmark V1 (原草稿) | Benchmark V1.1 (正式修订) |
| :--- | :--- | :--- |
| **提问 (Query)** | 高可用架构中 ModelHealthStore 是如何判定模型节点下线的？同时合规审计规范对后台多步执行 Trace 元数据的冷备份保存期限是如何要求的？ | **高可用架构中 ModelHealthStore 负责收集和聚合哪些核心健康指标？同时合规审计规范对平台多步执行 Trace 元数据的冷备份保存期限是如何要求的？** |
| **参考答案 (Reference Answer)** | 根据高可用规范与合规规范：1. 节点下线判定：ModelHealthStore 维持心跳探测，默认探测间隔为 5 秒，连续 3 次探测失败判定模型节点下线并切除流量；2. Trace 元数据保存期限：合规审计规范要求在后台全生命周期记录多步执行 Trace 元数据，其冷备份存储保存期限不少于 180 天。 | **根据高可用规范与合规规范：1. ModelHealthStore 核心职责为实时汇总各模型节点的 QPS、P95 响应延迟、错误率（4xx/5xx）以及实时当前熔断状态，并通过 /metrics 端点向 Prometheus 暴露指标；2. 合规审计规范要求在后台全生命周期记录多步执行 Trace 元数据，其冷备份存储保存期限不少于 180 天。** |
| **核心事实 (Key Facts)** | 1. 探测间隔 5 秒，连续 3 次失败判定节点下线并切除流量<br>2. 后台全生命周期记录多步执行 Trace 元数据<br>3. 冷备份存储保存期限不少于 180 天 | 1. **ModelHealthStore 汇总模型节点的 QPS、P95 延迟、错误率及熔断状态**<br>2. **通过 /metrics 端点向 Prometheus 暴露监控指标**<br>3. 后台全生命周期记录多步执行 Trace 元数据<br>4. 冷备份存储保存期限不少于 180 天 |
| **设计说明 (Design Note)** | 跨文档多证据联合问答：结合高可用规范中的节点下线标准与合规规范中的后台Trace元数据保存周期。 | **跨文档多证据联合问答：结合高可用规范中 ModelHealthStore 汇聚的核心监控指标（QPS/P95/错误率/熔断状态）与合规规范中的后台 Trace 元数据冷备保存期限（>=180天）。** |

---

### 2. GEN-22 (Parametric: 异步连接池参数提取)

- **涉及黄金证据分块**：
  - `e514f5b4-36cb-4206-a051-fc0ef5cb03f4` (`microservice_gateway_spec.md`, Page 3, Chunk 2)
- **底库物理事实核对**：
  - `microservice_gateway_spec.md` Page 3 原文：“连接池核心参数配置如下：最小空闲连接数 min_idle 设为 10，保证基础的即时响应能力；最大并发连接数 max_connections 设定为 100，作为系统单节点的上限；连接获取超时时间 acquire_timeout_ms 设为 3000 毫秒。在业务高峰期，若连接池等待队列长度超过 200 时，系统将触发背压告警...”。全库均无 `keepalive_timeout` 和 `connection_timeout`。
- **对比详情**：

| 字段 | Benchmark V1 (原草稿) | Benchmark V1.1 (正式修订) |
| :--- | :--- | :--- |
| **提问 (Query)** | 微服务网关异步连接池中，keepalive_timeout 与 connection_timeout 的具体配置值分别是多少？ | **微服务网关异步连接池中，最小空闲连接数、最大并发连接数与连接获取超时时间分别是多少？** |
| **参考答案 (Reference Answer)** | 在异步连接池中，keepalive_timeout 设定为 60 秒（60s），连接获取超时时间 connection_timeout 设定为 2000 毫秒（2000ms）。 | **微服务网关异步连接池核心参数配置为：最小空闲连接数 min_idle 为 10；最大并发连接数 max_connections 设定为 100；连接获取超时时间 acquire_timeout_ms 设定为 3000 毫秒。** |
| **核心事实 (Key Facts)** | 1. keepalive_timeout = 60 秒<br>2. connection_timeout = 2000 毫秒 | 1. **最小空闲连接数 min_idle = 10**<br>2. **最大并发连接数 max_connections = 100**<br>3. **连接获取超时时间 acquire_timeout_ms = 3000 毫秒（3000ms）** |
| **设计说明 (Design Note)** | 异步连接池超时参数提取 | **单文档参数抽取：考察微服务网关异步连接池的 min_idle、max_connections 与 acquire_timeout_ms 三项核心配置数值。** |

---

### 3. GEN-24 (Parametric: 超时重试机制参数提取)

- **涉及黄金证据分块**：
  - `e55427ef-406d-4ba1-8db0-8f8936fa4d34` (`microservice_gateway_spec.md`, Page 2, Chunk 1)
- **底库物理事实核对**：
  - `microservice_gateway_spec.md` Page 2 原文：“对于幂等性的 GET 查询，系统在超时后自动重试 2 次，避让间隔 initial_interval_ms 为 500 毫秒；对于非幂等性的 POST 业务写入则严格禁止自动重试。”
- **对比详情**：

| 字段 | Benchmark V1 (原草稿) | Benchmark V1.1 (正式修订) |
| :--- | :--- | :--- |
| **提问 (Query)** | 网关重试机制中，三次重试的指数避让间隔时间分别是多少毫秒？以及最多重试几次？ | **微服务网关针对超时请求的重试机制是如何规定的？重试次数、避让间隔与请求类型限制分别是多少？** |
| **参考答案 (Reference Answer)** | 网关重试机制采用指数避让策略，三次重试避让间隔时间分别为 200 毫秒、400 毫秒、800 毫秒；全局最多重试 3 次。 | **根据微服务网关规范：1. 重试策略仅针对幂等的 GET 查询生效，在超时后自动重试 2 次；2. 避让间隔 initial_interval_ms 为 500 毫秒；3. 对于非幂等的 POST 业务写入则严格禁止自动重试。** |
| **核心事实 (Key Facts)** | 1. 200 毫秒、400 毫秒、800 毫秒<br>2. 最多重试 3 次 | 1. **仅针对幂等的 GET 查询在超时后自动重试 2 次**<br>2. **避让间隔 initial_interval_ms = 500 毫秒（500ms）**<br>3. **非幂等 POST 写入严格禁止自动重试** |
| **设计说明 (Design Note)** | 重试避让间隔参数提取 | **单文档参数抽取：考察网关重试机制的重试次数（2次）、避让间隔（500ms）以及针对 GET/POST 的幂等限制规则。** |

---

## 四、未修改题目清单验证 (27 道题)

通过自动化哈希与字段逐项对比，确认以下 27 道题在 V1 与 V1.1 间**完全一致（Zero Modification）**：
- **Single-fact (10 题)**：GEN-01, GEN-02, GEN-03, GEN-04, GEN-05, GEN-06, GEN-07, GEN-08, GEN-09, GEN-10
- **Multi-hop (7 题)**：GEN-11, GEN-12, GEN-13, GEN-14, GEN-15, GEN-16, GEN-18
- **Parametric (4 题)**：GEN-19, GEN-20, GEN-21, GEN-23
- **Unanswerable (6 题)**：GEN-25, GEN-26, GEN-27, GEN-28, GEN-29, GEN-30

---

## 五、修订效果推演与预期收益

1. **消除冤假错案**：
   - GEN-22 模型此前因如实回答底库参数 `min_idle=10, max_connections=100, acquire_timeout_ms=3000ms` 并指出无 keepalive 获 Faithfulness=5 分，但被判 Correctness=1；V1.1 修订后模型将获 Correctness=5 分满分。
   - GEN-24 模型此前严格按底库回答“2次重试与 500ms”被判 Correctness=1；V1.1 修订后将获 Correctness=5 分满分。
2. **多跳逻辑严密闭环**：
   - GEN-17 模型此前因底库无心跳周期判定而忠实说明缺失，被判 Correctness=3 分；V1.1 修订后考察 ModelHealthStore 实际汇聚指标与 Trace 180 天冷备周期，模型可实现 5/5 双满分。
3. **数据集权威度提升**：
   - 彻底消除了 Benchmark 标准答案中的人为想象与概念漂移，所有答案 100% 具备物理 Chunk 支撑，具备极高技术信服力。
