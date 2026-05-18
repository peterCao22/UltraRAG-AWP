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

### Q3：一个问题对应很多 chunk 怎么办？比如"更换电池的步骤"涉及 step_1~step_11，要全填上吗？

**不要全填。最多 3 个，挑最核心的 1-2 个。**

**原因**：`relevant_chunk_ids` 是评测的"标准答案集合"，Recall 公式是：
```
Recall@5 = |gold ∩ top5_retrieved| / |gold|
```
分母是 `|gold|`。**填越多 chunk，分母越大，满分越难拿**。

举例：你填了 4 个 chunk，检索系统 top-5 只命中其中 2 个（`step_1` 和 `step_2`），Recall@5 = 2/4 = 0.5。即使系统行为合理（把最相关的 step_1/2 排前面），分数也只有一半。

更糟的是 nDCG：把全 11 步都标上，理想排序假设它们都该在前 11 位，但 top-5 只能装 5 个——剩下 6 个无论怎么排都"该在前面但没在"，扣分。

#### 实操规则

| 用户问题类型 | gold 怎么标 |
|---|---|
| "**更换电池的步骤**" | 标 `step_1`（首步骤）或 `_intro`（含步骤总览）。**别把全 11 步都标上** |
| "**第二步要做什么？**" | 只标 `step_2`。这才是题目真正考的位置 |
| "**更换电池一共多少步？**" | 标 `_intro` 或含总数的章节。问题问的是"多少步"，不是"每步内容" |
| "**怎么换电池，详细一些**" | 仍只标 1-2 个最核心 chunk。系统能召回 1-2 个相关 chunk → LLM 拼答案就够完整 |

#### 反直觉但重要的事实

**评测集不是"答案手册"，是"信号探针"**。你不是在告诉系统"这 11 个 chunk 都该召回"，你是在测**它能不能找到最该找到的 1-2 个**。剩下的相关 chunk 是系统的"加分项"，不影响打分。

#### 想测完整流程？拆成多个独立样本反而更好

把 "AGV 更换电池的步骤"（标 11 个 chunk）改写成 3-4 个独立样本，每个测一个具体维度：

```jsonl
{"id": "..001", "query": "换电池第一步是什么？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_1"]}
{"id": "..002", "query": "换电池第二步怎么操作？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_2"]}
{"id": "..003", "query": "换电池一共多少步？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_intro"]}
{"id": "..004", "query": "换电池的完整流程？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_intro"]}
```

3 个独立信号 vs 1 个含 11 chunk 的样本，**前者诊断价值高得多**——你能看到具体哪一步检索弱、哪一步检索强。

### Q4：那 gold_answer 怎么写？比如标了 3 个 chunk（step_1 / step_2 / step_7），硬汇总操作步骤很别扭

**两种方案，按情况选。**

#### 方案 A（推荐）：拆成多个独立样本

既然你已经识别出 3 个独立 step，**拆**比"汇总"信号更干净：

```jsonl
{"id": "eval_agv_demo_001", "query": "换电池第一步要做什么？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_1"], "gold_answer": "<step_1 内容精简>"}
{"id": "eval_agv_demo_002", "query": "换电池第二步要做什么？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_2"], "gold_answer": "<step_2 内容精简>"}
{"id": "eval_agv_demo_003", "query": "换电池到了第 7 步该怎么操作？", "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_7"], "gold_answer": "<step_7 内容精简>"}
```

3 条样本、3 个独立信号；每条 gold_answer 来自单个 chunk，简洁可验证。

#### 方案 B：保留宽泛 query，gold_answer 写"关键词清单"

如果用户真的爱问"更换电池的步骤"这种宽泛问题（想保留这类样本），gold_answer **不要逐步抄文档**，写**该出现的关键词清单**：

```jsonl
{"query": "AGV更换电池的步骤",
 "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_1", "BatteryChangeSequenceSOP_step_2", "BatteryChangeSequenceSOP_step_7"],
 "gold_answer": "拆电池、断电源、检查接口、装新电池、启动确认"}
```

要点：
- gold_answer 是评测打分用的"参考答案"，**不是给用户看的最终回答**
- 6-15 个关键词足够 F1 / Cover-EM 算分
- 不追求"完整可读的步骤描述"

#### 为什么硬汇总不好

| 写法 | 问题 |
|---|---|
| 把 3 步内容逐字拼起来（200+ 字） | gold_answer 太长 → token-level F1 极敏感，LLM 答案多/少一句都掉分 |
| 写一段流畅的"操作说明" | 你在替 LLM 写答案；评测应该测"系统答得对不对"，不是"系统写得像不像你" |
| 写"详见 SOP 文档第 1/2/7 步" | 这种元描述无法被 F1/Cover-EM 算分（标准化后变空字符串） |

#### 选哪个

- **优先方案 A**：session 里这类问题你已经过了一遍，拆分顺手；信号最细。
- **选方案 B 仅当**：用户真的爱问宽泛"步骤"问题（占比 >20%）想保留这一类，**或**你想专门测系统"能否同时召回多个相关 chunk 到 top-5"这个能力。
- 选 A 时每条 1 chunk；选 B 时 1-3 chunk 都合理（PLAN 推荐上限）。

### Q5：中英文混合怎么处理？我有些 query 中文有些英文

**中英文都要有，但每条样本内部 query 与 gold_answer 语言必须一致。**

#### 比例建议

**核心原则：评测集语言镜像生产环境**。判断依据**先看 KB 文档语言，再看用户实际问法**。

| KB 文档语言 | 评测集语言 | 举例（本项目） |
|---|---|---|
| **纯中文** | 100% 中文 | `ifs_docs`：培训手册纯中文，连专有名词都是中文（如"客户订单"），用户也只用中文问 → 评测 0 英文 |
| **中文 + 英文专有名词夹杂** | 中文为主 + 少量纯英文 query 测系统对英文短语的鲁棒性 | `agv_demo`：中文 SOP 里夹 `E-Stop Button Active`、`Master Link Down`、`Automatic Mode` 等英文告警/按钮名 → 中:英 ≈ 5:5 都合理 |
| **中英文档并存** | 按文档比例配 | 实际项目少见 |
| **纯英文** | 100% 英文 | — |

⚠️ **不要硬凑英文样本**：如果 KB 文档全中文、用户也只用中文问，加英文 query 测的是"系统对生产里不会发生的输入的反应"——分数好看与否，做出来的决策（要不要上 BM25、要不要调 chunk size）都基于不存在的场景。

#### 怎么判断 KB 是否需要英文样本

打开 `data/kb/<kb>/corpora/chunks.jsonl` 翻几个 chunk，看 `contents`：

- 全中文（含标点）→ 评测 100% 中文
- 出现英文术语、按钮名、告警代码 → 评测加几条英文 query 测这些术语

#### 语言一致性（F1 算分前提）

每条样本内部 query 和 gold_answer 必须同语言，否则 token-level F1 直接 0：

```jsonl
✅ {"query": "换电池第一步是什么？",         "gold_answer": "按 7 号键导航到空电池仓"}
✅ {"query": "How to fix E-Stop alarm?",     "gold_answer": "Check both E-stop buttons, rotate clockwise"}
❌ {"query": "换电池第一步是什么？",         "gold_answer": "Press 7 to navigate..."}     ← F1=0
❌ {"query": "What's step 1?",               "gold_answer": "按 7 号键..."}              ← F1=0
```

**例外允许夹术语**：中文 query/gold_answer 里夹英文专有名词（"Master Link Down 怎么消除？" → "等 AGV 重连系统后 Master Link Down 状态自动清除"）—— 这是真实的中文运维场景，反而**应该这样写**，让评测能测出系统对中英混合的处理能力。

#### 改写 raw 候选时的实操

如果你从 `<kb>_raw.jsonl` 里挑了一条中文 query，但写 gold_answer 时只想得到英文表述（比如照抄原文档），有两种选择：

- **保留中文 query → 把 gold_answer 翻成中文关键词清单**：保留原始 session 口吻，更贴近生产
- **改 query 为英文 → 保留英文 gold_answer**：把这条算到"英文 query 配额"里，source 改为 manual（因为已偏离 session 原文）
