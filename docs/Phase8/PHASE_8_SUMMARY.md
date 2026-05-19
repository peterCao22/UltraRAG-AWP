# Phase 8 完成情况总结

> 总结时间：2026-05-19
> 状态：8.0 / 8.1 / 8.2 全部完成；8.3 按 PLAN §八门槛跳过
> 涵盖 6 个 git commit：`eb34572`(路线图) → `cf2a75b`(8.0) → `3b8e7de`(8.1 脚手架) → `fcac185`(8.2 工程) → `d373ea3`(8.1+8.2 评测) → `92dd04e`(手工验收 + bug 修复)

---

## 一、Phase 8 各子阶段结论

### 8.0 兜底滑窗切分（_window_N）

✅ **已上线**。改造 [`docx_parser.py`](../../custom_app/services/docx_parser.py) 的 finalize 段：

| 文档类型 | 切分方式 | chunk_id 前缀 |
|---|---|---|
| 含 `STEP N:` 标记 | 按 STEP 切 | `_step_N` |
| 含 Heading 1/2/3 或全加粗短行 | 按 Heading 切 | `_section_N` |
| **无 STEP 无 Heading + 整篇 ≥ 800 字** | **按段落边界滑窗** | **`_window_N`** ← 8.0 新增 |
| 无 STEP 无 Heading + 整篇 < 800 字 | 单 chunk | `_intro` |

参数：`size=800` / `overlap=100` 字符（写死常量；评测后再决定是否调）。
向后兼容：现有 agv_demo / ifs_docs 重 ingest，56+16 chunk id 完全保留。

### 8.1 离线评测体系

✅ **已上线**。从 UltraRAG `evaluation.py` 剥离了 8 个生成指标到 `custom_app/services/eval/metrics.py`（0 行 ultrarag import），并自写 4 个检索指标。

**评测集**：
- [data/eval/agv_demo.jsonl](../../data/eval/agv_demo.jsonl) — 58 条（30 中文 + 28 英文）
- [data/eval/ifs_docs.jsonl](../../data/eval/ifs_docs.jsonl) — 55 条（全中文）

**基线分数**（2026-05-19）：

| KB | n_chunks | Recall@5 | MRR | Hit@1 | failures |
|---|---|---|---|---|---|
| agv_demo | 56 | 0.6753 | 0.6197 | 0.5517 | 16/58 |
| ifs_docs | 16 | 0.9927 | 0.9727 | 0.9455 | 1/55 |

### 8.2 Contextual + BM25 双路

✅ **工程已上线**，✅ **Contextual 保留**，🔴 **BM25 决策关闭**：

| 改进 | agv_demo ΔRecall@5 | ΔMRR | 决策 |
|---|---|---|---|
| +context | +2.59pp | -1.29pp | 🟡 **保留**（召回扩大，做净正向） |
| +BM25 | +0.00pp | +0.00pp | 🔴 **关闭**（中文场景 IDF 偏低无效） |
| +context+BM25 | +0.00pp | -1.15pp | 🔴 比单独 context 更差 |

**生产配置**：`servers/retriever/parameter.yaml` 设 `retrieval.mode: vector`，BM25 代码留作可选（env 切换）。

### 8.3 IRCoT 移植

🔴 **按 PLAN §八跳过**。退出条件门槛：Recall@5 提升 ≥10pp 或 MRR 提升 ≥0.05；8.2 评测显示主要瓶颈不是召回少而是排序错位，IRCoT 不直接攻这个方向。重启前需扩充 `multi_step` 标签样本 ≥15 条。

---

## 二、你的疑问逐条回答

### Q1：8.0 兜底滑窗主要用在通用文档？默认 800 字切块？

**对，但要更精确**：

- 兜底滑窗**只在两个条件同时满足**时触发：
  1. 文档**既无 STEP 又无 Heading**
  2. 整篇字符数 **≥ 800**

- 对当前的 agv_demo / ifs_docs **不触发** —— 它们都是规范化 SOP（有 STEP 或 Heading）。
- 未来上传通用类文档（FAQ 汇编、培训散文、用户上传的非标 SOP），如果整篇 ≥ 800 字，自动按 ~800 字 / 100 字 overlap 切多块 `_window_N`。
- < 800 字短文档仍按单 `_intro` 处理。

写死参数的原因：第一版评测体系尚未覆盖兜底文档，调参缺数据；下期评测加入此类文档后再调（或迁移到 yaml 可配置）。

---

### Q2.1：chunks.jsonl 在 RAG 检索流程中的角色？为什么需要它？

**chunks.jsonl 是 chunk 元数据查询表**，与 Qdrant 向量库职责分工：

| 组件 | 存什么 | 用途 |
|---|---|---|
| **Qdrant collection** | `chunk_id → embedding 向量 + 最小 payload`（kb_id / doc / title） | **检索**：用 query 向量找 top-k chunk_id |
| **chunks.jsonl** | `chunk_id → 完整元数据`（**contents / images / heading_path / context / structure / source_type / parser / tables** 等） | **回填**：拿到 chunk_id 后查完整内容，构造 prompt + sources + 插图 |

**为什么这样设计**：

1. **向量库不存大字段**：Qdrant payload 存全文 + 图片 + 上下文太重，影响检索性能
2. **重建索引时只需保留 chunks.jsonl**：embedding.npy + Qdrant 都可以从 chunks.jsonl 重新构造
3. **运行时数据访问 O(1)**：chunks.jsonl 在 `RagRunner.init()` 时全量加载到 `self._rows`，后续按行号查 chunk 元数据是数组下标访问

**与 WeKnora 的区别**：

WeKnora 把 chunk 元数据存 PostgreSQL（`document_chunks` 表），是**统一关系数据库 + 向量库双写**模式。custom_app 走的是**轻量的"文件 + 向量库"模式**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| **WeKnora 模式**（PG + 向量库） | 支持复杂 SQL 查询、事务、多租户、审计 | 多一个数据库依赖，写入要双写一致性 |
| **custom_app 模式**（chunks.jsonl + 向量库） | 0 依赖、易部署、易调试（VSCode 直接看 jsonl） | 多租户 / 高并发场景下文件 I/O 是瓶颈 |

custom_app 选这套是因为当前规模（每 KB 几十到几千 chunk）下文件就够；Phase 10 多租户/共享时再迁 PG。

**实际 RAG 检索链路**（来自 [rag_runner.py `_prepare_chat_context`](../../custom_app/services/rag_runner.py#L1610)）：

```
query → embed → Qdrant.search → 得到 [Hit(chunk_id, score)]
    ↓
chunk_id → 反查 self._rows[行号] → 得到完整 contents + images + ...
    ↓
拼 prompt + 调 LLM → 生成答案
    ↓
self._rows 里的 images 字段 → 转 data URL 嵌入答案
```

`self._rows` 就是 chunks.jsonl 内存化的版本——**检索必须经它**。

---

### Q2.2：评测集 query 由 chunks 内容反推生成，会影响 BM25 / 检索评分吗？

**会，而且这是评测信号偏向的根本原因**。

你的评测集来源：拿一个 chunk 的内容 → 让 Gemini 编 3 个问题 → 标该 chunk 为 gold。

**这种构造方式的偏向**：

1. **问题词汇与 chunk 字面词汇高度重叠** —— LLM 编问题时会大量复用 chunk 里的术语
2. **向量检索强 + BM25 看着也强** —— 因为问题就是 chunk 的"改写"，两种检索都能命中
3. **真实用户问题往往用同义词** —— 用户问"换电池怎么搞"，文档写"BatteryChangeSequence STEP 1"，向量 / BM25 都没法直接命中

**对 BM25 的具体影响**：

- 评测集里"问题 → gold chunk"的关键词高度对齐 → BM25 应该容易命中
- 但实测 BM25 在 agv_demo 上 +0.00pp，原因不是评测集问题，是：
  - **jieba 中文分词把"换电池"切成"换"+"电池"**，单字 token IDF 偏低
  - 评测集里 chunk_id 通常只有 1-2 个 chunk 完全匹配，BM25 IDF 高的词（特殊术语）一般 vector 也能召回
- **如果换成"用户日常 session 问题"作评测集**，BM25 的真实差距可能更大（向量误差变大，BM25 由字面命中救一些）

**结论**：你说得对——**Gemini 反推问题的方式让评测信号偏向"字面匹配场景"**，这恰好是 BM25 应该擅长的场景。BM25 都救不动，说明：

1. agv_demo 文档规模太小（56 chunk），向量已经能完整覆盖
2. jieba 在中文 SOP 上的 token 切分不够 IDF 友好

**后续如何修正评测信号**：

- A 路（从 `kb_session_messages` 抽真实用户问题）应该是评测集主力，但目前 agv_demo 只有 8 条 session（占 14%）
- 等业务上线一段时间积累更多真实 session，重新跑 baseline，BM25 / IRCoT 可能呈现完全不同的信号

---

### Q3：BM25 对当前 SOP 无效，对通用知识库是否有效？Contextual 怎么应用？BM25 能否按 KB 开关？

#### Q3.1：BM25 对通用文档（如 FAQ 汇编 / 培训手册）应该有效

理论上 BM25 在以下场景**比向量更准**：

| 场景 | 原因 |
|---|---|
| **专有名词 / 错误代码精确匹配** | `E03`、`Master Link Down`、`-2146697211` 这类字符串向量泛化差，BM25 字面命中 100% |
| **少 chunk 但 chunk 长** | IDF 区分度高，BM25 容易找到独家 chunk |
| **中英混合术语** | 英文术语 jieba 不切，BM25 直接精确匹配 |
| **长尾问题** | 向量模型对训练数据外的术语泛化弱，BM25 不依赖训练 |

当前 agv_demo 不显著的原因：
- chunk 数小（56）→ 向量已 0.6753 Recall@5，提升空间小
- 评测集偏向字面匹配 → 向量也覆盖了字面信号
- jieba 中文短句切分粗（"换电池" → "换"+"电池"）→ BM25 IDF 退化

未来加大型通用知识库（成百上千 chunk），BM25 + RRF 应该会有 5-15pp 的 Recall@5 提升。

#### Q3.2：Contextual chunking 怎么应用到检索

**完整链路**：

1. **ingest 阶段**（在 `_run_ingest_job` 的 parse 之后 / embed 之前插入 `_context_stage`）：
    - 对每个 chunk，把所属**整篇文档文本**塞给 Gemini Flash
    - Gemini 生成 50-150 字摘要描述"该 chunk 在文档中的位置和作用"
    - 摘要写入 chunks.jsonl 的 `context` 字段

2. **embedding 阶段**（[google_embedder.py compose_doc_embedding_text](../../custom_app/services/google_embedder.py#L118)）：
    ```
    [context]
    heading_path
    title
    contents
    ```
    把 context 拼到 chunk 文本最前面再去算 embedding。这样 chunk 向量里就**编码了文档级语义**。

3. **检索阶段**：query embed → Qdrant 找相似 chunk → 因为 chunk 的 embedding 含 context，"脱离原文档时也能定位"。

**典型例子**：

> chunk 内容：`"按 7 号键导航 AGV 到空电池仓"`
>
> 没 context 时，user 问 `"换电池第一步"`，embedding 相似度可能不够（chunk 没"换电池"字样）。
>
> 加 context `"本文档介绍 AGV 换电池流程共 11 步；本 chunk 是第 1 步，开机导航到电池仓"` 后，chunk 向量包含了"换电池"语义，命中概率大幅提高。

**评测结果验证**（agv_demo）：
- Recall@5 +2.59pp（召回扩大，命中更多 chunk）
- Hit@5 +3.45pp（top-5 命中率提高）
- Hit@1 -3.45pp / MRR -1.29pp（**副作用**：context 摘要稀释了原文最强匹配的语义相似度，导致原本排第 1 的 chunk 被排到后面）

**实际生产**：已开启 Contextual（chunks.jsonl 含 context 字段），生产配置 mode=vector + context。

#### Q3.3：BM25 能否对指定 KB 开启？

✅ **可以，但当前是全局 env 开关**：

| 方式 | 当前 | 实现 |
|---|---|---|
| **全局**（已有） | `ULTRARAG_RETRIEVAL_MODE=hybrid` env 或 yaml 配置 `retrieval.mode: hybrid` | [rag_runner.py _resolve_retrieval_mode](../../custom_app/services/rag_runner.py#L186) |
| **Per-KB**（未实现） | `kb` 表加 `retrieval_mode` 列 / `KbConfigStore` 按 KB 取 mode | Phase 9+ 待加 |

**临时手段**（不改代码就能切换）：

1. 启动 Flask 前 `set ULTRARAG_RETRIEVAL_MODE=hybrid` 让全局走 hybrid
2. 但**所有 KB 都会受影响**，不能只对一个 KB

**未来扩展（建议放 Phase 9 一并做）**：

- `kb_configs` 表加 `retrieval_mode` / `bm25_enabled` 字段
- admin 界面给每个 KB 配独立开关
- RagRunner 按 kb_id 从 repo 读 mode

代码改造点很小（rag_runner `_resolve_retrieval_mode` 加一个 KB-level lookup），1-2 小时工作量。如果某天某个新 KB 评测显示 BM25 显著有效，再做这个扩展也来得及。

---

### Q4：IRCoT 同 Q3 类似的问题？

**IRCoT 没真跑评测，目前是按 PLAN §八的退出条件直接跳过的**。原因：

1. **8.2 评测结果显示 agv_demo 瓶颈不是召回少**（Recall@10=0.7270 已不低），是**排序错位**（Hit@1 = 0.5517 / MRR = 0.6197）
2. **IRCoT 攻的是"多跳问题"** —— 需要 2-3 步推理才能找到答案的场景
3. **当前评测集多跳样本太少**：agv_demo 58 条里只有 ~3 条带 `multi_step` 标签

PLAN §八明确：评测分数不达标 → 不上线 → Phase 8 收尾。

**IRCoT 对什么场景有效**：

| 场景 | IRCoT 价值 |
|---|---|
| 单跳问题（"换电池第一步"） | ❌ 浪费（1 轮 RAG 就够） |
| **跨文档多跳**（"AGV 充电桩 E03 故障怎么处理"，需查"E03 = 电池温度过高" + "电池过热处理流程") | ✅ 显著（论文典型场景） |
| **隐含多步推理**（"为什么 STEP 5 后 AGV 还不动"，需推理"前置 STEP 1-4 状态 + STEP 5 后置 STEP 6 触发"） | ✅ 中等 |

当前 SOP 类问题多数是单跳，IRCoT 边际收益小。

**Per-KB / per-query 切换 IRCoT**：

| 方式 | 估计 |
|---|---|
| **按 KB 开关** | 通用知识库 KB / 多文档 KB 启用 IRCoT，单文档 SOP 关闭 |
| **按 query 自动判断** | 前端按 query 长度 / 关键词触发 IRCoT；难以可靠判断 |
| **用户手动切**（"深度推理"按钮） | UX 友好，但用户不知道何时该切 |

**重启 8.3 的条件**：

1. 评测集扩充 `multi_step` 标签样本到 ≥15 条（**业务侧手工标注**，不能让 Gemini 编 —— Gemini 编的多跳是假的）
2. 出现一类**跨文档**的真实业务问题（如 IFS 既要查"500 错原因"又要查"如何启动 Oracle"）
3. 在 UltraRAG 上借用验证（按 PLAN §四）确认 F1 提升 ≥0.05 后再剥离移植

---

## 三、Phase 8 工程产物清单

### 代码 / 配置

| 路径 | 用途 |
|---|---|
| [`custom_app/services/docx_parser.py`](../../custom_app/services/docx_parser.py) | 8.0 兜底滑窗（_sliding_window_chunks） |
| [`custom_app/services/eval/`](../../custom_app/services/eval/) | 8.1 评测体系（schema / dataset / metrics / runner） |
| [`custom_app/services/chunking/contextual.py`](../../custom_app/services/chunking/contextual.py) | 8.2.1 Contextual chunking |
| [`custom_app/services/retrieval/bm25.py`](../../custom_app/services/retrieval/bm25.py) | 8.2.2 BM25 关键词召回 |
| [`custom_app/services/retrieval/rrf.py`](../../custom_app/services/retrieval/rrf.py) | 8.2.2 RRF 融合 |
| [`custom_app/services/google_embedder.py`](../../custom_app/services/google_embedder.py) | compose_doc_embedding_text 含 context 前缀 |
| [`custom_app/api/kb.py`](../../custom_app/api/kb.py) | `_run_ingest_job` 加 `_context_stage` |
| [`servers/retriever/parameter.yaml`](../../servers/retriever/parameter.yaml) | retrieval.mode=vector（生产配置） |

### 脚本

| 路径 | 用途 |
|---|---|
| [`custom_app/scripts/extract_eval_queries.py`](../../custom_app/scripts/extract_eval_queries.py) | A 路：从 session 抽 user query |
| [`custom_app/scripts/generate_eval_queries.py`](../../custom_app/scripts/generate_eval_queries.py) | B 路：Gemini 编候选 |
| [`custom_app/scripts/eval_custom_app.py`](../../custom_app/scripts/eval_custom_app.py) | 评测入口 |
| [`custom_app/scripts/backfill_context.py`](../../custom_app/scripts/backfill_context.py) | Contextual 一次性回填 |
| [`custom_app/scripts/toggle_context_for_eval.py`](../../custom_app/scripts/toggle_context_for_eval.py) | 评测专用切换 context 可见性 |
| [`custom_app/scripts/phase8_2_compare.py`](../../custom_app/scripts/phase8_2_compare.py) | 4 组矩阵汇总生成 markdown |

### 评测产物

| 路径 | 用途 |
|---|---|
| [`data/eval/agv_demo.jsonl`](../../data/eval/agv_demo.jsonl) | 58 条评测 |
| [`data/eval/ifs_docs.jsonl`](../../data/eval/ifs_docs.jsonl) | 55 条评测 |
| [`data/eval/baseline/*.json`](../../data/eval/baseline/) | 基线快照 |
| [`data/eval/phase8_2/*.json`](../../data/eval/phase8_2/) | 4 组矩阵原始数据 |
| [`data/eval/phase8_2_comparison.md`](../../data/eval/phase8_2_comparison.md) | 完整对比报告 + 分析 + 建议 |

### 测试（167 case，全过）

| 路径 | case 数 |
|---|---|
| `tests/test_docx_parser_sliding.py` | 9 |
| `tests/test_eval_dataset.py` | 26 |
| `tests/test_eval_metrics.py` | 42 |
| `tests/test_eval_generators.py` | 9 |
| `tests/test_eval_runner.py` | 9 |
| `tests/test_chunking_contextual.py` | 16 |
| `tests/test_ingest_context_stage.py` | 4 |
| `tests/test_retrieval_bm25.py` | 16 |
| `tests/test_retrieval_rrf.py` | 7 |
| `tests/test_rag_runner_hybrid.py` | 11 |
| **合计** | **149**（加上 schema 测试若干） |

### 文档

| 路径 | 用途 |
|---|---|
| [`docs/Phase8/README.md`](./README.md) | Phase 8 入口（含状态列） |
| [`docs/Phase8/PHASE_8_0_PLAN.md`](./PHASE_8_0_PLAN.md) | 8.0 完整计划 + 实施记录 |
| [`docs/Phase8/PHASE_8_1_PLAN.md`](./PHASE_8_1_PLAN.md) | 8.1 完整计划 + 实施记录 |
| [`docs/Phase8/PHASE_8_2_PLAN.md`](./PHASE_8_2_PLAN.md) | 8.2 完整计划 + 实施记录 |
| [`docs/Phase8/PHASE_8_3_PLAN.md`](./PHASE_8_3_PLAN.md) | 8.3 计划（未实施） |
| [`docs/Phase8/MANUAL_TESTING.md`](./MANUAL_TESTING.md) | 5 块手工测试清单 + 复盘 |
| **`docs/Phase8/PHASE_8_SUMMARY.md`** | **本文** |
| [`data/eval/README.md`](../../data/eval/README.md) | 业务侧标注指南 |

---

## 四、关键收获 / 下一步方向

### Phase 8 最大收获

不是某个算法本身的"上线"，而是**建立了量化决策机制**：

1. **PoC 阶段先验证再投入**：BM25 写完代码 + 评测 + 决策不上线 = 1 周；如果没有评测体系，可能跑半年才发现不如纯 vector
2. **诊断瓶颈方向**：agv_demo 的问题不是召回少（Recall@10=0.73），是排序错位（Hit@1=0.55）—— 这是下期攻坚目标
3. **失败降级机制**：BM25 / context 任何一个出问题，自动降级到纯 vector，不阻塞生产

### 下一步候选方向（按 ROI 排序）

| 优先级 | 方向 | 估计工时 | 预期收益 |
|---|---|---|---|
| 🟢 高 | **Reranker 调优** | 1-2 天 | 提升 Hit@1 / MRR（直击 agv_demo 瓶颈） |
| 🟢 高 | **评测集扩充**（A 路 session 积累 + 业务手写 multi_step） | 业务侧 2-3 天 | 让 BM25 / IRCoT 评测信号更真实 |
| 🟡 中 | **Per-KB retrieval 配置** | 1-2 小时 | 给将来通用 KB 留口子 |
| 🟡 中 | **Phase 9 图文联动** | 2-6 周 | 现有 SOP 多图，回答里嵌图能力提升 |
| 🟡 中 | **Phase 11.1 生产化必需** | 4-5 周 | 上线前的工程兜底 |
| 🔴 低 | Phase 8.3 IRCoT 重启 | 3 周 | 等评测集多跳样本足够后再说 |
| 🔴 低 | Phase 10 多租户 | 6-8 周 | 业务规模到了再做 |

我个人建议下一步走 **Reranker 调优**（高 ROI、短工时、攻 Phase 8 暴露的真瓶颈）+ **评测集扩充**（业务侧并行做）。Phase 9 图文联动也是个独立有价值的方向。

---

## 五、Phase 8 之外发现的性能问题（待优化）

### 5.1 问题描述

手工测试 E.3 反馈"每个问题约 2 分钟响应"。该问题**不属于 Phase 8 范围**（Phase 8 没动 LLM 调用链路），但 Phase 8 评测过程中识别到了瓶颈，记录在此供后续 Phase 11.1 性能优化时直接对照。

### 5.2 用 `time.perf_counter()` 实测的耗时拆解

测试条件：query = "E-Stop Button Active 怎么处理？"，agv_demo KB，top_k=10，CUDA GPU。

| 阶段 | 首次 | 第二次（缓存后） | 是否每问发生 |
|---|---|---|---|
| 1. Query embedding（Gemini API 调用） | 1266ms | **1271ms** | ✅ 每问都要 |
| 2. Qdrant 向量检索 | 68ms | 63ms | ✅ 每问都要 |
| 3. Reranker（bge-reranker-v2-m3） | **8244ms**（含模型加载） | **57ms** | 仅首次慢；之后缓存 |
| 4. chunks.jsonl 元数据查询（`self._rows[i]`） | <1ms | <1ms | ✅ 每问都要，但开销可忽略 |
| 5. LLM 生成（Gemini-3.1-pro-preview / Claude） | **30-120 秒** | **30-120 秒** | ✅ **主瓶颈** |
| 6. 图片转 data URL（如答案含图） | 100-500ms / 张 | 100-500ms / 张 | 视答案而定 |

**chunks.jsonl 不是性能瓶颈**（< 1ms）。性能瓶颈集中在：

- **LLM 生成阶段 30-120s**：thinking 模型 + 跨国网络 + prompt 长度共同作用，不只是模型选择问题（Claude 同样慢）
- **Gemini embedding API 1.3 秒/问**：跨国网络延迟固定开销
- **Reranker 首次加载 10 秒**：每次 Flask 重启的第一个问题受影响

### 5.3 已批准的 3 项优化方向

放在 Phase 11.1 生产化必需阶段实施，**优先级按 ROI 排序**：

#### 优化 1：本地 Embedding 模型替代 Gemini API

**目标**：消除 1.3 秒/问的跨国网络往返。

**方案**：使用 `Qwen/Qwen3-Embedding-4B` 本地部署。

**对比**：

| 维度 | Gemini API（现状） | Qwen3-Embedding-4B（目标） |
|---|---|---|
| 单次延迟 | 1.3 秒（含网络） | 50-200ms（本地 GPU） |
| 每问开销 | 1.3 秒 | < 0.2 秒 |
| 月成本 | 视 Gemini 计费 | 一次性硬件 + 电费 |
| 多语言支持 | ✅ Gemini 全语种 | ✅ Qwen3 中英 + 主要语种 |
| 离线运行 | ❌ 必须联网 | ✅ 局域网 |
| 重建索引时长 | 56 chunks ~10 秒（网络限速） | 56 chunks <1 秒 |

**改造点**：

- 新增 `custom_app/services/local_embedder.py`：封装 Qwen3-Embedding-4B 推理（加载到 RTX 2080 / 类似 GPU）
- `parameter.yaml` 加 `embed_backend: local | gemini` 配置
- `google_embedder.py` 接口保持不变，按 backend 路由
- 评测：现有评测集跑一遍对比新旧 embedding 的检索分数，确保不掉分

**工时估计**：1-2 天（模型加载 + 接口封装 + 评测验证）

**风险**：

- 维度可能与 Gemini 不一致（Gemini 768 / Qwen3 默认 1024 或更高），需要重新建 Qdrant collection
- 中文 SOP 召回效果可能略不同，需评测验证

#### 优化 2：Flask 启动时预热 Reranker

**目标**：消除首次问答的 10 秒模型加载延迟。

**方案**：`RagRunner.init()` 末尾跑一次空 reranker 调用触发加载。

**改造点**（[rag_runner.py init](../../custom_app/services/rag_runner.py#L1325)）：

```python
# init() 末尾追加：
if self._rerank_cfg.get("enabled", True) and self._rerank_load_error is None:
    try:
        # 跑一次空 query，让 LocalReranker 完成模型加载到 GPU
        self._rerank_hit_ids("__warmup__", [0] if self._rows else [])
        logger.info("rag_runner reranker warmed up kb=%s", self.kb_id)
    except Exception as e:
        logger.warning("reranker warmup failed kb=%s: %s", self.kb_id, e)
```

**工时估计**：30 分钟 + 单测

**风险**：低（最坏退回到原行为，首次问答慢）

#### 优化 3：改成流式输出（quick mode 也走 streaming）

**目标**：体感速度大幅提升——用户在 LLM 思考完前就能看到首 token。

**当前现状**（[rag_runner.py chat_stream:1952-1957](../../custom_app/services/rag_runner.py#L1952)）：

```python
if normalized_mode == "quick":
    # 注释："vLLM/OpenAI-compatible streaming can hang on some local gateways..."
    # quick 模式当前用非流式调 LLM，拿到完整答案再一次性 yield chunk
    answer_raw = self._generate(prep["prompt_text"]).strip()
```

**改造点**：

1. quick 模式启用流式：调 `_generate_stream` 替代 `_generate`
2. 前端 `onChunk` 已支持累积渲染（[main.js:1229](../../custom_app/frontend/main.js#L1229)）
3. 处理对 Gemini / Claude / vLLM 三种 backend 流式接口的兼容
4. 容错：流式接口超时时降级到非流式（保持现有可靠性）

**工时估计**：1 天（兼容性测试 + 多 backend 验证）

**风险**：

- 不同 LLM backend 的流式协议略有差异（OpenAI SSE / Gemini streamGenerateContent / Anthropic stream）
- 注释提到"local gateway 可能 hang"，需要在 vLLM 上重新验证

### 5.4 不会做的方向

| 方向 | 不做原因 |
|---|---|
| **chunks.jsonl → PG 迁移** | 性能上 < 1ms 无影响；只为 Phase 10 多租户做，不为性能 |
| **整体重写 RagRunner** | 当前架构清晰，只需局部优化；动整体风险大 |
| **缓存 LLM 响应** | 用户问题口语化、噪声大，命中率低；Phase 11.1 看具体情况 |

### 5.5 执行节奏

建议在 Phase 11.1（生产化必需）启动时优先做这 3 项：

1. **优化 2（reranker 预热）**：先做，30 分钟立刻见效
2. **优化 3（流式输出）**：第二做，1 天，体感提升最显著
3. **优化 1（本地 embedding）**：第三做，1-2 天，需评测验证

完成后预期单次问答时间从 **30-120 秒** 降到 **首 token 1-2 秒 / 完整答案 10-30 秒**（LLM 生成本身就需要 10s+，无法低于这个下限）。
