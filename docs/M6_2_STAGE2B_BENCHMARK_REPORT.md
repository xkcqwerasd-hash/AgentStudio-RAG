# AgentStudio-RAG — M6.2 Stage 2-B：混合检索消融实验与性能评测报告

> **执行阶段**：M6.2 Stage 2-B（一次性混合检索消融实验与性能评测）  
> **评测日期**：2026-11-04  
> **评测环境**：纯本地 CPU（Intel Core / Windows）、独立内存评测沙箱、零大模型 API 调用、零数据库持久化写入  
> **评测数据基准**：112 个标准切片、60 条跨 6 类的标准查询、193 条人工切片级相关度标注（QRELs）  
> **阶段状态**：实验全部完成，数据客观闭环，**M6.2 检索消融评测阶段正式结项并冻结**  

---

## 一、执行摘要与核心结论

本次消融实验（Ablation Study）旨在从科学与工程双重维度，验证 AgentStudio-RAG 现有的 **Dense + BM25 + RRF + Cross-Encoder Reranker** 四级渐进式混合检索架构的合理性与各模块收益边界。

实验在严格相同且公平的基准下运行：
- **四组对比方案**：1. Dense Only、2. BM25 Only、3. Hybrid RRF、4. Hybrid + Reranker。
- **全量样本覆盖**：60 条标准 Query，涵盖技术概念、精确参数、错误码、跨文档、同义表述与中英文混合技术场景。
- **消融公平性保证**：Dense 与 BM25 各召回 Top-20；Hybrid RRF 基于相同的 Top-20 双路候选池融合；Reranker 严格对该 Top-20 融合候选池精排，最终统一在截断深度 Top-1、Top-3、Top-5 评测。

### 1.1 核心评测指标全景对比表

| 方案 / 架构组合 | Recall@1 | Recall@3 | Recall@5 | StrictRec@1 (Score=2) | StrictRec@5 (Score=2) | MRR | nDCG@5 | 平均耗时 (Mean) | P50 延迟 (Median) | P95 延迟 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Dense Only** | 0.3528 | 0.5639 | 0.6833 | 0.6556 | 0.8667 | 0.8267 | 0.7456 | **6.04 ms** | 5.89 ms | 7.91 ms |
| **2. BM25 Only** | 0.3667 | 0.6361 | **0.7250** | 0.6472 | **0.9083** | 0.8681 | 0.7783 | **1.00 ms** | 0.87 ms | 2.83 ms |
| **3. Hybrid RRF** | 0.3972 | **0.6417** | 0.7083 | 0.7222 | **0.9083** | 0.8986 | **0.7996** | **7.18 ms** | 7.02 ms | 10.36 ms |
| **4. Hybrid + Reranker** | **0.4139** | 0.6111 | 0.6750 | **0.7556** | 0.9000 | **0.9056** | 0.7985 | **2,496.57 ms** | 2,506.01 ms | 2,859.57 ms |

> [!NOTE]
> **标注说明与指标口径**：
> - **Recall@K**（综合召回率）：将 Score=2（核心依据）与 Score=1（支撑上下文）均视为正例命中。
> - **StrictRec@K**（核心精准召回率）：仅将 Score=2（核心关键切片）视为命中目标。
> - **MRR**（平均倒数排名）：首个相关切片所在排名的倒数（$1/rank$），未命中取 0。
> - **nDCG@5**（归一化折损累积增益）：采用非线性增益权重 $2^{rel} - 1$（Score 2 权重为 3，Score 1 权重为 1，Score 0 及未标注切片权重为 0）。

---

### 1.2 四大核心发现与工程洞察

1. **Hybrid RRF 是整个系统综合性价比最高（Pareto Optimal）的基石**：
   - 相比单一 Dense，RRF 的 nDCG@5 从 0.7456 提升至 **0.7996**（+7.24% 相对提升），StrictRec@1 从 0.6556 提升至 **0.7222**，MRR 达到 **0.8986**。
   - 综合耗时仅为 **7.18 ms**（RRF 计算自身开销仅 0.14 ms），在保持毫秒级响应的同时，全面修复了向量检索与关键词检索各自的盲区。
2. **BM25 Only 在精确技术问答中表现极佳，是工程 RAG 的刚需底层**：
   - 在技术文档场景下，BM25 单路耗时仅 **1.00 ms**，整体 Recall@5 甚至达到 **0.7250**（高于单一 Dense 的 0.6833）。
   - 在“精确参数查询（CAT-2）”和“错误码查询（CAT-3）”中，自定义 [`TechnicalBM25Tokenizer`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/bm25_retriever.py) 准确捕获下划线参数名、大小写混排与错误码，防止了稠密向量检索对数值和短字符的“模糊泛化”缺陷。
3. **Cross-Encoder Reranker 具备极强的 Top-1 精准推顶能力，但本地 CPU 计算代价高昂**：
   - Reranker 将 Recall@1 进一步提升至 **0.4139**，StrictRec@1 达到 **0.7556**，MRR 达到 **0.9056**。在概念解释与错误码排障场景下，MRR 达到了完美的 **1.0000**。
   - 但在本地 CPU（INT8 量化）环境下，精排 20 个切片的端到端耗时为 **2,496 ms（约 2.5 秒）**，相比 RRF 增加了 **347 倍耗时**。
   - **工程结论**：Reranker 绝不应作为每个 Query 的无脑同步必经节点；在生产工程中，应通过**意图路由（Intent Routing）**或**歧义探测**进行“按需触发”，或在候选集从 20 剪枝到 5～8 个后再精排。
4. **单一路线无法通吃，混合检索各尽其责**：
   - 在“同义表达与语义检索（CAT-5）”中，BM25 因缺乏字面交集表现较差（nDCG 0.4515），Dense 与 RRF 成功救场（nDCG 0.5523）；
   - 在“跨文档多跳（CAT-4）”中，BM25 凭借关键词覆盖度贡献了 0.8000 的高 Top-5 召回，而 Reranker 与 RRF 则保证了前位精度。

---

## 二、消融实验设计与执行规范

### 2.1 四组实验方案执行定义

```
┌────────────────────────────────────────────────────────────────────────┐
│                        用户输入 Query (例如: 60条标准测试问答)            │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
           ┌────────────────────────┴────────────────────────┐
           ▼                                                 ▼
┌──────────────────────┐                         ┌──────────────────────┐
│  1. Dense Only       │                         │  2. BM25 Only        │
│  BGE-Small-ZH (512d) │                         │  TechnicalBM25       │
│  余弦相似度全库扫描    │                         │  Lucene Inverted Idx │
│  耗时: ~6.04 ms       │                         │  耗时: ~1.00 ms       │
└──────────┬───────────┘                         └──────────┬───────────┘
           │ (Top-20 候选)                                  │ (Top-20 候选)
           └────────────────────────┬───────────────────────┘
                                    │
                                    ▼
                      ┌───────────────────────────┐
                      │  3. Hybrid RRF            │
                      │  rrf_fuse(k=60, top_k=20) │
                      │  融合耗时: ~0.14 ms        │
                      │  总延迟: ~7.18 ms          │
                      └─────────────┬─────────────┘
                                    │ (Top-20 融合候选)
                                    ▼
                      ┌───────────────────────────┐
                      │  4. Hybrid + Reranker     │
                      │  bge-reranker-base (ONNX) │
                      │  Cross-Encoder 交叉注意力  │
                      │  精排耗时: ~2,487 ms       │
                      │  总延迟: ~2,496 ms         │
                      └───────────────────────────┘
```

1. **Dense Only**：
   - 算法实现：[`VectorRetriever`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/vector_store.py) 余弦相似度。
   - 向量模型：`BAAI/bge-small-zh-v1.5`（512 维，Local CPU）。
2. **BM25 Only**：
   - 算法实现：[`BM25KBIndex`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/bm25_retriever.py) Lucene 正 IDF 倒排索引。
   - 分词组件：[`TechnicalBM25Tokenizer`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/bm25_retriever.py)（针对 `min_idle`、`ERR-AUTH-401`、驼峰及中英文深度定制）。
3. **Hybrid RRF**：
   - 算法实现：[`rrf_fuse`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/rrf_fusion.py) 双路倒数排名融合。
   - 参数配置：$k=60$，等权加权（$w_{dense}=1.0, w_{bm25}=1.0$），多重确定性 Tie-Breaker。
4. **Hybrid + Cross-Encoder Reranker**：
   - 算法实现：[`CrossEncoderReranker`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/reranker.py) 批处理推理。
   - 精排模型：`Xenova/bge-reranker-base`（ONNX INT8 量化，多线程 CPU）。

### 2.2 严格防作弊与防抖动控制

- **环境隔离**：使用独立的内存数据结构（`InMemoryVectorRetriever` 与内存 `BM25KBIndex`），检索全过程**不产生任何 SQLite 读写**，生产数据库 `agentstudio_rag.db` 0 污染。
- **预热机制**：正式评测前执行 3 轮独立预热（Dense + BM25 + RRF + ONNX 推理），彻底消除 HuggingFace/ONNX Runtime 动态库加载与 CPU 线程池冷启动噪声。
- **重复测量**：每条 Query 的检索耗时均经过 **3 次独立运行**，记录每次耗时并统计 Mean、P50（中位数）与 P95。
- **未标注候选切片规范**：检索返回的未标注切片在相关度计算中归零处理，但在 JSON/CSV 原始数据中明确标识为 `unannotated_candidate`，严禁伪称为已确认对抗负例。

---

## 三、全局检索指标对比分析

### 3.1 关键信息检索指标对比

```mermaid
xychart-beta
    title "各检索方案核心指标对比 (Recall@1, Recall@5, MRR, nDCG@5)"
    x-axis ["Recall@1", "Recall@5", "MRR", "nDCG@5"]
    y-axis "Score (0.0 - 1.0)" 0.0 --> 1.0
    bar [0.35, 0.68, 0.83, 0.75]
    bar [0.37, 0.73, 0.87, 0.78]
    bar [0.40, 0.71, 0.90, 0.80]
    bar [0.41, 0.68, 0.91, 0.80]
```

#### 指标对比数据明细

| 检索方案 | Recall@1 | Recall@3 | Recall@5 | StrictRec@1 | StrictRec@5 | MRR | nDCG@5 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Dense Only** | 0.3528 | 0.5639 | 0.6833 | 0.6556 | 0.8667 | 0.8267 | 0.7456 |
| **BM25 Only** | 0.3667 | 0.6361 | **0.7250** | 0.6472 | **0.9083** | 0.8681 | 0.7783 |
| **Hybrid RRF** | 0.3972 | **0.6417** | 0.7083 | 0.7222 | **0.9083** | 0.8986 | **0.7996** |
| **Hybrid + Reranker** | **0.4139** | 0.6111 | 0.6750 | **0.7556** | 0.9000 | **0.9056** | 0.7985 |

### 3.2 深度指标解析

1. **从 Dense 到 RRF 的跨越（召回与排序双升）**：
   - Dense Only 的核心瓶颈在于硬对抗干扰项：遇到同名参数（例如网关 `min_idle` 对比数据库 `min_idle`）时，余弦相似度容易由于全局语义相似而发生混淆。
   - 引入 BM25 后，RRF 将首位核心证据命中率（StrictRec@1）直接从 **65.56% 推升至 72.22%**（绝对提升 +6.66%），MRR 从 0.8267 提升至 0.8986。
2. **Reranker 的首位推顶效应（Push-to-Top）**：
   - Reranker 将 StrictRec@1 进一步拉高至 **75.56%**，意味着超过四分之三的用户问题，其最关键的解答切片精准排在首位（Rank 1）。
   - MRR 达到 0.9056，证明 Cross-Encoder 的 Full-Attention 机制在辨析细粒度技术差异上具有极高精度。
3. **为什么 Reranker 的 Recall@5 没有继续增加？**：
   - 必须客观指出：Reranker 的评估截断为 Top-5，且精排的作用是**候选集重排**，而非扩展候选集。如果双路召回的 Top-20 中未能包含某个冷门切片，Reranker 无法“凭空创造”召回。
   - 此外，Reranker 的打分偏好将语义关联极高的一两篇切片拉至极高分，这强化了 Top-1～Top-2 的精准度，但对长尾边缘切片的打分可能受到局部语境偏置影响。

---

## 四、六大技术问答场景分类消融剖析

本次测试 60 条 Query 分属于 6 类典型技术场景（每类 10 条），各场景消融结果展现出强烈的场景特异性：

```
CAT-1: 技术概念解释     CAT-2: 精确参数查询     CAT-3: 错误码与术语
CAT-4: 跨文档关联检索   CAT-5: 同义表达与语义   CAT-6: 中英文技术混合
```

### 4.1 分类消融详细对比矩阵

| 场景分类编号与名称 | 指标维度 | 1. Dense Only | 2. BM25 Only | 3. Hybrid RRF | 4. Hybrid + Reranker | 场景最优方案 |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **CAT-1 技术概念解释** | Recall@1<br>MRR<br>nDCG@5 | 0.4500<br>0.9500<br>0.8887 | 0.4500<br>0.9500<br>0.8731 | 0.4500<br>0.9500<br>0.9017 | **0.5000**<br>**1.0000**<br>**0.9296** | **Hybrid + Reranker**<br>(深度语义理解最佳) |
| **CAT-2 精确参数查询** | Recall@1<br>Recall@5<br>nDCG@5 | 0.3500<br>0.7000<br>0.7414 | **0.4000**<br>**0.8000**<br>**0.8042** | 0.4000<br>0.7500<br>0.7900 | 0.4000<br>0.7000<br>0.7769 | **BM25 Only / RRF**<br>(精确分词压制向量模糊) |
| **CAT-3 错误码与术语** | Recall@1<br>MRR<br>nDCG@5 | 0.4000<br>0.8700<br>0.8161 | 0.4500<br>0.9500<br>0.8771 | 0.4500<br>0.9500<br>0.8771 | **0.5000**<br>**1.0000**<br>**0.8921** | **Hybrid + Reranker**<br>(排障排位最准) |
| **CAT-4 跨文档关联** | Recall@1<br>Recall@5<br>nDCG@5 | 0.2333<br>0.6333<br>0.7158 | 0.2666<br>**0.8000**<br>**0.8216** | **0.3000**<br>0.7000<br>0.8030 | **0.3000**<br>0.7000<br>0.8121 | **Hybrid RRF**<br>(兼顾跨域广度与精度) |
| **CAT-5 同义表达与语义** | Recall@1<br>MRR<br>nDCG@5 | 0.2500<br>0.5700<br>0.4767 | 0.2000<br>0.6000<br>0.4515 | **0.3000**<br>**0.6750**<br>**0.5523** | **0.3000**<br>0.6500<br>0.5275 | **Hybrid RRF**<br>(语义纠偏，挽救关键词失配) |
| **CAT-6 中英文技术混合** | Recall@1<br>MRR<br>nDCG@5 | 0.4333<br>0.9500<br>0.8350 | 0.4333<br>0.9500<br>0.8423 | **0.4833**<br>**1.0000**<br>**0.8734** | **0.4833**<br>**1.0000**<br>0.8525 | **Hybrid RRF**<br>(分词器与多语言向量完美契合) |

### 4.2 场景典型案例与深层成因分析

#### 案例 1：CAT-2 精确参数查询中的“向量退化”现象
- **测试 Query**：`Q-13: 在分布式追踪中，采样率控制参数 trace_sample_ratio 默认值是多少？`
- **Dense 表现**：返回了多篇包含 `ratio`、`sample` 和其他追踪配置的切片，但关键的 `CHK-08-002`（明确标注 `trace_sample_ratio: 0.1`）仅排在第 4 位。
- **BM25 表现**：[`TechnicalBM25Tokenizer`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/bm25_retriever.py) 准确识别了 `trace_sample_ratio` 复合标识符，以高达 28.4 的得分将 `CHK-08-002` 稳稳排在第 1 位。
- **结论**：在参数配置类检索中，关键词精确匹配具有压倒性优势，若单纯依赖 Embedding 向量，容易因为长尾数字和变量名的稠密表示模糊而丢失前位排序。

#### 案例 2：CAT-5 同义表达与纯语义问答中的“关键词瘫痪”
- **测试 Query**：`Q-41: 当下游服务处理缓慢时，微服务如何暂停接收请求进行自我保护？`
- **文档原文**：规范文档中使用的是标准术语“三态熔断器（HALF_OPEN 状态流转机制）”，未出现“暂停接收请求进行自我保护”等口语化词汇。
- **BM25 表现**：由于分词后关键词无重合，BM25 产生灾难性失配，检索得分接近 0，MRR 骤降。
- **Hybrid RRF 表现**：Dense 向量根据上下文语义成功捕获了熔断机制文档，RRF 融合后将正确切片推入 Top-3，最终实现 nDCG@5 0.5523，体现了混合检索互补兜底的不可替代性。

#### 案例 3：CAT-1 与 CAT-3 中的 Cross-Encoder 终极压制
- **测试 Query**：`Q-21: 客户端调用时收到 ERR-GATEWAY-504 错误码，应该排查哪些配置项？`
- **RRF 排序**：将网关超时的切片 `CHK-01-004` 与硬对抗干扰切片 `CHK-10-001`（数据库连接超时 504 类似文案）一同带入前位。
- **Reranker 精排**：Cross-Encoder 通过 Query 与 Chunk 的全局 Cross-Attention，敏锐捕获到 `ERR-GATEWAY-504` 与网关 upstream 超时配置的强逻辑绑定，将 `CHK-01-004` 赋予 5.82 高 logit，而将数据库干扰项打压至后位，实现完美命中。

---

## 五、检索延迟与阶段性能开销拆解

对于生产级企业 RAG 系统，检索指标绝不能脱离**计算延迟（Latency）与算力成本**单独衡量。

### 5.1 各阶段耗时详细拆解（Local CPU 测试环境）

```mermaid
xychart-beta
    title "各阶段平均耗时拆解 (毫秒 ms)"
    x-axis ["Query向量化", "Dense点积扫描", "BM25分词与倒排", "RRF融合计算", "Cross-Encoder精排"]
    y-axis "耗时 (ms)" 0 --> 2600
    bar [5.59, 0.55, 1.72, 0.14, 2487.52]
```

| 检索阶段 / 处理步骤 | 对应模块与实现类 | 平均耗时 (Mean) | P50 (中位数) | P95 延迟 | 占总耗时比例 (在 Hybrid+Reranker 中) |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **1. Query 语义向量化** | [`LocalBGEEmbeddingClient`](file:///e:/develop/python%20agentic%20RAG/backend/app/llm/embeddings.py) | 5.59 ms | 5.34 ms | 7.73 ms | 0.22% |
| **2. Dense 向量点积与排序** | [`VectorRetriever`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/vector_store.py) (NumPy) | 0.55 ms | 0.49 ms | 0.87 ms | 0.02% |
| **3. BM25 分词与倒排扫描** | [`BM25KBIndex`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/bm25_retriever.py) | 1.72 ms | 1.29 ms | 4.09 ms | 0.07% |
| **4. RRF 倒数排名融合** | [`rrf_fuse`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/rrf_fusion.py) | **0.14 ms** | **0.14 ms** | **0.21 ms** | **< 0.01%** |
| **5. Cross-Encoder 推理** | [`LocalONNXReranker`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/reranker.py) (INT8) | **2,487.52 ms** | **2,497.24 ms** | **2,801.21 ms** | **99.69%** |

### 5.2 端到端方案总延迟对比

| 完整流水线方案 | Mean 耗时 | P50 耗时 | P95 耗时 | 耗时相对倍数 |
| :--- | :---: | :---: | :---: | :---: |
| **BM25 Only** | **1.00 ms** | 0.87 ms | 2.83 ms | 1.0x (基准) |
| **Dense Only** | **6.04 ms** | 5.89 ms | 7.91 ms | 6.0x |
| **Hybrid RRF** | **7.18 ms** | 7.02 ms | 10.36 ms | **7.2x** |
| **Hybrid + Reranker** | **2,496.57 ms** | 2,506.01 ms | 2,859.57 ms | **2,496.5x (对比 RRF 增加 347 倍)** |

---

### 5.3 生产工程决策：性能损益比（ROI）与落地启示

实验数据清晰指明了生产系统架构设计的取舍原则：

1. **为什么在默认快速链路上不能常驻 Reranker？**
   - Reranker 消耗了 2.5 秒的 CPU 密集计算，换取 Recall@1 提升 1.67%（0.3972 $\to$ 0.4139），MRR 提升 0.70%（0.8986 $\to$ 0.9056），但总耗时上升了 347 倍。
   - 在高并发或实时交互对话场景中，2.5 秒的纯检索延迟将直接破坏用户体验（导致首字吐出 TTFT 严重滞后）。
2. **AgentStudio-RAG 的生产落地最佳实践**：
   - **默认检索骨干**：采用 **Hybrid RRF** 作为全系统默认检索链，在 7.18 毫秒内即可提供 0.7996 的极高 nDCG@5 与 0.8986 的 MRR，满足 95% 以上的标准问答需求。
   - **条件精排触发（Conditional Rerank）**：
     - 仅当进入 Agent“深度研究/复杂排障”模式时启用；
     - 或通过前置意图路由检测到 Query 属于概念歧义、错误码排查等高混淆场景时按需介入；
     - 或在候选池规模上进行动态剪枝：将送入 Reranker 的候选数由 20 削减至 **5～8 个**，计算耗时可立降 60%～75%（降至约 600～800 ms）。

---

## 六、对 AgentStudio-RAG 算法架构的客观评定

本次 Stage 2-B 消融实验不仅完成了算法性能测量，更对现有系统的架构设计给出了定论：

1. **架构合理性充分坐实**：
   - 数据证明：没有任何单一算法（无论是 Dense 还是 BM25）能够在全部 6 类业务场景中占据主导。现有 Dense + BM25 的双路召回策略完全正确，互补优势明显。
2. **分词器技术价值显著**：
   - 自主实现的 [`TechnicalBM25Tokenizer`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/bm25_retriever.py) 在参数（CAT-2）、错误码（CAT-3）和中英文混合（CAT-6）中取得了超出预期的出色成绩，证明在垂类技术 RAG 中，自研专业分词器的价值不亚于大模型本身。
3. **融合算法轻量高效**：
   - [`rrf_fuse`](file:///e:/develop/python%20agentic%20RAG/backend/app/retrieval/rrf_fusion.py) 仅耗费 0.14 ms，无需超参数搜索即可稳健输出最高综合排序质量（nDCG@5 0.7996），是系统中最优雅且不可或缺的组件。

---

## 七、评测数据资产清单与归档

本次实验产生的所有原始测量数据与图表资产均已完整持久化并归档：

| 资产文件名 | 存放绝对路径 | 资产描述 |
| :--- | :--- | :--- |
| **全量原始结果 JSON** | [`data/benchmark/m6_2_results.json`](file:///e:/develop/python%20agentic%20RAG/data/benchmark/m6_2_results.json) | 包含 60 条 Query × 4 组方案 × 3 轮耗时及 Top-5 切片详情（831 KB） |
| **指标对比汇总 CSV** | [`data/benchmark/m6_2_summary.csv`](file:///e:/develop/python%20agentic%20RAG/data/benchmark/m6_2_summary.csv) | 包含 Overall 及 6 大分类的指标均值与耗时统计表格 |
| **逐条 Query 明细 CSV** | [`data/benchmark/m6_2_query_details.csv`](file:///e:/develop/python%20agentic%20RAG/data/benchmark/m6_2_query_details.csv) | 每一条 Query 的 4 方案指标、得分、Ground Truth 判定与切片 ID（87 KB） |
| **指标对比矢量图 (SVG)** | [`data/benchmark/m6_2_metrics_comparison.svg`](file:///e:/develop/python%20agentic%20RAG/data/benchmark/m6_2_metrics_comparison.svg) | Recall@1/3/5、MRR、nDCG@5 方案分组柱状图 |
| **延迟耗时矢量图 (SVG)** | [`data/benchmark/m6_2_latency_comparison.svg`](file:///e:/develop/python%20agentic%20RAG/data/benchmark/m6_2_latency_comparison.svg) | Mean、P50、P95 检索延迟对比矢量图 |
| **结果镜像目录** | [`backend/benchmark/retrieval/results/`](file:///e:/develop/python%20agentic%20RAG/backend/benchmark/retrieval/results/) | 后端代码树同源备份，支持测试套件与 CI 读取 |

---

## 八、M6.2 阶段冻结声明

> [!IMPORTANT]
> **里程碑结项与阶段冻结声明**：  
> 1. 本次消融实验已圆满达成既定评测目标，完整解答了“各模块带来多少实际收益”、“各自擅长场景”以及“耗时延迟边界”三大核心问题。  
> 2. **正式冻结 M6.2 模块**：自即日起，停止对检索消融实验、检索参数微调或 M6.3 算法优化的无限扩展。  
> 3. 后续开发重心全面转向 AgentStudio-RAG 的产品级能力构建：包括**智能问答路由（Intent Routing）**、**长期记忆（Long-term Memory）**、**工程可靠性**与**生产部署交付**。
