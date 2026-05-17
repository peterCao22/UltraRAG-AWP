# Phase 8.1 —— 离线评测体系

> **状态**：🟡 工程脚手架已落地（2026-05-17）；剥离 metrics 已完成；待业务侧人工标注 50 条/KB → 跑 baseline
> **草案讨论**：2026-05-16
> **前置**：[Phase 8 README](./README.md)、Phase 5 三栈已就绪
> **借用**：UltraRAG `servers/evaluation` + `servers/benchmark`（开发期）
> **剥离时机**：评测脚本跑通后，半天内把 `evaluation.py` 核心指标搬到 `custom_app/services/eval/metrics.py`

---

## 一、目标

1. 建立 **50 条以上**的离线评测集（per KB），覆盖典型用户问题
2. 写一个评测驱动脚本，能把当前 `rag_runner` 的检索 + 生成结果跑出**量化分数**
3. 输出 **baseline.json** 作为 Phase 8.2 / 8.3 的对照基线
4. 验证剥离可行性：评测脚本能在**不依赖 UltraRAG runtime** 的情况下完成全部指标计算

---

## 二、非目标（推迟）

| 推迟项 | 推到哪 |
|--------|--------|
| 在线评测（生产流量采样打分） | Phase 9+ |
| LLM-as-judge（用大模型给答案打分） | Phase 8.1.x 增量，先用纯字符串指标 |
| 多语言评测集 | 当前 SOP 主要中文，英文样本酌情加 |
| 自动从生产日志构造评测集 | 手工构造 + Gemini 辅助生成混合 |

---

## 三、评测集设计

### 3.1 格式（JSONL）

```jsonl
{"id": "eval_001", "kb_id": "ifs_docs", "query": "如何在 IFS 中查询库存？", "relevant_chunk_ids": ["ifs_demo_section_3", "ifs_demo_step_2"], "gold_answer": "1. 打开库存模块...\n2. 输入零件号...\n3. 查看库存数量"}
{"id": "eval_002", "kb_id": "agv_demo", "query": "AGV 启动前要做哪些检查？", "relevant_chunk_ids": ["agv_demo_step_1"], "gold_answer": "检查电池电量、急停按钮、传感器"}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | str | ✅ | 评测项唯一 ID（`eval_NNN`） |
| `kb_id` | str | ✅ | 哪个知识库 |
| `query` | str | ✅ | 用户问题（保持口语化） |
| `relevant_chunk_ids` | list[str] | ✅ | 标注「应该被召回」的 chunk_id，**1-3 个**为佳 |
| `gold_answer` | str | ✅ | 期望答案，**简短**（≤200 字），用于字符串匹配/F1 |
| `tags` | list[str] | ⭕ | `["step_query"]` / `["faq"]` / `["multi_step"]` 等，分桶分析 |

### 3.2 评测集来源（三路组合）

| 来源 | 数量目标 | 工作量 | 备注 |
|------|---------|--------|------|
| **A. 从 `kb_session_messages` 真实问题** | 20-30 条 | 1 天 | 最有代表性；要人工标 `relevant_chunk_ids` 和精简 `gold_answer` |
| **B. Gemini 自动生成** | 20-30 条 | 半天 | 给一个 chunk，让 Gemini 编 3 个问题 + gold answer；人工筛 |
| **C. 业务方手写** | 0-20 条（可选） | 业务侧 0.5-1 天 | 最高质量，但要协调时间 |

总目标 **50-60 条 / KB**，先做 `ifs_docs` 和 `agv_demo` 两个 KB。

### 3.3 存放位置

```
data/eval/
├── ifs_docs.jsonl           # 评测集
├── agv_demo.jsonl
└── baseline/
    ├── ifs_docs_2026-05-XX.json     # 跑分快照
    └── agv_demo_2026-05-XX.json
```

---

## 四、评测指标

### 4.1 检索指标（不依赖生成模型）

| 指标 | 含义 | 计算 |
|------|------|------|
| **Recall@5** | top-5 召回中包含正确 chunk 的样本占比 | `\|gold ∩ top5\| / \|gold\|` 按样本平均 |
| **Recall@10** | top-10 召回中包含正确 chunk 的样本占比 | 同上 |
| **MRR** | 第一个正确 chunk 出现位置的倒数 | `1 / first_rank`，按样本平均 |
| **nDCG@10** | 折损命中（位置越靠前权重越大） | 标准 nDCG 公式 |
| **Hit@1** | top-1 就命中 gold 的比例 | 衡量"一击必中"能力 |

### 4.2 生成指标（端到端）

| 指标 | 含义 | 用 UltraRAG 哪个函数 |
|------|------|--------------------|
| **Accuracy** | gold 是否包含在 prediction 中 | `evaluation.accuracy_score` |
| **F1** | token-level F1（gold vs prediction） | `evaluation.f1_score` |
| **ROUGE-L** | 最长公共子序列召回 | `evaluation.rouge_l_score` |
| **EM** | 完全匹配（标准化后） | `evaluation.exact_match_score` |
| **Cover-EM** | gold tokens 是否全部出现在 prediction | `evaluation.cover_exact_match_score` |

### 4.3 分桶分析

按 `tags` 字段分桶（如 `step_query` / `faq` / `multi_step`），分别报告指标，识别**弱项场景**。

---

## 五、技术方案

### 5.1 开发期（借用 UltraRAG）

**阶段 1：评测集构造（1.5 天）**

```bash
# A. 从 kb_session_messages 抽 20-30 条
python -m custom_app.scripts.extract_eval_queries --kb agv_demo --output data/eval/agv_demo_raw.jsonl

# B. Gemini 生成 20-30 条
python -m custom_app.scripts.generate_eval_queries --kb agv_demo --num 30 --output data/eval/agv_demo_gen.jsonl

# C. 人工合并 + 标注（手工编辑 jsonl）
```

**阶段 2：跑分（半天）**

```python
# custom_app/scripts/eval_custom_app.py
def evaluate(kb_id: str, eval_file: Path) -> dict:
    runner = RagRunner(kb_id=kb_id)
    predictions = []
    for item in load_jsonl(eval_file):
        # 调 RagRunner.search() 拿 retrieved chunks
        hits = runner.search(item["query"], top_k=10)
        # 调 RagRunner.chat() 拿 generated answer（或只跑检索）
        answer = runner.chat(item["query"])
        predictions.append({
            "id": item["id"],
            "query": item["query"],
            "gold_chunk_ids": item["relevant_chunk_ids"],
            "retrieved_chunk_ids": [h.chunk_id for h in hits],
            "gold_answer": item["gold_answer"],
            "predicted_answer": answer,
        })
    return compute_metrics(predictions)
```

`compute_metrics` 调 UltraRAG 的 `evaluation` server（开发期）：

```bash
# 把 predictions 导出 → 让 UltraRAG 算分
ultrarag run examples/eval_custom_app.yaml --pred predictions.jsonl --gold data/eval/agv_demo.jsonl
```

或直接 import UltraRAG 的算法函数（更简单，省一个 MCP 进程）：

```python
from servers.evaluation.src.evaluation import (
    accuracy_score, f1_score, rouge_l_score, exact_match_score
)
```

**阶段 3：导出基线**

```bash
python -m custom_app.scripts.eval_custom_app --kb agv_demo --save-baseline
# → data/eval/baseline/agv_demo_2026-05-XX.json
```

### 5.2 剥离期（半天）

**剥离动作**：

1. 复制 `servers/evaluation/src/evaluation.py` → `custom_app/services/eval/metrics.py`
2. 删掉文件顶部的：
   ```python
   from ultrarag.server import UltraRAG_MCP_Server
   app = UltraRAG_MCP_Server("evaluation")
   ```
3. 删掉每个函数上的 `@app.tool()` 装饰器
4. 保留：`normalize_text` / `accuracy_score` / `exact_match_score` / `cover_exact_match_score` / `f1_score` / `rouge1_score` / `rouge2_score` / `rouge_l_score` / `string_em_score`
5. 加 pytest 用例：`tests/test_eval_metrics.py`（每个函数 2-3 个边界 case）

**剥离后**的 `custom_app/services/eval/`：

```
custom_app/services/eval/
├── __init__.py
├── metrics.py          # ~400 行（剥离自 UltraRAG）
├── dataset.py          # ~80 行（jsonl IO + 评测集校验）
└── runner.py           # ~120 行（驱动 RagRunner + 收集 predictions）
```

`scripts/eval_custom_app.py` 改成 `import from custom_app.services.eval` 即可，**0 行 UltraRAG 依赖**。

---

## 六、任务拆分

| 子任务 | 工时 | 复杂度 | 验收 |
|--------|------|--------|------|
| 8.1.1 评测集格式 + schema 校验函数 | 0.5 天 | LOW | `pytest tests/test_eval_dataset.py` |
| 8.1.2 `extract_eval_queries.py`（从 session 抽真实 query） | 0.5 天 | LOW | 跑出 20+ 条候选 |
| 8.1.3 `generate_eval_queries.py`（Gemini 辅助生成） | 0.5 天 | MEDIUM | 跑出 30 条候选；人工筛选率应 >50% |
| 8.1.4 人工标注 + 合并 → 终版 `data/eval/<kb>.jsonl` | 1 天 | LOW（耗时） | 每 KB 50+ 条 |
| 8.1.5 `eval_custom_app.py`（驱动 + 指标计算） | 1 天 | MEDIUM | 跑出 baseline 报告 |
| 8.1.6 剥离 `metrics.py` 到 custom_app + pytest | 0.5 天 | LOW | 全部测试通过；评测脚本切到新路径 |
| 8.1.7 基线快照 + 文档 | 0.5 天 | LOW | `baseline_2026-05-XX.json` |
| **合计** | **4.5 天** | | |

---

## 七、关键风险

| 等级 | 风险 | 缓解 |
|------|------|------|
| 🟡 MED | 评测集偏向当前 chat 历史 → 改进方向被锁死在已有问题 | B 路（Gemini 生成）补充长尾；定期补新 query |
| 🟡 MED | gold_chunk_ids 标注主观 → 同一问题可能有多个合理 chunk | 允许标 1-3 个，Recall@5 而非 Recall@1 |
| 🟢 LOW | 字符串指标对意译不敏感 | 后续 Phase 8.1.x 加 LLM-as-judge |
| 🟢 LOW | UltraRAG `evaluation.py` 的依赖（rouge_score / tabulate）需装 | 已在 `.venv` 中，剥离后直接保留 |

---

## 八、待讨论问题

实施前要确认：

1. **评测集是否分 train/test**？目前不分（50 条不够分），全用于评测；改 chunking 时不能用评测集本身调参
2. **生成评测是否每次跑**？跑生成耗 Gemini 配额；可分两档：CI 跑「检索指标」，本地手动跑「生成指标」
3. **多个 KB 是否合并打分**？建议分 KB 报告（agv_demo SOP 强结构、ifs_docs FAQ 弱结构，混在一起会平均掉信号）
4. **baseline 是否进 git**？建议**进**，便于历史回溯，但放 `data/eval/baseline/` 不进 `data/kb/`
5. **失败样本的复盘机制**？跑完输出 `failures.jsonl`（gold 没命中或 F1<0.3 的样本），人工 review

---

## 九、验收清单

- [ ] `data/eval/agv_demo.jsonl` 和 `data/eval/ifs_docs.jsonl` 各 ≥50 条
- [ ] `python -m custom_app.scripts.eval_custom_app --kb agv_demo` 跑通，输出 8 个指标
- [ ] `data/eval/baseline/<kb>_2026-05-XX.json` 已 commit
- [ ] `custom_app/services/eval/metrics.py` 0 行 UltraRAG import
- [ ] `tests/test_eval_metrics.py` 全部通过
- [ ] README 段落更新：如何跑评测、如何看分数

---

> 本文档进入讨论后，逐项确认 → 进入实施。所有「待讨论问题」必须先达成共识。

---

## 十、实施记录（2026-05-17，工程脚手架阶段）

### 共识确认（PLAN §八.待讨论问题）

| 议题 | 决议 |
|---|---|
| train/test 切分 | **不分**（50 条不够分；人为约束：改 chunking 时不能用评测集本身调参） |
| 生成指标频率 | **本地手动 + CI 只跑检索**（节省 Gemini 配额；`eval_custom_app.py` 默认 `--with-generation=False`） |
| KB 合并打分 | **分 KB 报告**（SOP vs FAQ 特性差异大；混报会掩盖弱项） |
| baseline 入 git | **进 git**（`data/eval/baseline/` 下；便于历史回溯与 PR 对比） |
| 失败样本复盘 | EvalReport.failures 字段收集 top-5 未命中 OR F1<0.3 样本 |

### 落地结构

```
custom_app/
├── services/eval/
│   ├── __init__.py
│   ├── schema.py        # EvalItem / EvalReport / RetrievalResult / GenerationResult
│   ├── dataset.py       # iter / load / write jsonl + 校验
│   ├── metrics.py       # ✅ 已剥离自 UltraRAG evaluation.py，0 行 ultrarag import
│   └── runner.py        # EvalRunner 驱动器（解耦 RagRunner，可注入 stub 测试）
└── scripts/
    ├── extract_eval_queries.py    # A 路：kb_session_messages 抽 user query
    ├── generate_eval_queries.py   # B 路：Gemini 编 N 题/chunk + 自填 relevant_chunk_ids
    └── eval_custom_app.py         # 评测入口：driver → report → baseline

tests/
├── test_eval_dataset.py       # 26 case
├── test_eval_metrics.py       # 42 case
├── test_eval_generators.py    # 9 case
└── test_eval_runner.py        # 9 case  →  小计 86 个新增测试

data/eval/
├── README.md                  # 业务侧标注指南
├── agv_demo_raw.jsonl         # 15 条 session 真实 query（待人工标注）
├── agv_demo_gen.jsonl         # 40 条 Gemini 候选（待人工筛 ≥50%）
├── ifs_docs_raw.jsonl         # 7 条 session 真实 query
├── ifs_docs_gen.jsonl         # 41 条 Gemini 候选
└── baseline/                  # 占位，待 8.1.7 跑分写入
```

`.gitignore` 已加 `data/eval/*_raw.jsonl` / `*_gen.jsonl` / `*_manual.jsonl`，
仅终版 `<kb>.jsonl` 与 `baseline/` 进 git。

### 与计划的落地差异

| PLAN 点 | 实际实施 | 原因 |
|---|---|---|
| evaluation.py 剥离仅指标函数 | 同时新增 4 个**检索指标**（Recall@k / Hit@k / MRR / nDCG@k） | UltraRAG 的 TREC 评估器（pytrec_eval）依赖外部进程，且只支持 TREC 格式；自写 30 行更直接、0 依赖 |
| `evaluation.py` 中 `compute_metrics` | 拆成 `compute_generation_metrics` + `compute_retrieval_metrics` 两个函数 | PLAN 的"unknown metric 不报错且不出现"语义更清晰；同时把检索/生成两套指标解耦 |
| 评测脚本生成阶段直接复用 `RagRunner.chat()` | 调 `_prepare_chat_context()` 拿 hit_ids，再可选调 `chat()` | RagRunner.chat() 内部已做完整检索+生成；评测只跑检索时绕开 LLM 调用更省成本 |
| Gemini prompt 使用纯文本 + JSON 解析 | 同 | 不引入 function calling 复杂度；JSON 响应靠 `responseMimeType: application/json` |

### Smoke test 结果

用 `agv_demo_gen.jsonl` 前 6 条做端到端 smoke：
- ✅ `eval_custom_app.py --kb agv_demo` 跑通：检索 + rerank + Qdrant + 指标 + 报告全链路无错
- ✅ 输出 8 个检索指标 + 失败样本 + per-tag 报告
- ⚠️ smoke 集 Recall@5 = 16.7%（6 条样本）—— 不是 PLAN 不达标，是 6 条样本的统计噪声；正常应等到 50 条/KB 终版数据出真实基线
- 一个有意思的信号：`hit@1 == hit@5 == hit@10`，说明 top-10 召回里要么命中要么完全不命中——正是 8.2/8.3 要破的"召回率本身不够"

### 测试结果

```
$ pytest tests/test_eval_*.py
86 passed in 0.6s
```

100% 通过，包括：
- ✅ `metrics.py` 中无 `import ultrarag` / `from ultrarag` —— PLAN §九 验收过
- ✅ rouge_score 缺失时 lazy fallback 返回 0.0，不抛错
- ✅ EvalRunner 注入 stub 即可单测，无须真起 FAISS/Qdrant

### 接下来需要业务侧介入的事

1. 阅读 [`data/eval/README.md`](../../data/eval/README.md)
2. 人工补完：
   - `agv_demo_raw.jsonl`（15 条）的 `relevant_chunk_ids` + `gold_answer`
   - `agv_demo_gen.jsonl`（40 条）的人工筛选（保留 ≥50%）
   - `ifs_docs_*.jsonl` 同上；不够 50 条时手写补 `ifs_docs_manual.jsonl`
3. 合并三路 → 终版 `data/eval/agv_demo.jsonl` 与 `data/eval/ifs_docs.jsonl`
4. RAG 团队跑 `python -m custom_app.scripts.eval_custom_app --kb <kb> --save-baseline` 产出 baseline JSON

**剥离工作已完成**：`services/eval/metrics.py` 与 `runner.py` 共 360 行，**0 行 UltraRAG 依赖**。Phase 8.2 / 8.3 可直接在此基础上对比检索分数。
