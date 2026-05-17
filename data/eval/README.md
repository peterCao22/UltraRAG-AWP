# 评测集人工标注指南（Phase 8.1.4）

> 本目录是 Phase 8.1 离线评测体系的数据落点。你（业务方 / RAG owner）需要把
> 自动产出的候选集合并、筛选、补全 → 终版 `data/eval/<kb>.jsonl`，让 RAG 团队
> 可以跑 `eval_custom_app.py` 出基线分数。

## 一、文件说明

| 文件 | 用途 | 状态 |
|------|------|------|
| `<kb>_raw.jsonl` | A 路：从 `kb_session_messages` 抽的真实用户 query。**没有 gold_answer / relevant_chunk_ids**，必须人工补 | 自动产出 |
| `<kb>_gen.jsonl` | B 路：Gemini 编的「问题 + gold_answer」，每条带 `relevant_chunk_ids` 指向源 chunk。**需人工筛选**（保留 ≥50%） | 自动产出 |
| `<kb>_manual.jsonl` | C 路：业务方手写的高质量 query（可选） | **你创建** |
| `<kb>.jsonl` | **终版评测集**。所有三路合并后的最终标注，每条必须含 5 个必填字段 | **你产出** |
| `baseline/<kb>_<date>.json` | 跑分快照（Phase 8.1.7 由评测脚本自动写入） | 自动产出 |

## 二、JSONL 字段定义

```jsonl
{"id": "eval_001",
 "kb_id": "agv_demo",
 "query": "AGV 启动前要做哪些检查？",
 "relevant_chunk_ids": ["agv_demo_step_1"],
 "gold_answer": "检查电池电量、急停按钮、传感器",
 "tags": ["step_query"],
 "source": "session"}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | ✅ | 唯一 ID，建议格式 `eval_<kb>_<n>` |
| `kb_id` | ✅ | 知识库 ID，必须与文件名前缀一致 |
| `query` | ✅ | 用户问题（保持口语化） |
| `relevant_chunk_ids` | ✅ | **1-3 个**应被召回的 chunk_id。从 `data/kb/<kb>/corpora/chunks.jsonl` 里挑 |
| `gold_answer` | ✅ | 期望答案，**≤200 字**；用于字符串/F1 匹配 |
| `tags` | ⭕ | 分桶标签，如 `step_query` / `faq` / `multi_step` / `error_code` |
| `source` | ⭕ | `session` / `generated` / `manual`（保留追溯） |

## 三、标注工作流

### Step 1：阅读候选

```powershell
# 看看自动产出（用编辑器或 cat）
type data\eval\agv_demo_raw.jsonl
type data\eval\agv_demo_gen.jsonl
```

### Step 2：决定每条候选去留

- **`<kb>_raw.jsonl`**（session 真实问题）：每条都要**人工补 `relevant_chunk_ids` + `gold_answer`**
  - 在 `data/kb/<kb>/corpora/chunks.jsonl` 里找最相关的 1-3 个 chunk
  - gold_answer 从 chunk 内容里精简成 ≤200 字
- **`<kb>_gen.jsonl`**（Gemini 编的）：**人工筛**
  - 问题口语化、不照抄文档 → 保留
  - 问题与 chunk 不对应、答案编造、过于死板 → 删除
  - 期望保留率 ≥50%
- **`<kb>_manual.jsonl`**（手写）：直接写终版字段

### Step 3：合并

把三路确认通过的样本合并写入 `data/eval/<kb>.jsonl`：

```powershell
# 简单合并示例（PowerShell）；实际工作时建议用编辑器手动调整
Get-Content data\eval\agv_demo_raw_reviewed.jsonl, data\eval\agv_demo_gen_reviewed.jsonl, data\eval\agv_demo_manual.jsonl | Set-Content data\eval\agv_demo.jsonl
```

### Step 4：自检

```powershell
# 校验文件合规（schema + 重复 ID + KB 一致性）
.venv\Scripts\python.exe -c "from custom_app.services.eval.dataset import load_eval_dataset; from pathlib import Path; xs = load_eval_dataset(Path('data/eval/agv_demo.jsonl'), expected_kb_id='agv_demo'); print(f'OK: {len(xs)} items')"
```

通过后即可交给 RAG 团队跑 baseline。

## 四、标注质量经验

> 这些原则来自 Phase 8.1 PLAN §三和实操打磨：

1. **gold_answer 越短越好**。token-level F1 对长答案很敏感，「检查电池电量、急停按钮、传感器」比「您需要先确认 AGV 的电池电量是否充足，然后再检查急停按钮的状态…」更稳定
2. **relevant_chunk_ids 标 1-3 个**。太多会让 Recall@5 失真；只标"最相关"那 1-3 个
3. **每条样本只有一个 KB**。不要让一个文件里混 `agv_demo` 和 `ifs_docs`
4. **打 tag**。分桶分析时会按 tag 分组报告（同 tag ≥3 条样本才出独立行）

## 五、终版规模建议

| KB | 候选合计 | 标注后目标 |
|----|---------|----------|
| agv_demo | 55（15 raw + 40 gen） | ≥50 条 |
| ifs_docs | 48（7 raw + 41 gen） | ≥50 条（不够可加 C 路手写补） |

每 KB **≥50 条**是 PLAN §九 验收门槛。少于 50 条信号容易被噪声淹没。

## 六、不进 git 的文件

`<kb>_raw.jsonl` / `<kb>_gen.jsonl` 是过程文件，每次自动跑都会被覆盖；**只把终版 `<kb>.jsonl` 进 git**。当前 `.gitignore` 未硬隔离，请人工把 `_raw` / `_gen` 文件加进忽略列表，或在 git add 时只显式添加终版文件。

## 七、常见疑问 FAQ

### Q1：三路是怎么回事？A 路是不是「user 问 + assistant 答」一起抽？

**不是。** A 路只抽 user 问的话，**答案需要人工补**——助手历史回答可能就是错的（评测要找的恰恰是检索/回答缺陷），把 bug 当成标准答案会让评测失去意义。

| 路 | 谁产出 query | 谁产出 gold_answer + relevant_chunk_ids | 你要做什么 |
|---|---|---|---|
| **A 路**（`<kb>_raw.jsonl`） | 自动抽 `kb_session_messages` 真实用户问题 | **都为空** | 人工对每条去 `chunks.jsonl` 找对应 chunk_id + 从 chunk 内容精简出 gold_answer |
| **B 路**（`<kb>_gen.jsonl`） | Gemini 看着 chunk 编 | Gemini 一起编（已自动填好） | **只筛不写**——逐条扫，问题不像真实用户会问的、或答案被 Gemini 编造的，整行删掉。期望保留率 ≥50% |
| **C 路**（`<kb>_manual.jsonl`） | 你手写 | 你手写 | 全部手写。补 A/B 路覆盖不到的角度（反问类、数字类、跨文档类） |

B 路怎么跑（已经跑过，不需要重跑）：
```powershell
python -m custom_app.scripts.generate_eval_queries --kb agv_demo --num-chunks 20 --per-chunk 2 --output data/eval/agv_demo_gen.jsonl
```
脚本逻辑：sample 20 个 chunk → 让 Gemini 每个 chunk 编 2 道题 + gold_answer → 写文件。

C 路示例（你新建 `data/eval/agv_demo_manual.jsonl`，照下面格式逐行写）：
```jsonl
{"id": "eval_agv_demo_manual_001", "kb_id": "agv_demo", "query": "AGV 启动前要做哪些检查？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_intro"], "gold_answer": "检查电池电量、急停按钮、传感器状态", "tags": ["pre_check"], "source": "manual"}
{"id": "eval_agv_demo_manual_002", "kb_id": "agv_demo", "query": "ID 01 这个告警代码代表什么故障？", "relevant_chunk_ids": ["E-Stop SOP_intro"], "gold_answer": "E-Stop Button Active，急停按钮被按下导致 AGV 停止", "tags": ["error_code"], "source": "manual"}
{"id": "eval_agv_demo_manual_003", "kb_id": "agv_demo", "query": "更换 AGV 电池一共多少步？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_1"], "gold_answer": "共 11 步，从拆卸旧电池到装回新电池", "tags": ["step_count", "multi_step"], "source": "manual"}
```

### Q2：同义问题怎么办？比如 "AGV更换电池的步骤" 和 "AGV换电池的流程" 答案一样，要写 2 条吗？

**不要重复写。只保留 1 条**，删掉另一条。

**原因**：评测打分按样本平均。同义问题写两条 = 同一道题被算两次：
- 检索答对 → Recall@5 多加 1
- 检索答错 → Recall@5 多扣 1

50 条评测集里如果有 5 对同义问题，等于实际只有 45 道独立题，权重失真。

**那同义问题就完全浪费了吗**——不浪费。保留 1 条用来评测，另 1 条**改写**成不同角度的问题，让两条样本都有独立信号：

```
原 query 1: AGV更换电池的步骤
原 query 2: AGV换电池的流程
```
改成：
```
保留: AGV更换电池的步骤        ← 测「能否召回 SOP 全步骤」
改写: 换电池第一步要做什么？    ← 测「能否定位到 step_1 这一节」
```

或者删掉一条、用 C 路手写一条覆盖**别的角度**（如"换电池一共多少步"、"换电池途中急停了怎么办"）。

**经验数据**：这种"换说法但同一问题"的情况通常占真实 session 的 20-30%，扫 raw 文件时优先合并/改写这类。
