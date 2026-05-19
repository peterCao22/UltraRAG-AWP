# 评测集扩充作业指南

> 整理时间：2026-05-19
> 适用：[`data/eval/agv_demo.jsonl`](../../data/eval/agv_demo.jsonl)（当前 58 条）+ [`data/eval/ifs_docs.jsonl`](../../data/eval/ifs_docs.jsonl)（当前 55 条）
> 目标：扩到 **每 KB ≥80 条 + multi_step 标签 ≥15 条**，让 Phase 8.3 重启 / Phase 11.1 调优时评测信号更稳定

---

## 一、为什么要扩充

[Phase 8 总结 §二 Q2.2](./PHASE_8_SUMMARY.md) 提到现有评测信号有偏：

- **agv_demo 58 条里只有 8 条来自真实 session**（占 14%），其他 50 条是从 chunk 内容反推编出来的 → query 与 chunk 字面词汇高度对齐 → BM25 / 检索的"难度差异"被磨平
- **ifs_docs 55 条全部 from_session**（早期标注阶段把 raw 直接当终版），但 raw 也是从 chunk 内容反推 → 同样的字面对齐问题
- **没有 `multi_step` 标签**（PLAN §五.4 期望 ≥15 条，IRCoT 重启的前置条件）

修正这两点后，下次跑 baseline 才能反映真实生产场景的检索质量。

---

## 二、扩充目标（明确数字）

| KB | 当前 | 目标增量 | 目标终值 |
|---|---|---|---|
| agv_demo | 58 条（含 8 from_session） | +25 条真实 session + 10 条 multi_step | ≥83 条，其中 ≥30 from_session、≥10 multi_step |
| ifs_docs | 55 条（全 from_session 但来源 chunk 反推） | +15 条真实 session + 5 条 multi_step | ≥70 条，其中 ≥15 真 session、≥5 multi_step |
| **multi_step 合计** | **0** | **+15 条以上** | **≥15 条**（IRCoT 重启前置） |

---

## 三、A 路扩充 —— 从真实 session 抽取（每周 20 分钟）

### 3.1 当前 session 数据量

```
agv_demo:  17 sessions / 35 user msgs
ifs_docs:  10 sessions / 21 user msgs
```

只够覆盖几个月真实用户行为。**每周或每月跑一次 extract，把新增 user query 加入评测集**是最高 ROI 的做法。

### 3.2 一次抽取流程（30-60 分钟）

#### Step 1：自动抽取 + 去重

```powershell
.venv\Scripts\python.exe -m custom_app.scripts.extract_eval_queries `
    --kb agv_demo `
    --output data\eval\agv_demo_session_new.jsonl

.venv\Scripts\python.exe -m custom_app.scripts.extract_eval_queries `
    --kb ifs_docs `
    --output data\eval\ifs_docs_session_new.jsonl
```

脚本自动：
- 从 `kb_session_messages` 抽 `role='user'` 的消息
- 按 [`_normalize_query`](../../custom_app/scripts/extract_eval_queries.py#L38)（小写 + 去标点 + 去空白）去重
- 过滤 4-300 字之外的长度异常
- 输出含 query 但 `relevant_chunk_ids` 和 `gold_answer` 为空的候选 jsonl

#### Step 2：人工筛选 + 去掉与现有评测集重复的 query

```powershell
# 看新抽出多少条
type data\eval\agv_demo_session_new.jsonl | find /c /v ""

# 对照现有终版，剔除已有 query
.venv\Scripts\python.exe -c "import json; existing = set(json.loads(l)['query'].strip() for l in open('data/eval/agv_demo.jsonl','r',encoding='utf-8') if l.strip()); rows = [json.loads(l) for l in open('data/eval/agv_demo_session_new.jsonl','r',encoding='utf-8') if l.strip()]; new_rows = [r for r in rows if r['query'].strip() not in existing]; print(f'抽到 {len(rows)} 条，去重后剩 {len(new_rows)} 条'); open('data/eval/agv_demo_session_new.jsonl','w',encoding='utf-8').writelines(json.dumps(r, ensure_ascii=False)+'\n' for r in new_rows)"
```

#### Step 3：业务侧标注（VSCode 打开 `_session_new.jsonl`）

每条样本要补：

1. **`relevant_chunk_ids`**：去 [`data/kb/<kb>/corpora/chunks.jsonl`](../../data/kb/agv_demo/corpora/chunks.jsonl) 找最相关的 1-3 个 chunk_id
2. **`gold_answer`**：从 chunk 内容里精简成 ≤150 字的关键词清单 / 简短描述
3. **`tags`**：加上恰当分桶标签（见 [`data/eval/README.md`](../../data/eval/README.md) §四）

样例：

```jsonl
{"id": "eval_agv_demo_new_001", "kb_id": "agv_demo", "query": "AGV 启动不了，黄灯一直闪怎么办？", "relevant_chunk_ids": ["E-Stop SOP_intro"], "gold_answer": "检查急停按钮是否被按下；按下后顺时针旋转复位", "tags": ["from_session", "error_diagnosis", "alarm_id"], "source": "session"}
```

#### Step 4：合并进终版 + schema 校验

```powershell
type data\eval\agv_demo.jsonl, data\eval\agv_demo_session_new.jsonl | Set-Content data\eval\agv_demo.jsonl

# 校验
.venv\Scripts\python.exe -c "from custom_app.services.eval.dataset import load_eval_dataset; from pathlib import Path; xs = load_eval_dataset(Path('data/eval/agv_demo.jsonl'), expected_kb_id='agv_demo'); print(f'OK: {len(xs)} items')"

# 校验通过后，清掉过程文件
del data\eval\agv_demo_session_new.jsonl
```

### 3.3 标注质量门槛

**保留**：
- 用户语气自然、能反映真实使用场景
- chunks.jsonl 里有对应 chunk 能答（哪怕只是部分覆盖）
- 中文专业术语 / 英文报错代码混用都好（如 "Master Link Down 怎么消除"）

**剔除**：
- "继续"、"好的"、"嗯？" 这类对话碎片
- 与已有评测样本同义重复（如已有 "AGV 换电池步骤"，再来 "换电池流程是什么" 直接剔除）
- 与 KB 内容完全无关的（如用户问"今天天气怎么样"）

**改写但保留 source=session**（合法）：
- 把口语化的稍微规范化：「换电池怎么搞」→「AGV 换电池的步骤」
- 把宽泛的拆成具体的：「换电池步骤」→「换电池第一步是什么」

---

## 四、multi_step 标签专项扩充 —— 业务手写（4-6 小时）

### 4.1 什么算 multi_step

判定准则：**单跳 RAG（query 直接命中 1 个 chunk → LLM 生成答案）无法答全，至少需要 2 个 chunk 拼凑或 2 轮推理**。

| query 类型 | 是否 multi_step | 例子 |
|---|---|---|
| 单步操作 | ❌ | "换电池第一步是什么？" |
| 单一告警处理 | ❌ | "E-Stop Button Active 怎么处理？" |
| **整套流程概述** | ✅ | "换电池一共多少步，每步分别做什么？" |
| **跨文档诊断** | ✅ | "AGV 报 ID 01，但已经按急停复位还是不动，怎么办？"（需查 E-Stop + Master Link Down 两个文档） |
| **因果推理** | ✅ | "为什么 STEP 5 完成后 AGV 还显示 Master Link Down？" |
| **多场景列举** | ✅ | "IFS 客户端登录失败的常见原因有哪些？"（涉及 500/404/-2146697211 三类） |
| **条件分支** | ✅ | "如果是 USB 故障 vs 急停按钮按下，处理步骤差别是什么？" |

### 4.2 标 multi_step 的样本必须满足

- `relevant_chunk_ids` 必须 ≥ 2 个**不同文档**或**同文档不同 step/section** 的 chunk
- `gold_answer` 要覆盖**至少 2 个核心要点**（每个要点来自一个 gold chunk）
- `tags` 必须含 `multi_step`，还可以加 `cross_doc` / `causal` / `enumeration` 等子分类

### 4.3 写 10-15 条的方法

由业务侧主导（你最了解真实运维场景）：

#### 方法 A：从历史故障复盘里翻

业务侧有"AGV 故障处理记录"、"IFS 升级失败案例"之类的内部文档？把里面**真实排查过程**抽出来当 multi_step 样本：

```
真实故障 → "AGV 换电池后启动报 PLS 异常"
排查过程 → 1. 查电池是否装到位（BatteryChange_step_8）
          2. 查 Front PLS 是否被电池遮挡（Front PLS SOP）
          3. 清理后按蓝绿按钮复位
↓
multi_step 评测样本：
{"query": "AGV 换电池后启动报 Front PLS 异常",
 "relevant_chunk_ids": ["BatteryChangeSequenceSOP_step_8", "Front PLS SOP_intro"],
 "gold_answer": "电池可能没装到位遮挡了 Front PLS。先确认电池块归位，清理遮挡，再按蓝绿按钮复位",
 "tags": ["multi_step", "cross_doc", "error_diagnosis"],
 "source": "manual"}
```

#### 方法 B：把现有单跳样本组合

从 `agv_demo.jsonl` 里挑 2-3 个相关的单跳样本，**组合提问**：

```
单跳 1：换电池一共多少步？
单跳 2：换电池后报 Master Link Down 怎么办？
↓ 组合
multi_step：完整换电池后如果系统连不上，怎么排查？
gold = step_intro + step_11 + Master_Link_Down_SOP_section_1 三个 chunk
```

#### 方法 C：用 LLM 辅助（但要严格人工筛）

把 chunks.jsonl 的 2-3 个相关 chunk 喂给 Gemini，让它编"需要跨这些 chunk 才能答"的问题：

```powershell
# 当前 generate_eval_queries.py 是单 chunk 编题；写一个 multi_step 版本
# 暂时不实现脚本，先用 ChatGPT/Claude 网页版手工跑：
#   prompt = "我会给你 3 个 SOP 片段，请你编 1 个用户问题，
#            必须同时引用所有 3 个片段的内容才能答对"
```

**人工筛保留率 30-50%**（比单跳生成低，因为 LLM 容易把多跳编成"伪多跳"——表面引用多个片段，实际单跳就能答）。

### 4.4 标注质量自检

每条 multi_step 样本写完，问自己：

1. **"如果检索只命中 gold_chunks 里的 1 个，LLM 能完整答出 gold_answer 吗？"**
   - 能 → 这不是 multi_step，删 `multi_step` 标签
   - 不能 → 真 multi_step ✅
2. **"gold_answer 里每个要点能在 gold_chunks 里找到对应文字吗？"**
   - 全能找到 → ✅
   - 有 LLM 编造的成分 → 重写 gold_answer

---

## 五、提交节奏建议

### 短期（1-2 周内）

| 任务 | 工时 | 负责 |
|---|---|---|
| 跑 extract_eval_queries 一遍 → 抽 agv_demo / ifs_docs 新 session query | 30 分钟 | 我（RAG 侧） |
| 业务侧人工标注 A 路新增样本（每 KB 15-25 条） | 4-6 小时 | 业务侧 |
| 业务侧手写 multi_step 样本 10-15 条 | 4-6 小时 | 业务侧 |
| schema 校验 + 合并进 `<kb>.jsonl` + 重跑 baseline | 1 小时 | 我 |

### 长期（持续）

**每月最后一周**作为"评测集 sprint"：
1. 跑 extract 把当月新增 session query 抽出来
2. 业务侧 1-2 小时标注
3. 合并 + 重跑 baseline + git commit

3-6 个月后评测集自然增长到 200+ 条/KB，覆盖度足够再启动 Phase 8.3 IRCoT 验证。

---

## 六、扩充后立即可做的事情

1. **重跑 baseline**：
   ```powershell
   .venv\Scripts\python.exe -m custom_app.scripts.eval_custom_app --kb agv_demo --save-baseline
   .venv\Scripts\python.exe -m custom_app.scripts.eval_custom_app --kb ifs_docs --save-baseline
   ```
   对比 [`data/eval/baseline/agv_demo_2026-05-19.json`](../../data/eval/baseline/agv_demo_2026-05-19.json) 看分数走向（预期 Recall@5 会下降 5-10pp，因为真实 query 比反推 query 难）

2. **multi_step ≥15 时重启 Phase 8.3 Week 1 借用验证**（PLAN §八门槛）

3. **跑 8.2.3 矩阵 4 组对比**（看 BM25 是否在真实 query 上有效）：
   ```powershell
   # 临时关闭 context、切 vector/hybrid 跑 4 组，参考 docs/Phase8/PHASE_8_2_PLAN.md §十 流程
   ```

---

## 七、不要做的事情

### 7.1 不要让 Gemini 一次性编 50 条 multi_step

LLM 编出来的"多跳"往往是伪多跳。具体表现：
- 表面引用 3 个 chunk，但实际单跳就答得出（因为同义复述）
- 编成"复合问题"（A 问题 + B 问题），不是真正的依赖关系
- 标签是 multi_step，但实际 single-step 就 PASS

**人工写质量 > 量产数量**。15 条真 multi_step 价值远大于 50 条伪 multi_step。

### 7.2 不要把评测集 query 与 chunk 内容 100% 对齐

之前用 generate_eval_queries.py 编的样本陷阱：

```jsonl
{"query": "Customer Order 状态变为 Reserved 是什么时候？",
 "relevant_chunk_ids": ["IFS_section_3"],
 "gold_answer": "下达订单后状态变为 Reserved"}
```

`Reserved` 在 query 和 gold 里都直接出现，BM25 字面命中 100%，向量也命中 100%。**这种样本测不出检索质量差异**。

应改写为：

```jsonl
{"query": "客户订单下达之后状态会变成什么？",
 "relevant_chunk_ids": ["IFS_section_3"],
 "gold_answer": "Reserved（已预留）"}
```

query 里没出现"Reserved"，迫使检索必须从语义层面理解"下达之后" ↔ "release" ↔ "Reserved" 的关系。

### 7.3 不要标超过 3 个 chunk

[`data/eval/README.md`](../../data/eval/README.md) §Q3 已经说过：标越多，分母越大，Recall 满分越难拿。multi_step 样本 2-3 chunk 就够，4 个以上拆成多个独立样本。

---

## 八、参考资料

- [data/eval/README.md](../../data/eval/README.md)：基础标注指南（已有 5 个 FAQ）
- [docs/Phase8/PHASE_8_1_PLAN.md](./PHASE_8_1_PLAN.md) §三：评测集格式 schema
- [docs/Phase8/PHASE_8_2_PLAN.md](./PHASE_8_2_PLAN.md) §八：8.2.3 评测对比退出条件
- [docs/Phase8/PHASE_8_SUMMARY.md](./PHASE_8_SUMMARY.md) §二 Q2.2：为什么现有评测信号偏
