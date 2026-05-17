# Phase 8.2 —— Contextual Chunking + BM25 双路检索

> **状态**：草案待讨论（2026-05-16）
> **前置**：[Phase 8.1](./PHASE_8_1_PLAN.md) 完成（基线已建立）
> **借用**：❌ 不借 UltraRAG，全部 custom_app 内部实现
> **参考**：Anthropic 2024 [Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)、WeKnora [fuseWithRRF](../../../WeKnora/internal/application/service/knowledgebase_search_fusion.go)

---

## 一、目标

1. **Contextual chunking**：每个 chunk 写入向量库前，附加一段「文档级上下文摘要」前缀，让 chunk 在脱离原文档时仍可定位
2. **BM25 双路**：向量检索 + 关键词 BM25 检索并行，RRF 融合
3. 用 Phase 8.1 的评测集证明：**Recall@5 提升 ≥10pp 或 MRR 提升 ≥0.05**
4. 不达标则**不上线**，Phase 8 至此收尾，直接跳过 8.3

---

## 二、非目标（推迟）

| 推迟项 | 推到哪 |
|--------|--------|
| Tantivy / Elasticsearch 等独立全文索引 | Phase 9+；先用 `rank_bm25`（Python 内存）或 PG FTS5 |
| Multi-query rewrite（一个 query 拆多个） | Phase 8.x 增量，先验证基础 BM25 |
| HyDE（假设性文档嵌入） | Phase 9+ |
| 给 chunk 加「可回答问题列表」（HyDE-Q） | 同上 |
| chunking 切分策略调整（滑窗、token 切分） | 已在 [docx_parser.py](../../custom_app/services/docx_parser.py) 中按 STEP/heading 切，本期**不动切分**，只加 context |

---

## 三、Contextual Chunking 设计

### 3.1 当前 chunk 长什么样

[docx_parser.py:228](../../custom_app/services/docx_parser.py#L228) `pack_chunk` 写出的 chunk：

```json
{
  "id": "agv_demo_step_3",
  "title": "AGV 启动手册 | STEP 3",
  "contents": "STEP 3: 启动主控电源\n按下绿色按钮...\n[IMG: images/agv/img_0003.png]",
  "doc": "agv_demo",
  "structure": {"heading_path": ["AGV 启动手册"], "step_number": 3}
}
```

**问题**：`contents` 单独看不出"在讲什么 AGV 型号"、"前置步骤是什么"。embedding 时这些上下文信息丢失。

### 3.2 加上 context 前缀

```json
{
  "id": "agv_demo_step_3",
  "title": "AGV 启动手册 | STEP 3",
  "contents": "STEP 3: 启动主控电源\n按下绿色按钮...",
  "context": "本文档介绍 XYZ-型号 AGV 的启动流程，共 8 步。STEP 3 紧接电池检查之后，是通电的关键步骤。",
  "doc": "agv_demo",
  "structure": {...}
}
```

embedding 时拼接：`text_for_embedding = context + "\n\n" + contents`

### 3.3 Context 怎么生成

**用 Gemini 给每个 chunk 生成 50-100 字摘要**：

```python
# custom_app/services/chunking/contextual.py
PROMPT = """
<document>
{full_document_text}
</document>

<chunk>
{chunk_contents}
</chunk>

请用 50-100 字描述这个 chunk 在整个文档中的位置和作用。直接输出摘要，不要解释。
"""

def generate_context(full_doc: str, chunk_text: str) -> str:
    return gemini.generate(PROMPT.format(...))
```

**性能与成本**：

| 维度 | 估算 |
|------|------|
| 调用次数 | 每个 KB ingest 时 = chunk 数（IFS 16 + AGV 23 = 当前 39 次） |
| 单次成本 | Gemini Flash ~$0.0001 / 调用 |
| 单 KB 成本 | < $0.005 |
| 单次延迟 | 0.5-1s / chunk |
| 全 KB ingest 延迟增量 | +20-40s（39 chunks 并行 4 路） |

**缓存策略**：context 生成结果存进 chunk JSONL，重建索引不重新生成。Phase 6 已有的 `chunks.jsonl` 加字段即可。

### 3.4 改造点

| 文件 | 改动 |
|------|------|
| [`docx_parser.py`](../../custom_app/services/docx_parser.py) | `pack_chunk` 不动；新建 `services/chunking/contextual.py` 在 ingest 之后批量调用 |
| [`api/kb.py:_run_ingest_job`](../../custom_app/api/kb.py) | parse 阶段之后、embed 阶段之前，插入 `_context_stage` |
| [`services/google_embedder.py`](../../custom_app/services/google_embedder.py) | `text_for_embedding` 拼 `context + contents` |
| `chunks.jsonl` schema | 加 `context: str` 字段（向后兼容：缺失时按原行为） |

### 3.5 Prompt caching 优化

Anthropic 原方案：用 prompt caching 让 `full_doc` 部分只算一次，后续 chunks 复用。

Gemini 也有 [Context Caching](https://ai.google.dev/gemini-api/docs/caching)：

- 全文 > 32k tokens 可缓存
- 缓存费用比正常输入便宜 75%
- 我们的 SOP 文档大多 5-20k tokens，不到缓存阈值；**先不上**，纯并行调用

---

## 四、BM25 双路检索设计

### 4.1 现状

[`rag_runner.py`](../../custom_app/services/rag_runner.py) `search()`：query → Gemini embedding → Qdrant 单路向量召回 → bge-reranker 重排 → top_k。

### 4.2 加 BM25 双路

```
query
  ├── 向量召回（Qdrant top-20）  ─┐
  └── BM25 召回（top-20）        ─┤── RRF 融合 → top-20 → rerank → top-5
```

### 4.3 BM25 后端选择

| 候选 | 优点 | 缺点 | 决策 |
|------|------|------|------|
| **A. `rank_bm25` Python 库** | 0 部署、纯内存、API 简单 | 全量重建慢、不持久化 | **MVP 首选** |
| **B. PostgreSQL FTS5（tsvector）** | 持久化、和现有 PG 同栈 | 中文分词要装扩展（zhparser/pgroonga），中文效果一般 | 中文场景慎用 |
| **C. Elasticsearch** | 业界标准、中文好 | 又多一个服务 | 不在本期范围 |
| **D. Tantivy** | Rust 实现，性能强 | 又多一个组件 | Phase 9+ |

**决策**：先 A（`rank_bm25`）。每个 KB 一个 BM25 实例，启动时从 `chunks.jsonl` 全量建。chunk 数（百级到千级）下内存和时间都不是瓶颈。

### 4.4 中文分词

`rank_bm25` 默认按空格分词，对中文无效。需要：

- 用 `jieba`（成熟、轻量、`.venv` 易装）做中文分词
- 英文走默认分词
- 混合文本（如 SOP 里夹英文术语）：`jieba.cut(text, cut_all=False)` 自动混合分词

```python
import jieba
def tokenize(text: str) -> list[str]:
    return [t for t in jieba.cut(text) if t.strip()]
```

### 4.5 RRF 融合

直接抄 WeKnora 的实现（30 行）：

```python
# custom_app/services/retrieval/rrf.py
def fuse_with_rrf(
    vector_hits: list[Hit],
    keyword_hits: list[Hit],
    *,
    k: int = 60,
    vector_weight: float = 0.7,
    keyword_weight: float = 0.3,
) -> list[Hit]:
    """Reciprocal Rank Fusion. 参考 WeKnora knowledgebase_search_fusion.go:75"""
    vector_ranks = {h.chunk_id: i + 1 for i, h in enumerate(vector_hits)}
    keyword_ranks = {h.chunk_id: i + 1 for i, h in enumerate(keyword_hits)}
    # ... 合并所有 chunk_id, 计算 weighted RRF score
```

权重 0.7 / 0.3 是 WeKnora 默认值，可调。**应用层调整 → 评测 → 选最优**。

### 4.6 改造点

| 文件 | 改动 |
|------|------|
| `custom_app/services/retrieval/bm25.py` | 新建：`Bm25Store.load(chunks_path)`、`search(query, top_k)` |
| `custom_app/services/retrieval/rrf.py` | 新建：`fuse_with_rrf(...)` |
| [`rag_runner.py`](../../custom_app/services/rag_runner.py) | `__init__` 加载 BM25；`search()` 改成"双路并行 + RRF 融合" |
| [`servers/retriever/parameter.yaml`](../../servers/retriever/parameter.yaml) | 加 `bm25.enabled` / `bm25.weight` / `vector.weight` |
| `chunks.jsonl` | 不变（BM25 直接读 contents + context 字段） |

---

## 五、任务拆分

### 8.2.1 Contextual chunking（3 天）

| 子任务 | 工时 | 验收 |
|--------|------|------|
| 设计 prompt + 单文档 PoC | 0.5 天 | 跑 5 个 chunk 看生成质量 |
| `services/chunking/contextual.py` 实现（并行调 Gemini） | 1 天 | 单测：mock Gemini，3 chunks 并行 |
| 接入 `_run_ingest_job` 新 stage | 0.5 天 | ingest 完跑通 + `chunks.jsonl` 含 `context` 字段 |
| `google_embedder` 拼接 context | 0.5 天 | 单测：缺失 context 时回退原行为 |
| 重建索引：ifs_docs + agv_demo | 0.5 天 | Qdrant collection 数量不变，向量已更新 |

### 8.2.2 BM25 双路（3 天）

| 子任务 | 工时 | 验收 |
|--------|------|------|
| `services/retrieval/bm25.py` + jieba 分词 | 1 天 | 单测：3 个 query 命中预期 chunk |
| `services/retrieval/rrf.py` | 0.5 天 | 单测：抄 WeKnora 那段公式，做边界 case |
| `rag_runner.search()` 改造（开关式） | 1 天 | 改造前后 yaml 切换：纯向量 ↔ 双路 |
| 集成测试 + 日志打点 | 0.5 天 | logs 能看到 `vector_hits / bm25_hits / rrf_topk` |

### 8.2.3 评测对比（1 天）

| 子任务 | 工时 | 验收 |
|--------|------|------|
| 跑 4 组评测 | 0.5 天 | 矩阵：[纯向量, +context, +bm25, +context+bm25] × 2 KB |
| 输出对比报告 | 0.5 天 | `data/eval/phase8_2_comparison.md`，分数表 + 失败样本分析 |

**合计：约 7 天（1 周）**。

---

## 六、关键风险

| 等级 | 风险 | 缓解 |
|------|------|------|
| 🔴 HIGH | context 生成质量差（Gemini 生成的摘要不准/无信息量） | PoC 阶段先 5 chunk 抽查；prompt 迭代 |
| 🟡 MED | BM25 中文分词在 SOP 专有名词上效果差（如型号、零件号） | 加自定义词典：`jieba.load_userdict("data/eval/agv_terms.txt")` |
| 🟡 MED | RRF 权重 0.7/0.3 对 SOP 不一定最优 | Phase 8.1 评测脚本支持权重扫描（0.5/0.5、0.6/0.4、0.7/0.3、0.8/0.2） |
| 🟡 MED | Context 重建索引消耗 Gemini 配额 | 全量 ingest 才生成，重建索引不重生成；幂等存进 chunks.jsonl |
| 🟢 LOW | `rank_bm25` 大库内存占用 | 单 KB <10k chunks 时无问题，未来切 Tantivy 再说 |
| 🟢 LOW | jieba 启动慢（首次 import ~1s） | RagRunner 复用一次加载，不影响 query 延迟 |

---

## 七、待讨论问题

1. **Context 范围**：用「完整文档」还是「附近 ±N 段」做上下文？文档短（<10k token）用完整，长文档可能要分段
2. **现有 KB 的 context 何时回填**：本期触发一次性全量回填，还是只对新 ingest 生效？建议**全量回填**两个评测 KB（ifs_docs / agv_demo），别的 KB 自然 ingest 时再说
3. **BM25 实例的生命周期**：和 RagRunner 一起加载（按 kb_id 缓存），还是单独的 BM25 服务？建议前者，简单
4. **双路开关粒度**：env 全局开关 vs 每 KB 配置？建议**先全局**（`ULTRARAG_RETRIEVAL_MODE=vector|hybrid`），Phase 9 再做 per-KB
5. **失败回退**：BM25 异常 / context 生成失败时，是降级到纯向量还是 fail-fast？建议**降级 + 日志告警**

---

## 八、退出条件

跑完 8.2.3 评测后，三种结局：

| 结局 | 条件 | 行动 |
|------|------|------|
| 🟢 **全胜** | context 和 BM25 都有显著收益（≥10pp Recall@5 提升） | 全量上线，进入 Phase 8.3 |
| 🟡 **半胜** | 只有其中之一有效 | 只上有效的那部分，进入 Phase 8.3 |
| 🔴 **失败** | 都没显著提升 | 不上线，Phase 8 收尾，跳过 8.3 |

---

## 九、验收清单

- [ ] `custom_app/services/chunking/contextual.py` 实现完成 + 单测
- [ ] `custom_app/services/retrieval/bm25.py` + `rrf.py` 实现完成 + 单测
- [ ] `_run_ingest_job` 新 stage `context` 跑通 ifs_docs / agv_demo
- [ ] `rag_runner.search()` 双路 + RRF 融合，可通过 yaml/env 切换
- [ ] `data/eval/phase8_2_comparison.md` 输出 4 组分数对比
- [ ] 满足「退出条件」中至少一种胜利场景

---

> 实施前所有「待讨论问题」必须先达成共识。
