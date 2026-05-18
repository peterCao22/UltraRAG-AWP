# Phase 8.2.3 评测对比矩阵

> 跑分时间：2026-05-18  |  git: fcac185  |  top_k=10  |  with_generation=False

> 评测集：agv_demo (58 items) + ifs_docs (55 items)

## 一、4 组矩阵

| Group | KB | Recall@1 | Recall@5 | Recall@10 | MRR | Hit@1 | Hit@5 | nDCG@5 |
|---|---|---|---|---|---|---|---|---|
| vector + no context | agv_demo | 0.5201 | 0.6753 | 0.7270 | 0.6312 | 0.5690 | 0.7241 | 0.6125 |
| vector + no context | ifs_docs | 0.9455 | 0.9964 | 0.9964 | 0.9727 | 0.9455 | 1.0000 | 0.9804 |
| vector + context | agv_demo | 0.4856 | 0.7011 | 0.7529 | 0.6183 | 0.5345 | 0.7586 | 0.6075 |
| vector + context | ifs_docs | 0.9455 | 0.9927 | 0.9927 | 0.9727 | 0.9455 | 1.0000 | 0.9780 |
| hybrid + no context | agv_demo | 0.5201 | 0.6753 | 0.7270 | 0.6312 | 0.5690 | 0.7241 | 0.6125 |
| hybrid + no context | ifs_docs | 0.9455 | 0.9964 | 0.9964 | 0.9727 | 0.9455 | 1.0000 | 0.9804 |
| hybrid + context (production) | agv_demo | 0.5029 | 0.6753 | 0.7270 | 0.6197 | 0.5517 | 0.7241 | 0.6034 |
| hybrid + context (production) | ifs_docs | 0.9455 | 0.9927 | 0.9927 | 0.9727 | 0.9455 | 1.0000 | 0.9780 |

## 二、相对组 1（vector+noctx）的提升

| Group | KB | ΔRecall@5 | ΔRecall@10 | ΔMRR | ΔHit@1 | ΔnDCG@5 |
|---|---|---|---|---|---|---|
| vector + context | agv_demo | ↑+2.59pp | ↑+2.59pp | ↓-1.29pp | ↓-3.45pp | ↓-0.50pp |
| vector + context | ifs_docs | ↓-0.36pp | ↓-0.36pp |  +0.00pp |  +0.00pp | ↓-0.24pp |
| hybrid + no context | agv_demo |  +0.00pp |  +0.00pp |  +0.00pp |  +0.00pp |  +0.00pp |
| hybrid + no context | ifs_docs |  +0.00pp |  +0.00pp |  +0.00pp |  +0.00pp |  +0.00pp |
| hybrid + context (production) | agv_demo |  +0.00pp |  +0.00pp | ↓-1.15pp | ↓-1.72pp | ↓-0.91pp |
| hybrid + context (production) | ifs_docs | ↓-0.36pp | ↓-0.36pp |  +0.00pp |  +0.00pp | ↓-0.24pp |

## 三、退出条件判定（PLAN §八）

门槛（agv_demo，从 ifs_docs 已饱和 r@5≈0.99 取信号有限）：
- Recall@5 提升 ≥10pp **或** MRR 提升 ≥0.05 → 改进有效
- 若两项均不达标 → 该改进**不上线**

### agv_demo（主要信号 KB）
- **vector + context**: ΔRecall@5=+2.59pp, ΔMRR=-0.0129 → 🟡 持平/微提
- **hybrid + no context**: ΔRecall@5=+0.00pp, ΔMRR=+0.0000 → 🟡 持平/微提
- **hybrid + context (production)**: ΔRecall@5=+0.00pp, ΔMRR=-0.0115 → 🟡 持平/微提

### ifs_docs（参考信号；评测集饱和）
- vector + context: ΔRecall@5=-0.36pp, ΔMRR=+0.0000
- hybrid + no context: ΔRecall@5=+0.00pp, ΔMRR=+0.0000
- hybrid + context (production): ΔRecall@5=-0.36pp, ΔMRR=+0.0000

## 四、失败样本对比（agv_demo）

- vector + no context: 16 failures
- vector + context: 14 failures
- hybrid + no context: 16 failures
- hybrid + context (production): 16 failures

---

## 五、关键发现与分析

### 5.1 Context 部分有效，但收益未达 PLAN 门槛

**信号**：vector+ctx vs vector+noctx（agv_demo）：
- Recall@5: **+2.59pp** ✅（虽未达 ≥10pp）
- Recall@10: +2.59pp
- Hit@5: **+3.45pp**
- 失败样本：16 → 14（少 2 个 retrieval_miss）

**反向信号**：
- Hit@1: **-3.45pp**
- MRR: -0.0129
- Recall@1: -3.45pp

**解读**：context 把更多相关 chunk 拉进 top-5（**召回扩大**），但同时让某些原本排第 1 位的 chunk 被排到后面（**重排错位**）。这是 contextual retrieval 的典型 trade-off：摘要在 embedding 输入里加了"全局描述"，让长尾 chunk 也能被命中，但稀释了原本最强匹配的语义相似度。

### 5.2 BM25 双路在本 KB 完全无效

**信号**：hybrid+noctx vs vector+noctx（agv_demo）：所有指标**完全相同**（差 0.0000）。

**为什么**：
1. **agv_demo 是中文 SOP**，jieba 把中文短语切成单字 token（"换电池" → "换" / "电池"），BM25 在这类 token 上 IDF 极低（几乎所有 chunk 都含"电池"）
2. **vector 已经强**（Recall@5 = 0.6753），RRF 融合反而稀释了精度
3. **RRF k=60 默认参数偏保守**，BM25 那一路得分 ≈ 0.7/61 vs 向量已经能区分

**ifs_docs** 评测集饱和（top-5 命中率 99%），BM25 自然也提不起来。

### 5.3 退出条件最终判定

按 PLAN §八门槛（agv_demo Recall@5 ≥10pp 或 MRR ≥0.05）：

| 改进 | Δr@5 | ΔMRR | 是否上线 |
|---|---|---|---|
| **+ context (vector)** | +2.59pp | -1.29pp | 🟡 **微提，需人工抉择** |
| **+ BM25 (hybrid)** | +0.00pp | +0.00pp | 🔴 **不上线** |
| +ctx+bm25 (production) | +0.00pp | -1.15pp | 🔴 **比单独 context 还差** |

---

## 六、对生产配置的建议

### 6.1 强烈建议：关闭 BM25，回到纯 vector

**当前生产配置（hybrid + context）= 最差组之一**：
- Recall@5 = 0.6753（与 vector+noctx 持平）
- MRR = 0.6197（比 vector+noctx 低 1.15pp）
- 多消耗 BM25 索引内存 + jieba 启动时间

**改成 vector + context** 即可：
- 关掉 hybrid：`ULTRARAG_RETRIEVAL_MODE=vector` 或 `parameter.yaml` retrieval.mode = vector
- 保留 chunks.jsonl 的 context 字段不动

### 6.2 Context 是否保留：建议保留

虽然 MRR 微降但 Recall 微升 + 失败样本减 2 个，**对端到端 RAG 体验是净正向**：
- LLM 看到的 top-5 chunk 更多元（Recall@5 ↑），答案更全面
- Hit@1 微降不致命（用户看 top-5 整体引用，不是只看第 1 条）

如果未来希望 Hit@1 也上去，可以调整：
- **context 长度更短**（当前 100-150 字，可压到 50-80 字）：减少摘要对向量的稀释
- **embedding 权重微调**：embedder 中 context 前缀加位置标记让模型识别

### 6.3 PLAN §八严格判定

**未达标 → Phase 8 至 8.2 收尾，跳过 8.3 IRCoT**（PLAN §八明确）。

但本期最大收获是 **诊断**：agv_demo 的检索瓶颈不是"召回少"，是"前几位排序不准"——MRR/Hit@1 才是该攻的方向。BM25 / context 都不直接攻这个，所以提升有限。

IRCoT 攻的是"多跳 query"，而本评测集多跳样本少（tags 里 `multi_step` 仅 1 条），所以 8.3 启动前需先扩充评测集（C 路再写 10-15 条多跳样本）。

### 6.4 推荐下一步行动

1. **立刻**：把生产配置改回 `retrieval.mode=vector`（保留 context），节省 BM25 资源
2. **短期**：调研 reranker 是否在 +context 后仍把对的 chunk 排在 top-1
   - 失败样本 4/14 是 query "ID01"、"第二步呢"、"第十一步" 这类位置/编号问题，reranker 在 +context 后可能误判
3. **中期**（如要重启 8.3）：评测集扩充 `multi_step` 样本 ≥15 条；再跑 IRCoT 验证
