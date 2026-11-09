# AgentStudio-RAG — 独立离线评测系统 (Evaluation Framework)

本目录为 AgentStudio-RAG 的独立评测模块，与生产业务服务完全解耦。用于对系统的检索算法、排序模型、Token 预算及 Agent 多步执行能力进行可重复、版本化的量化实验。

## 一、目录结构

```text
eval/
├── datasets/
│   ├── benchmark_v1.jsonl       # 20 条经过真实解析分块与人工复核的技术问答评测基准数据
│   └── fixtures/                # 评测独立测试文档资产 (Markdown)
│       ├── microservice_gateway_spec.md
│       ├── high_availability_resilience.md
│       └── compliance_and_audit.md
├── core/
│   ├── metrics_retrieval.py     # 纯 Python 检索指标计算器 (Recall@K, Precision@K, MRR@K, nDCG@K)
│   ├── dataset_loader.py        # 基准测试集加载、校验与 Chunk 动态绑定器
│   └── result_writer.py         # 评测报告持久化组件 (导出 JSON, CSV, Markdown)
├── runners/
│   └── run_retrieval_ablation.py# 检索四组消融实验执行入口
├── reports/                     # 实验报告输出目录
│   ├── retrieval_results.json   # 完整实验明细与元数据
│   ├── retrieval_results.csv    # 指标对比表格
│   └── retrieval_ablation_report.md # 正式对比实验分析报告
└── README.md                    # 本说明文档
```

## 二、评测指标定义

1. **Recall@K (召回率)**：
   $$\text{Recall@K} = \frac{|\text{Top-K 检出的相关分块} \cap \text{标注黄金相关分块集合}|}{|\text{标注黄金相关分块集合}|}$$
2. **Precision@K (精确率)**：
   $$\text{Precision@K} = \frac{|\text{Top-K 检出的相关分块} \cap \text{标注黄金相关分块集合}|}{K}$$
3. **MRR@K (平均倒数排名)**：
   $$\text{MRR@K} = \frac{1}{\text{rank of first relevant chunk}} \quad (\text{若未检出则为 } 0)$$
4. **nDCG@K (归一化折损累积增益)**：
   基于分级相关性（直接核心答案分块为 2，背景补充上下文分块为 1）：
   $$\text{DCG@K} = \sum_{i=1}^{K} \frac{2^{rel_i} - 1}{\log_2(i + 1)}, \quad \text{nDCG@K} = \frac{\text{DCG@K}}{\text{IDCG@K}}$$

## 三、运行消融实验

评测使用独立的 SQLite 数据库 `backend/data/storage/eval_benchmark.db`，不影响生产库：

```bash
# 在项目根目录下执行
backend\.venv\Scripts\python.exe eval/runners/run_retrieval_ablation.py
```
