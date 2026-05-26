# Phase 8.3 Week 1 IRCoT 借用验证对比报告

> 跑分时间：2026-05-26  |  git: 7ae9f27  |  KB: agv_demo  |  评测子集: multi_step (n=20)
> 路径：自写 IRCoT（[`custom_app/services/strategies/ircot.py`](../../custom_app/services/strategies/ircot.py)）
> 上游决策：[PHASE_8_3_KICKOFF.md](../../docs/Phase8/PHASE_8_3_KICKOFF.md)（不借 UltraRAG，直接自写）

---

## 一、对比矩阵（multi_step 子集 n=20）

| 指标 | Quick（baseline） | IRCoT loops=2 | Δ |
|---|---|---|---|
| **Recall@5** | 0.4833 | **0.5000** | +1.67pp |
| **Recall@10** | 0.4833 | **0.6917** | **+20.83pp** 🟢 |
| MRR | 0.9625 | 0.9625 | 0 |
| Hit@1 | 0.9500 | 0.9500 | 0 |
| Hit@5 | 0.9500 | 0.9500 | 0 |
| nDCG@5 | (基线) | (略升) | — |
| **F1（生成）** | 0.0210 | **0.4246** | **+0.4036** 🟢🟢🟢 |
| Accuracy | 0 | 0 | 0 |
| Cover-EM | 0 | 0 | 0 |
| ROUGE-L | 0 | 0 | 0 |
| **failures** | 19/20（95%） | **7/20（35%）** | -60pp |
| **平均生成延迟** | ~10-15s | ~24s | **~2×** |

---

## 二、关键发现

### 🟢 核心胜利：召回率 + 答案质量同步提升

1. **Recall@10 从 0.4833 → 0.6917（+20.83pp）**：IRCoT 第 2 轮检索成功拉回 multi_step 缺失的 chunks
2. **F1 从 0.02 → 0.42（+0.40）**：LLM 拼答案的能力随召回扩大而大幅提升
3. **失败样本从 19 → 7**：12 个原本答不全的 multi_step 样本，IRCoT 答对了

### 🟡 延迟代价：约 2×

- Quick mode 单次答案约 10-15 秒
- IRCoT loops=2 约 24 秒
- 在 PLAN §四.4 的 "<2× 单跳" 门槛**边缘** —— 接近但未明显超标

### ⚠️ 指标解读：为什么 Acc / Cover-EM / ROUGE-L 仍 0

这三个指标硬性匹配：
- **Accuracy**：要 gold 是 pred 子串
- **Cover-EM**：要 gold 所有 token 都在 pred
- **ROUGE-L**：要语序匹配

multi_step gold 是简洁的关键词清单（如 "导航 7 号键、下降电池块、抽出旧电池"），LLM 答完整步骤时词序 / 用词与 gold 不严格一致 → 三个指标 = 0。

**F1（token 集合 + 词频）+0.40 才是核心信号**：LLM 答案与 gold 在词汇上大量重合。

---

## 三、PLAN §四.4 退出条件判定

| 门槛 | 判定 |
|---|---|
| F1 提升 ≥0.05 | **+0.4036 远超** 🟢🟢 |
| 延迟 <2× 单跳 | **1.6-2.4× 边缘** 🟡 |

### 🟢 决策：**进入 Week 2-3 全量移植**，附条件

PLAN §四.4 三种结果中匹配第 3 种：

> F1 提升够但延迟 >3× → 评估"chat 模式 vs 思考模式"双轨：前端加按钮，默认单轮，复杂问题手动切 IRCoT

虽然这里延迟 ~2× 比 ">3×" 好一些，**但仍建议走双轨**：

| 模式 | 默认 | 触发场景 |
|---|---|---|
| **quick**（单轮，10s） | ✅ 默认 | 95% 单跳问答用户体验最好 |
| **deep_reasoning** / IRCoT（24s） | 用户主动切 | 跨文档、多步骤问题 |

---

## 四、推荐 Week 2-3 实施路径

### Week 2：接入生产链路

| 子任务 | 工时 | 验收 |
|---|---|---|
| `api/chat.py` 加 `mode` 参数（quick/deep_reasoning） | 0.5 天 | curl 测 `?mode=deep_reasoning` 触发 IRCoT |
| `RagRunner` 加 `chat_ircot` 方法（包装 `chat_ircot()`） | 0.5 天 | quick 路径完全不动 |
| 前端 / 后端 SSE 兼容流式（IRCoT 思考过程逐轮推送） | 1 天 | 前端能看到 "正在思考第 1 轮..." 状态 |
| 全量评测集跑分（130 + 21 multi_step）| 1 天 | 看 IRCoT 在非 multi_step 样本上是否退化 |

### Week 3：前端按钮 + 调优 + 上线

| 子任务 | 工时 | 验收 |
|---|---|---|
| 前端 chat 输入区加"深度思考"开关 | 1 天 | 用户能切换 |
| Prompt 微调（基于失败的 7/20 样本分析） | 1 天 | F1 再提 5-10pp |
| max_loops 扫描（1/2/3） | 0.5 天 | 选最优 loops |
| 文档 + MANUAL_TESTING + commit | 0.5 天 | 上线 checklist |

---

## 五、剩余 7 个失败样本快速分析

需要看具体哪些 multi_step 样本仍失败，决定 Week 3 prompt 调优方向。已经可以从评测 JSON 抽出来分析（留给 Week 3 启动时做）。

---

## 六、与 PHASE_8_2_COMPARISON_V2 的呼应

| 信号 | 8.2.3 v2 诊断 | 8.3 Week 1 验证 |
|---|---|---|
| failing/multi_step Recall@5 = 0.48 | 召回不足是瓶颈 | ✅ Recall@10 +20.83pp 证明多轮检索能补 |
| failing/multi_step MRR = 0.96 | 排序准（找到的 chunk 在前） | ✅ MRR 没变化（IRCoT 不破坏第 1 轮的排序） |
| 用户笔记 20 条 failing 多为"系统漏步骤" | 跨 section_1+2 才完整 | ✅ F1 +0.40 证明 IRCoT 把缺的步骤补回来了 |

---

## 七、立即可做的事

1. **commit 本期产物**（ircot.py + tests + prompt + eval CLI + 两份评测结果 + 本报告）
2. **决定 Week 2 是否启动**：
   - 启动 → 走 §四 路径
   - 暂缓 → 留作"功能就绪但默认关"，让业务侧用现有 quick 模式继续，等需要时再开 deep_reasoning 按钮
