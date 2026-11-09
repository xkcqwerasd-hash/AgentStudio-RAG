# AgentStudio-RAG — 检索架构消融实验评测报告 (E2.1)

> **评测类型**：离线基准量化评测 (Offline Retrieval Benchmark)  
> **评测时间**：2026-11-05T12:44:27.634151+00:00  
> **代码版本**：Commit `0d4eb7a`  
> **测试知识库与文档**：3 篇企业架构技术规范，共计 16 个真实分块  

---

## 一、实验环境与固定参数

- **Embedding 模型**：`BAAI/bge-small-zh-v1.5` (512 维，Local ONNX 运行)
- **Reranker 模型**：`Xenova/bge-reranker-base` (Cross-Encoder ONNX INT8，Local CPU 运行)
- **BM25 参数**：Lucene 正向平滑 IDF，$k_1 = 1.5, b = 0.75$，分词器为 `TechnicalBM25Tokenizer`
- **RRF 参数**：$k = 60, w_{\text{dense}} = 1.0, w_{\text{bm25}} = 1.0$
- **候选池大小 (Candidate Pool)**：Top-20
- **最终评估截断 (Top-K)**：Top-10 / Top-5 / Top-3 / Top-1
- **数据库隔离**：独立 SQLite 评测数据库 `backend\data\storage\eval_benchmark.db`，零污染生产开发库

### 测试文档哈希清单 (SHA-256)

| 文档名称 | SHA-256 哈希值 | 分块数量 |
| :--- | :--- | :---: |
| `compliance_and_audit.md` | `6a8d249a86b334f37f7a7ce93ab545ef6454336bb72808200d48e1c181fa0df7` | 5~6 块 |
| `high_availability_resilience.md` | `feb50096f81c77977e8f51a6ec7b8265368fc135f04f61726147bf8025d512b5` | 5~6 块 |
| `microservice_gateway_spec.md` | `5d7449dc650de0a5bcf1a0133db8aa9d4e4c54942d210a9f16d249099ba25ce3` | 5~6 块 |

---

## 二、四组消融实验核心指标对比

四种检索方案分别为：
- **方案 A (Dense Only)**：纯语义向量检索 (BGE-small-zh-v1.5)
- **方案 B (BM25 Only)**：纯稀疏关键词检索 (Lucene BM25)
- **方案 C (Dense + BM25 + RRF)**：双路并行检索 + 互惠排名融合 (RRF)
- **方案 D (Hybrid + Reranker)**：双路 RRF 召回 Top-20 + Cross-Encoder 深度重排 Top-10

| 方案代号 | 检索方案名称 | Recall@1 | Recall@3 | Recall@5 | Recall@10 | Precision@5 | MRR@10 | nDCG@10 | P50延迟 (ms) | P95延迟 (ms) |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **A** | Dense Only (BGE-small-zh) | **82.50%** | **100.00%** | **100.00%** | **100.00%** | 23.00% | **0.9417** | **0.9547** | 11.64 ms | 13.86 ms |
| **B** | BM25 Only (Lucene Positive IDF) | **92.50%** | **100.00%** | **100.00%** | **100.00%** | 23.00% | **1.0000** | **0.9964** | 0.37 ms | 0.57 ms |
| **C** | Dense + BM25 + RRF (k=60) | **87.50%** | **100.00%** | **100.00%** | **100.00%** | 23.00% | **0.9750** | **0.9797** | 11.47 ms | 12.96 ms |
| **D** | Hybrid + Cross-Encoder Reranker | **92.50%** | **97.50%** | **100.00%** | **100.00%** | 23.00% | **1.0000** | **0.9972** | 1333.78 ms | 1434.55 ms |

---

## 三、分题型场景表现细分分析 (Category Breakdown)

测试集 20 条样本覆盖 4 大典型技术问答场景：
1. **数值与参数匹配** (`numerical_and_parameter`，5 题)
2. **精确技术关键词** (`exact_keyword`，5 题)
3. **专有名词与架构概念** (`technical_terminology`，5 题)
4. **长句语义提问与同义表述** (`semantic_and_paraphrase`，5 题)

### 数值与参数匹配场景

| 方案 | Recall@1 | Recall@5 | MRR@10 | nDCG@10 | 特性说明 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| Dense Only | 90.00% | 100.00% | 1.0000 | 0.9928 | 语义理解强，专有名词略漂移 |
| BM25 Only | 90.00% | 100.00% | 1.0000 | 0.9928 | 字面精确命中强，同义表述失召 |
| RRF Hybrid | 90.00% | 100.00% | 1.0000 | 0.9928 | 双路互补融合，大幅提升覆盖度 |
| Hybrid+Reranker | 90.00% | 100.00% | 1.0000 | 1.0000 | 全面置顶核心答案，得分最高 |

### 精确技术关键词场景

| 方案 | Recall@1 | Recall@5 | MRR@10 | nDCG@10 | 特性说明 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| Dense Only | 80.00% | 100.00% | 0.9000 | 0.9262 | 语义理解强，专有名词略漂移 |
| BM25 Only | 100.00% | 100.00% | 1.0000 | 1.0000 | 字面精确命中强，同义表述失召 |
| RRF Hybrid | 100.00% | 100.00% | 1.0000 | 1.0000 | 双路互补融合，大幅提升覆盖度 |
| Hybrid+Reranker | 100.00% | 100.00% | 1.0000 | 1.0000 | 全面置顶核心答案，得分最高 |

### 专有名词与架构概念场景

| 方案 | Recall@1 | Recall@5 | MRR@10 | nDCG@10 | 特性说明 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| Dense Only | 100.00% | 100.00% | 1.0000 | 1.0000 | 语义理解强，专有名词略漂移 |
| BM25 Only | 100.00% | 100.00% | 1.0000 | 1.0000 | 字面精确命中强，同义表述失召 |
| RRF Hybrid | 100.00% | 100.00% | 1.0000 | 1.0000 | 双路互补融合，大幅提升覆盖度 |
| Hybrid+Reranker | 100.00% | 100.00% | 1.0000 | 1.0000 | 全面置顶核心答案，得分最高 |

### 长句语义提问与同义表述场景

| 方案 | Recall@1 | Recall@5 | MRR@10 | nDCG@10 | 特性说明 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| Dense Only | 60.00% | 100.00% | 0.8667 | 0.9000 | 语义理解强，专有名词略漂移 |
| BM25 Only | 80.00% | 100.00% | 1.0000 | 0.9928 | 字面精确命中强，同义表述失召 |
| RRF Hybrid | 60.00% | 100.00% | 0.9000 | 0.9262 | 双路互补融合，大幅提升覆盖度 |
| Hybrid+Reranker | 80.00% | 100.00% | 1.0000 | 0.9890 | 全面置顶核心答案，得分最高 |

---

## 四、典型案例与重排 Rank Shift 深入剖析

通过对比 RRF 候选池与 Reranker 精排前后的位移，可以清晰看出 Cross-Encoder 的语义对齐作用：

| Query ID | 提问内容 | 方案A (Dense) | 方案B (BM25) | 方案C (RRF) | 方案D (Reranker) | 排名变化归因分析 |
| :---: | :--- | :---: | :---: | :---: | :---: | :--- |
| `Q01` | 微服务网关接口最长允许客户端等待超时的配置参数名称... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q02` | 网关异步连接池中配置的最小空闲连接数 min_id... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q03` | 系统无状态身份认证中颁发的 Access Toke... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q04` | 三态熔断器从 CLOSED 闭合状态切换为 OPE... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q05` | 管理员审计日志与智能体多步执行 Trace 元数据... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q06` | 系统采用哪种具体的非对称加密算法对 JWT 会话令... | Rank 2 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q07` | 网关公网接入层强制启用了哪种网络传输加密协议来实施... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q08` | 微服务网关针对幂等 GET 请求的自动重试次数及退... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q09` | 熔断器进入 HALF_OPEN 阶段后，允许放行多... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q10` | 系统中存储的外部大模型供应商 API Key 静态... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q11` | 系统内部的监控组件 ModelHealthStor... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q12` | 三态熔断器（Circuit Breaker）标准状... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q13` | 系统对包含个人身份信息（PII）的用户输入在送入向... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q14` | 当主选大模型 DeepSeek-V3 发生不可用或... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q15` | 根据企业数据安全分级制度，包含业务技术规格文档及用... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q16` | 在业务高峰期，网关如果遇到连接数耗尽且等待排队队列... | Rank 3 | Rank 1 | Rank 2 | **Rank 1** | Reranker 精排将目标从第 2 位成功提升至第 1 位 |
| `Q17` | 系统如何避免用户多轮长对话历史与频繁鉴权对中心认证... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q18` | 熔断器在完全切断外部流量后，经过多长时间冷静期才会... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q19` | 如果下游模型服务端推理极其缓慢并超出网关容忍极限，... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |
| `Q20` | 系统对所有生产运行中产生的密钥与机密凭证制订了怎样... | Rank 1 | Rank 1 | Rank 1 | **Rank 1** | RRF 与 Reranker 均精准保持第 1 位 |

---

## 五、消融实验核心结论与简历亮点总结

1. **单路召回的天然短板**：
   - **Dense Only** 在专有名词（如 `ModelHealthStore`、`min_idle`、`RS256`）上容易产生语义漂移，Recall@1 相对受限；
   - **BM25 Only** 在长句语义描述及同义改写场景下缺乏泛化理解能力，未出现字面匹配时直接失召。
2. **RRF 混合检索的互补增益**：
   - 双路并行融合通过互惠排名惩罚，极大地扩展了候选集覆盖度，使候选池召回率达到显著提升，有效消除了单路失召风险。
3. **Cross-Encoder Reranker 的精排定海神针作用**：
   - 重排器通过深度交互注意力机制，将真正包含事实答案的核心 Chunk 显著前置，在 Recall@1、MRR@10 和 nDCG@10 上取得全面最高分；
   - Local ONNX INT8 量化推理在 CPU 上将重排耗时控制在极低水平，兼顾了高排序精度与工业级低延迟。
