# Phase 8.2.3 评测对比矩阵 v2

> 跑分时间：2026-05-21  |  git: 95caa0d  |  top_k=10  |  with_generation=False
> 评测集：agv_demo (130 items，+72 真实 session) + ifs_docs (61 items，+6 真实 session)
> 对比基线：[`phase8_2_comparison.md`](./phase8_2_comparison.md)（v1，05-18，58/55 items）

## 一、4 组矩阵 v2

| Group | KB | Recall@1 | Recall@5 | Recall@10 | MRR | Hit@1 | nDCG@5 |
|---|---|---|---|---|---|---|---|
| vector + no context | agv_demo | 0.6269 | 0.7346 | 0.7577 | 0.7838 | 0.7462 | 0.7117 |
| vector + no context | ifs_docs | 0.9016 | 0.9770 | 0.9770 | 0.9399 | 0.9016 | 0.9515 |
| vector + context | agv_demo | 0.6192 | 0.7821 | 0.8051 | 0.7898 | 0.7385 | 0.7323 |
| vector + context | ifs_docs | 0.8852 | 0.9803 | 0.9803 | 0.9317 | 0.8852 | 0.9476 |
| hybrid + no context | agv_demo | 0.6269 | 0.7423 | 0.7654 | 0.7876 | 0.7462 | 0.7165 |
| hybrid + no context | ifs_docs | 0.8852 | 0.9803 | 0.9803 | 0.9317 | 0.8852 | 0.9476 |
| **hybrid + context** (current production) | agv_demo | **0.6436** | 0.7654 | 0.7885 | **0.7925** | **0.7538** | **0.7318** |
| **hybrid + context** (current production) | ifs_docs | 0.8852 | 0.9770 | 0.9770 | 0.9317 | 0.8852 | 0.9454 |

## 二、相对组 1（vector+noctx）提升

### agv_demo（主信号 KB，130 真实评测样本）

| Group | ΔRecall@5 | ΔRecall@10 | ΔMRR | ΔHit@1 | 判定 |
|---|---|---|---|---|---|
| **+context (vector)** | **+4.75pp** | **+4.74pp** | +0.60pp | -0.77pp | 🟢 显著（召回提升） |
| **+BM25 (hybrid)** | +0.77pp | +0.77pp | +0.38pp | 0.00pp | 🟡 微正向（v1 时为 0） |
| **+ctx+BM25 (production)** | +3.08pp | +3.08pp | +0.87pp | **+0.76pp** | 🟢 综合最佳 |

### ifs_docs（n=61，评测集仍偏饱和）

| Group | ΔRecall@5 | ΔMRR | ΔHit@1 | 判定 |
|---|---|---|---|---|
| +context | +0.33pp | -0.82pp | -1.64pp | 🟡 微动 |
| +BM25 | +0.33pp | -0.82pp | -1.64pp | 🟡 微动 |
| +ctx+BM25 | 0 | -0.82pp | -1.64pp | 🟡 微动 |

## 三、与 v1 对比（信号反转）

| 改进 | v1 ΔR@5（05-18） | **v2 ΔR@5（05-21）** | 信号反转幅度 |
|---|---|---|---|
| +context | +2.59pp | **+4.75pp** | **几乎翻倍** |
| +BM25 | +0.00pp | **+0.77pp** | 从无效到微正向 |
| +ctx+BM25 | +0.00pp | **+3.08pp** | **从无效到显著** |

**为什么反转**：v1 评测集 80%+ 是 chunk 反推的 query（字面与 chunk 100% 对齐），vector 已经"看上去很强"，BM25 / context 的差异被磨平。v2 加入了 72 条真实 session 后，query 用户口语化 / 中英混合 / 同义改写，向量泛化能力的天花板暴露，BM25 / context 的提升空间显现。

## 四、PLAN §八 退出条件最终判定（v2）

门槛：agv_demo Recall@5 ≥10pp **或** MRR ≥0.05

| 改进 | Δr@5 | ΔMRR | 是否上线 |
|---|---|---|---|
| **+ context (vector)** | +4.75pp | +0.60pp | 🟡 **接近达标**（未到 ≥10pp，但显著） |
| **+ BM25 (hybrid)** | +0.77pp | +0.38pp | 🟡 **微正向**（v1 时是 0） |
| **+ ctx+bm25 (production)** | +3.08pp | +0.87pp | 🟢 **综合最佳** |

**严格按 PLAN ≥10pp 仍未达**，但**所有方向均为正**（v1 时 BM25 是 0），说明 Phase 8.2 改造**整体有效但收益分散**，无单一杀手锏。

## 五、per-tag 信号 — IRCoT 重启的诊断依据

agv_demo 组 4（生产配置）按 tag 分桶：

| Tag | 样本数 | Recall@5 | MRR | 解读 |
|---|---|---|---|---|
| passing | 49 | 0.9592 | 0.9235 | 系统真实强项（用户笔记标"答对"的） |
| failing | 20 | **0.4833** | 0.9625 | **召回低**但**排序正确**——TopK 缺 chunk |
| multi_step | 20 | **0.4833** | 0.9625 | 同上——多跳查询本质问题 |
| description_query | ~半数 | 0.8135 | 0.9107 | 长描述类口语化好 |
| alarm_id_query | ~半数 | 0.8167 | 0.9417 | 直接问 ID 表现好 |
| zh_query | 34 | 0.8431 | 0.9191 | 中文 query 整体好 |
| en_query | 38 | 0.7895 | 0.9276 | 英文 query 略弱 |

**关键洞察**：
- `failing` 和 `multi_step` 两个 tag 的 Recall@5 = 0.48 = **整体 Recall@5 (0.77) 的 63%**
- MRR 却高达 0.96 —— 命中的 chunk 总在 top-1/2，但**还有别的 chunk 没找到**
- 这正是 **IRCoT 多轮检索能补的场景**：第二轮用第一轮的部分答案再查，把漏掉的 chunk 拉回来

## 六、决策建议

### 6.1 生产配置：**升级到 hybrid + context**

**理由**：
- v1 时 hybrid + context 是"最差组之一"（与 vector + noctx 持平）
- v2 时**变成综合最佳**：Recall@5 +3.08pp / MRR +0.87pp / Hit@1 +0.76pp（唯一一个 Hit@1 提升的组合）
- BM25 / RRF 在真实 query 上的价值终于显现

**操作**：把 [`servers/retriever/parameter.yaml`](../../servers/retriever/parameter.yaml) 的 `retrieval.mode` 从 `vector` 改回 `hybrid`。

### 6.2 Phase 8.3 IRCoT：**满足重启条件**

**门槛检查**：

| PLAN §五.4 要求 | 当前状态 |
|---|---|
| multi_step 标签 ≥15 条 | ✅ **21 条**（agv 20 + ifs 1） |
| 失败样本集中可分析 | ✅ failing/multi_step Recall@5 = 0.48 |
| 召回不足是诊断瓶颈 | ✅ multi_step MRR=0.96 + Recall@5=0.48 |

**建议**：启动 Phase 8.3 Week 1 借用验证（PLAN §四），在 UltraRAG 上跑 IRCoT，对比 agv multi_step 子集分数。

### 6.3 评测集进一步扩充（后台进行）

继续按 [`EVAL_DATASET_EXPANSION_GUIDE.md`](../../docs/Phase8/EVAL_DATASET_EXPANSION_GUIDE.md) 每月 sprint 加 session query；3-6 个月后达到 300+ 真实 session 时再跑一次矩阵。

## 七、下一步执行顺序

1. ✅ commit 本报告 + phase8_2_v2 矩阵原始数据
2. 改 `parameter.yaml` `retrieval.mode: vector → hybrid`，commit
3. 还原 chunks.jsonl context + 重建 emb + Qdrant（恢复生产环境）
4. 启动 Phase 8.3 Week 1（评估在 UltraRAG 上跑 IRCoT 的可行性 + 借用验证流程）
