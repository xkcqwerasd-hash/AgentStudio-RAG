# AgentStudio-RAG — M3.3 审计加固与最终验收交付报告
**Document Identifier**: `docs/M3_3_AUDIT_REMEDIATION.md`  
**Execution Date**: 2026-11-02  
**Remediation Scope**: Fix-1 ～ Fix-4 (基于 `docs/M3_3_FINAL_AUDIT.md` 授权加固)  
**Acceptance Status**: **Officially Accepted & Frozen (正式验收通过并冻结)**

---

## 目录
1. [加固背景与范围限制](#一加固背景与范围限制)
2. [四项修复实施前后对比明细](#二四项修复实施前后对比明细)
3. [修改文件与具体函数清单](#三修改文件与具体函数清单)
4. [Benchmark 真实测量结果对比](#四benchmark-真实测量结果对比)
5. [自动化测试与真实 ONNX 验证结果](#五自动化测试与真实-onnx-验证结果)
6. [尚未解决的问题与已知工程风险](#六尚未解决的问题与已知工程风险)
7. [M1～M3 最终验收与功能冻结声明](#七m1m3-最终验收与功能冻结声明)

---

## 一、加固背景与范围限制

在完成《M3.3 Cross-Encoder Reranker 最终工程核查》（`docs/M3_3_FINAL_AUDIT.md`）后，针对审计发现的 4 项 WARNING 瑕疵，本轮实施了**最小必要工程加固（Audit Remediation）**。

### 严格限制的工作边界遵循确认：
1. **零架构重构**：未改变现有的 Dense+BM25 -> RRF -> Cross-Encoder -> Token Budget -> LLM -> Citation 核心链路。
2. **API 契约零破坏**：保留了现有数据模型的所有字段名称（如 `similarity_score`、`reranker_score`、`reranker_raw_score`）。
3. **零外部费用与依赖**：未调用消耗资费的外部 API，未引入新 PyPI 包，未重复下载 HuggingFace 模型。
4. **功能范围冻结**：坚决未提前开发 M4、LangGraph、MCP 或多 Agent。

---

## 二、四项修复实施前后对比明细

### Fix-1：修正 Sigmoid 分数的概念表述与展示

- **修复前问题**：后端注释、前端调试台和技术文档中广泛使用“Sigmoid 概率校准”、“校准置信度”等术语，容易误导下游使用者认为模型输出具备统计学后验置信度。
- **修复措施**：
  1. 将技术文档和后端代码注释中的“概率校准”全面纠正为“相关度归一化得分（Sigmoid Score）”；
  2. 明确说明 Sigmoid 仅为将连续 Logit 单调压缩到 $[0, 1]$ 闭区间的数学映射；
  3. 前端 `RetrievalDebugPage.tsx` 中的展示文案由 `Cross-Encoder 精排得分: 98.4%` 调整为 `Cross-Encoder 相关度归一化得分 (Sigmoid): 0.9841 (Logit: 4.125)`，去除百分比形式的概率伪装。

### Fix-2：修正 Reranker 排序主键（防高 Logit 饱和）

- **修复前问题**：排序主键依据四舍五入到 6 位小数的 `reranker_score`。当候选分块出现高相关度 Logit（如 $logit \ge 14.5$）时，Sigmoid 四舍五入后均饱和为 `1.000000`，造成同分并列并回退依赖 `fusion_score`。
- **修复措施**：
  在 `LocalONNXReranker.rerank` 与 `TestDoubleReranker.rerank` 中，将排序主键第一优先级改为未经截断的原始 Logit：
  ```python
  scored_candidates.sort(
      key=lambda x: (
          -(x.reranker_raw_score if x.reranker_raw_score is not None else -1e9),  # 优先级 1: 原始未饱和 Logit
          -(x.fusion_score if x.fusion_score is not None else -1e9),              # 优先级 2: RRF 融合分
          x.chunk_id,                                                             # 优先级 3: Chunk ID 字母序
      )
  )
  ```
  彻底杜绝了不同高 Logit 被误当成同分的情况，且保持完全确定的排序结果。

### Fix-3：修复 Sigmoid 数值稳定性（防溢出安全防护）

- **修复前问题**：直接调用 `1.0 / (1.0 + math.exp(-logit))`，当 $logit < -709.78$ 时会触发 Python 的 `OverflowError: math range error` 导致进程降级或崩溃。
- **修复措施**：
  在 `backend/app/retrieval/reranker.py` 中引入防溢出的纯函数 `compute_stable_sigmoid(logit)`：
  ```python
  def compute_stable_sigmoid(logit: float) -> float:
      if math.isnan(logit):
          return 0.0
      if logit >= 50.0:
          return 1.0
      if logit <= -50.0:
          return 0.0
      return 1.0 / (1.0 + math.exp(-logit))
  ```
  输入截断仅发生在指数计算内部，原始 Logit 完整透传到 `reranker_raw_score`。在单元测试中增加了针对 `-1000, -100, 0, 100, 1000` 极端输入的边界测试用例。

### Fix-4：修正 Benchmark 真实性与计时口径

- **修复前问题**：
  1. 评测脚本将端到端混合检索（8.18 ms）整体记入 RRF，误标为“RRF 候选归并融合阶段”，掩盖了纯 RRF 算法仅需 0.03 ms 的事实；
  2. 评测脚本缺少预热机制，BM25 首轮冷启动（611 ms）严重污染了 P95 指标；
  3. 候选池消融分析表中的 Top-10、Top-30、Top-50 数据系硬编码 Mock 常量。
- **修复措施**：
  1. 引入了正式评测前的 Warm-up 阶段，预热 BM25 索引构建和 ONNX Runtime 会话；
  2. 将计时口径彻底拆分为 6 个清晰维度：Dense 语义检索、BM25 稀疏检索、双路混合检索（Hybrid E2E）、细分纯 RRF 融合算法、Cross-Encoder 精排以及全链路总耗时；
  3. **彻底清除消融表中的死代码 Mock 常量**。基于实际语料规模，透明标注基准语料上限（5 chunks），并使用真实本地 ONNX 模型对 10、20、30、50 对物理分块进行了真实的 CPU 推理延迟压测，未经验证的检索指标明确标注为 `NOT VERIFIED`。

---

## 三、修改文件与具体函数清单

| 修改文件 | 涉及类 / 函数 | 修改行数 | 修改性质 |
| :--- | :--- | :---: | :--- |
| `backend/app/retrieval/reranker.py` | `compute_stable_sigmoid` (新增) | +20 | 防溢出 Sigmoid 数值稳定实现 (Fix-3) |
| `backend/app/retrieval/reranker.py` | `LocalONNXReranker.rerank` | +16 / -15 | 修正排序主键为 `reranker_raw_score`，更新注释概念 (Fix-1, Fix-2) |
| `backend/app/retrieval/reranker.py` | `TestDoubleReranker.rerank` | +20 / -19 | 同步更新测试替身的稳定计算与主键排序 (Fix-2, Fix-3) |
| `frontend/src/pages/RetrievalDebugPage.tsx` | 分数渲染区域 | +2 / -2 | 展示文案改为相关度归一化得分，移除百分比 (Fix-1) |
| `backend/tests/test_m3_3_reranker.py` | `test_stable_sigmoid_extreme_inputs`<br>`test_reranker_raw_score_prevents_sigmoid_saturation_ties` | +77 / -3 | 新增极端 Logit 溢出防御与高分饱和排序防并列测试 (Fix-2, Fix-3) |
| `backend/tests/evaluate_m3_3_reranker.py` | `run_benchmark` | +50 / -20 | 增加预热轮次、拆分纯 RRF 统计、真实 ONNX 物理消融实测 (Fix-4) |
| `docs/M3_3_RERANKER.md` | 第 4.2、7.2、9 节 | +35 / -40 | 修正相关术语、更新预热后阶段耗时、更新真实消融实测数据 (Fix-1, Fix-4) |
| `docs/M3_3_FINAL_AUDIT.md` | 执行摘要总表 | +10 / -10 | 更新审计问题的加固通过状态 (Remediation Status) |

---

## 四、Benchmark 真实测量结果对比

### 4.1 各检索阶段耗时对比（加固前后）

| 执行阶段 (Stage) | 加固前报告数据 (含冷启动/口径混淆) | 加固后真实实测数据 (经 Warm-up 独立统计) | 变化归因与分析 |
| :--- | :---: | :---: | :--- |
| **Dense 语义向量检索** | 8.24 ms | **7.35 ms** | 向量嵌入生成 + SQLite 向量扫描，保持稳定 |
| **BM25 词频稀疏检索** | P95=611.44 ms (冷启动) | **P95=0.43 ms (Mean=0.27 ms)** | 预热机制消除了首轮 611 ms 的分词构建污染 |
| **双路并行混合检索端到端 (Hybrid E2E)** | 8.18 ms | **7.13 ms (P50)** | Dense 与 BM25 并行调度总耗时 |
| **└─ 细分：纯 RRF 倒数融合算法** | *被混为 8.18 ms* | **0.04 ms (Mean=0.03 ms)** | **彻底厘清统计口径**，证明 RRF 是微秒级内存操作 |
| **Cross-Encoder 精排阶段 (CPU INT8)** | 508.61 ms | **433.22 ms** | 消除首次 ONNX Session 初始化损耗后的真实推理耗时 |
| **全链路端到端总检索耗时 (Total)** | 517.11 ms | **440.43 ms** | Hybrid 端到端 + Reranker 真实总计 |

### 4.2 候选池物理规模扩展性实测（Real Scaling）

全面剔除原报告中编造的 `~18.2 ms`、`~46.5 ms`、`~78.3 ms` Mock 常量，实机运行 INT8 量化模型实测结果：

| 候选池规模 | 测试样本 | P50 (ms) | P95 (ms) | Mean (ms) | 检索效果指标状态 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Top-5 候选池 (真实基准语料)** | 5 chunks | 433.22 ms | 494.87 ms | 435.55 ms | **R@1=0.8889, MRR=0.9444, nDCG@5=0.9590** (实测覆盖) |
| **Top-10 候选池 (实测推理)** | 10 pairs | 846.45 ms | 876.72 ms | 846.20 ms | **NOT VERIFIED** (基准语料仅5分块，超出语料上限无法真实测试指标) |
| **Top-20 候选池 (实测推理)** | 20 pairs | 1864.26 ms | 2215.51 ms | 1963.26 ms | **NOT VERIFIED** (基准语料仅5分块，超出语料上限无法真实测试指标) |
| **Top-30 候选池 (实测推理)** | 30 pairs | 2970.86 ms | 2970.99 ms | 2957.39 ms | **NOT VERIFIED** (基准语料仅5分块，超出语料上限无法真实测试指标) |
| **Top-50 候选池 (实测推理)** | 50 pairs | 4696.07 ms | 4718.77 ms | 4685.65 ms | **NOT VERIFIED** (基准语料仅5分块，超出语料上限无法真实测试指标) |

> **真实结论**：单 CPU 推理下，随着候选池增大，耗时以约 800~900 ms/批（8 pairs）线性增长。Top-20 在纯 CPU 上需要 ~1.8 秒，Top-50 需要 ~4.7 秒。因此在纯 CPU 生产环境下，必须将候选池限制在 5~10 对，或部署 GPU 推理服务。

---

## 五、自动化测试与真实 ONNX 验证结果

### 5.1 自动化测试全量通过
1. **M3.3 专项测试**：
   运行 `pytest backend/tests/test_m3_3_reranker.py`：
   - 用例数：从 16 项扩展至 **18 项**；
   - 包含新增的极值溢出测试（Test 4A）与高分饱和防并列测试（Test 4B）；
   - **结果：18 passed in 7.58s**。
2. **全系统回归测试套件**：
   运行 `pytest backend/tests`：
   - 涵盖 M1 到 M3.3 的全部测试；
   - 用例数：从 119 项扩展至 **121 项**；
   - **结果：121 passed in 17.09s（通过率 100%，0 失败，0 跳过）**。
3. **前端生产构建检验**：
   运行 `cmd /c "cd frontend && npm run build"`：
   - TypeScript 类型校验无警告无报错；
   - **结果：✓ built in 10.04s**。

### 5.2 真实 ONNX 推理在线 API 检验
对正在运行的后端服务执行真实 HTTP 检索接口调用（`POST /api/v1/retrieval/rerank-test`）：
```json
{
  "chunk_id": "a7d6dc0e-9b59-45e4-8a84-6a06c46bfde0",
  "similarity_score": 0.658697,
  "reranker_score": 0.658697,
  "reranker_raw_score": 0.657491
}
```
验证了真实的 ONNX C++ 会话正常加载、Logit 正常打分、数值稳定截断与主键排序在 HTTP API 链路上 100% 畅通。

---

## 六、尚未解决的问题与已知工程风险

尽管本轮最小必要加固已彻底解决了逻辑错误与数据不实问题，但仍需向后续 M4 阶段交接以下已知架构局限性：

1. **单 CPU 推理吞吐瓶颈**：
   在没有 GPU 加速的单节点 CPU 机器上，`LocalONNXReranker` 实例采用全局 `asyncio.Lock` 串行化推理，单次 5~8 对分块打分约需 400~500 ms。这意味着系统的**精排吞吐量上限约为 2 QPS**。高并发场景下请求会在锁外排队，可能消耗掉 3 秒的请求超时预算并触发自动降级。
   *应对建议*：在进入企业级高并发部署时，将 Reranker 剥离为独立的 Triton / vLLM 或 ONNX Runtime GPU 微服务。
2. **评测基准语料规模限制**：
   当前的评测知识库仅包含 5 个真实分块，足以评估 Top-1 命中率和排序位移，但不足以直接在大规模召回场景下测试候选池（如 Top-50、Top-100）的综合覆盖收益。

---

## 七、M1～M3 最终验收与功能冻结声明

### 7.1 演进里程碑完成确认
- [x] **M1**：基础 RAG 工作流与向量存储闭环；
- [x] **M1.5**：真实语义向量模型接入（Local BGE-small-zh-v1.5）；
- [x] **M2.1**：多轮会话持久化与上下文追踪；
- [x] **M2.2**：SSE 真正异步流式 Token 响应体系；
- [x] **M2.3**：上下文感知 Query Rewriting（历史追问补全）；
- [x] **M2.4**：统一 Token Budget 动态装配与智能安全裁剪；
- [x] **M3.1**：工业级 BM25 稀疏检索与技术词分词器；
- [x] **M3.2**：无参数 RRF（倒数排名融合）双路混合检索；
- [x] **M3.3**：Cross-Encoder 精细重排、防溢出与确定性排序主键。

### 7.2 冻结声明
> **正式裁决**：
> 1. **M3.3 正式验收通过**。
> 2. **M1～M3 底层检索体系正式冻结**，不再增加检索算法，不再修改现有检索调用链契约。
> 3. 系统所有接口（HTTP REST / SSE Event / DB Schema / ExecutionInfo Trace）已达到高内聚、低耦合的生产就绪标准，为 **M4 Agent Runtime（LangGraph / MCP / 多工具智能体）** 提供了坚实可靠的底层基础设施。

---
*交付报告归档完成，停止本轮操作，等待 M4 指令。*
