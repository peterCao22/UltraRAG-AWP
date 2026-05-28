# 技术债：rag_runner.py 中 AGV/SOP 专用硬编码

> 创建时间：2026-05-28（Phase 12.1 收官后用户提出）
> 状态：**已识别，未实施**。推到 Phase 11.3 或 12.x 实施。
> 优先级：中

---

## 一、问题

`custom_app/services/rag_runner.py` 当前包含 **AGV SOP 业务专用**的检索/扩展机制，但该文件**面向全部 KB**（包括 ifs_docs 操作手册、gen_test 通用文档等）。硬编码影响：

### 1.1 影响行为的硬编码（🔴 真正有害）

| 位置 | 内容 | 影响 |
|---|---|---|
| L43 `_STEP_IN_ID_RE = r"_step_(\d+)"` | 只识别 `_step_N` chunk_id | 其他 KB 命名（`_part_N` / 自由形式）→ `_is_step_chunk_row` 永远 False → SOP 扩展机制失效 |
| L44 `_STEP_IN_TITLE_RE = r"STEP\s*(\d+)"` | 只识别 "STEP N" 标题 | 同上 |
| L472-473 `MIN_STEPS_FOR_EXPAND=2` / `TOP_RANK_FOR_SINGLE_STEP=3` / `NON_STEP_GUARD_TOP_RANK=3` | 硬编码扩展阈值 | 不同 KB 可能需要不同阈值 |
| L1252-1253 `_rewrite_query` prompt | "Keep AGV domain terms..." / "battery replacement" | 改写所有 KB 的 query 时都注入 AGV 语境（**已确认影响 ifs_docs**）|
| L853-856 STEP/步骤 前缀剥离 | 在 chunk 显示文本里硬剥 `STEP N:` | 非 STEP KB 不受影响，但代码逻辑泄漏 |

### 1.2 注释/文案级（🟡 文档污染但不影响行为）

- L26 模块 docstring 写 "AGV 场景的单轮 RAG"
- L122 RagRunner docstring 写 "AGV Phase-1 RAG 运行器"
- L383 / L385 注释举例 "E-Stop Button Active"

---

## 二、当前状态分析

### 2.1 已有的 KB 类型分类

DB 表 `knowledge_bases.type` 已经分两类（`api/kb.py:KB_TYPE_*`）：

```
KB_TYPE_SOP_DOCX = "sop_docx"   # SOP 类（走 docx_parser 业务定制分块）
KB_TYPE_GENERAL  = "general"     # 通用文档（mineru / docling 解析）
```

实际数据库内容（2026-05-28 验证）：

| KB | type | 实际结构 |
|---|---|---|
| agv_demo | sop_docx | 有 STEP 1/2/3 真正的 SOP |
| **ifs_docs** | **sop_docx** | **没有 STEP，只有 section_N_part_M** ← 标错了 type 但 "运气" 没事 |
| gen_test | general | 通用文档 |
| phase_test | general | 通用 |

### 2.2 "运气"侥幸

ifs_docs 评测 Hit@5=1.00（Phase 11.2）—— 不是因为它配置对，而是因为：
- `_STEP_IN_ID_RE` 对 `section_X_part_M` 不匹配
- `_is_step_chunk_row` 对 ifs_docs 所有 chunk 都返回 False
- → `_docs_to_expand` 对 ifs_docs **永远返回空集**
- → **SOP 扩展机制不动 ifs_docs 的检索结果**
- → 检索质量靠纯 RRF + rerank 保住

**这是侥幸不是设计**。未来加新 KB 时随时可能踩坑：
- 如果新 KB 文件含 `STEP` 字眼但不是真 SOP → 被误扩展
- 如果新 KB 用 `_step_N` 命名（很常见的工业 SOP 结构）→ 不期望地被卷进

---

## 三、目标设计

### 3.1 用户需求（2026-05-28 明确）

> "可针对 SOP 类的一种特定方式，非 SOP 的普通方式。我们的 KB 上传分类就做了这两种区分。"
> 修订（2026-05-28 同日）：
> "并不是要取消所有扩展，只是不要被这些专用的硬编码给带偏了"

**翻译成技术语言**：

- 保留扩展能力，但去掉**领域硬编码**（STEP 正则 / battery 词典 / 整本 SOP 拉取）
- 把扩展分成 **2 层**：通用邻居扩展（所有 KB）+ SOP 全文扩展（仅 sop_docx 且含 STEP）

### 3.2 WeKnora 参考实现（值得借鉴）

`d:\Peter2025\myCursor\WeKnora\internal\application\service\chat_pipeline\merge_expand.go`：

```go
// expandShortContextWithNeighbors
const minLen = 350  // 太短的 chunk 触发扩展
const maxLen = 850  // 合并后字数上限
```

**核心机制**（完全通用、零领域知识）：
1. 遍历 retrieved chunks，挑出 content < 350 字的"短 chunk"作为扩展目标
2. 用 chunk 自身的 `PreChunkID` / `NextChunkID` 链拉前后邻居
3. 严格按 `KnowledgeID`（doc 边界）限制，不跨文档
4. 双向迭代扩展直到合并后 ≥ 350 或没有更多邻居或达到 850 上限
5. `concatNoOverlap` 处理重叠拼接，避免重复

**关键优势**：
- **0 硬编码**：不识别 STEP / battery / SOP 任何领域词
- **0 KB 类型依赖**：所有 KB 都用同一套，按 chunk 结构决定
- **目标精准**：只补"短 chunk"的语境，不会把整本文档灌进 context

### 3.3 进一步发现

ifs_docs 暴露了一个 **额外维度**：即便都是 sop_docx，**结构也不同**——
- agv_demo 是"STEP 1/2/3 步骤型 SOP"
- ifs_docs 是"章节型操作手册"（section_N_part_M）

按 chunk 结构自动检测比依赖 `kb.type` 标签更可靠。

### 3.4 进一步打脸：连 agv_demo 内部也不全是 STEP（2026-05-28 用户提出）

用户翻看 raw 文档后发现：agv_demo 共 20 份 docx，**只有 1 份**
（`BatteryChangeSequenceSOP.docx`）真正是 STEP 流程型。其余 19 份是
故障告警 SOP（"Alarm Block Battery Low / Cannot Enter System /
E-Stop / Loop Emergency / Master Link Down"等），结构上是
"intro + 1-3 section"，**没有 STEP**。

实测分布：

| 文档结构 | 数量 | 例子 |
|---|---|---|
| STEP-heavy（≥5 STEP） | **1** | BatteryChangeSequenceSOP（11 STEP）|
| section 型 | 19 | 其余全部告警 SOP |

**含义**：

- 当前 SOP 扩展机制实际上只对 **5% 的 agv_demo 文档**生效
- "AGV = STEP 型 SOP" 是错误的简化；**只有特定流程文档**才是
- 重构方向不是 per-KB，而是 **per-doc 自动探测**
- WeKnora 邻居扩展（Layer 1）对 19 份告警 SOP **正好合适**——它们
  本来就是 "短 intro + 几个 section"，命中短 chunk 时补邻居最有用

---

## 四、重构方案

### 4.1 设计选项（按工作量从小到大）

#### Option A：双层扩展架构（推荐，借鉴 WeKnora，3-5 天）

**Layer 1 — 通用邻居扩展**（所有 KB 都启用）：

借鉴 WeKnora `expandShortContextWithNeighbors`。短 chunk（< 350 字）触发，
按 chunk 自身的前后链拉邻居补语境，扩展到 350-850 字。**零硬编码、
零领域知识**。

```python
# 新增方法（无 KB 类型依赖）
def _expand_short_chunks_with_neighbors(
    self,
    hit_ids: list[int],
    *,
    min_len: int = 350,
    max_len: int = 850,
) -> list[int]:
    """对 content < min_len 的命中按前后邻居链补足语境。"""
```

**Layer 2 — STEP 全文扩展**（per-doc 探测，仅对真正的 STEP-heavy 文档生效）：

保留现有 `_docs_to_expand` / `_expand_hit_ids` 逻辑，但加 **per-doc**
门卫——只对真正含 ≥N 个 STEP 块的文档生效。这样：

- BatteryChangeSequenceSOP（11 STEP）→ 走 Layer 2
- agv_demo 其余 19 份告警 SOP（0 STEP）→ 走 Layer 1
- ifs_docs / 未来任何新 KB → 同上自动适配

```python
# 在 _prepare_chat_context 入口（伪代码）
# init() 时预计算 doc → step_count 映射
step_heavy_docs = self._step_heavy_docs()  # {doc_id: step_count >= 5}

# Layer 2: 仅对真正 STEP-heavy 的命中 doc 走整本扩展
hit_step_heavy_docs = {self._rows[i]['doc'] for i in hit_ids} & step_heavy_docs
if hit_step_heavy_docs:
    expanded_docs = self._docs_to_expand(hit_ids, q,
                                         allow_only=hit_step_heavy_docs)
    hit_ids = self._expand_hit_ids(hit_ids, q, expanded_docs)

# Layer 1: 短 chunk 邻居扩展（对所有命中都跑）
hit_ids = self._expand_short_chunks_with_neighbors(hit_ids)
```

**前置**：

- chunks.jsonl 需要 `prev_chunk_id` / `next_chunk_id` 字段（docx_parser 重切时加）
- `RagRunner.init()` 预计算 `_step_heavy_docs`（扫一遍 `_rows`，统计每个 doc 的
  STEP 块数；≥ 阈值的进入集合，阈值建议 5）
- 不需要任何 KB 类型标签或人工分类

**好处**：

- ifs_docs / agv_demo / 未来任何 KB 都能享受通用的语境补充
- STEP 文档保留现有"整本拉取"能力，但行为更精准
- 与 WeKnora 设计对齐，便于未来迁移参考

**风险**：

- chunks.jsonl schema 升级 → 需要重建所有 KB 的索引
- 邻居链跨 doc 边界要严格防御（避免拉到无关 chunk）

#### Option B：servers/retriever/parameter.yaml 配置化（中等，3-5 天）

仅在确实需要 per-KB 微调时再升级到 Option B。可与 Option A 叠加。

```yaml
short_chunk_expand:
  enabled: true
  min_len: 350
  max_len: 850
sop_step_expand:
  enabled: true
  per_kb_overrides:
    agv_demo:
      min_steps_for_expand: 2
      top_rank_for_single_step: 3
```

#### Option C：策略模式重构（大改造，1-2 周）

每个 `kb.type` 对应一个 `RetrievalStrategy` 类。Option A 充分满足时不必走 C。

### 4.2 推荐路径

**Phase 11.3 或 12.2 时实施 Option A**（双层扩展架构）。
- Layer 1 通用邻居扩展立刻消除 ifs_docs 的"运气"依赖
- Layer 2 STEP 全文扩展用门卫保住 agv_demo 行为
- 如未来需要 per-KB 微调再叠加 Option B

---

## 五、_rewrite_query 中性化（已完成）

**状态**：✅ 已实施（commit `75ee17c`，2026-05-28）。

旧 prompt：

```python
"- Keep AGV domain terms and technical nouns.\n"
"- For SOP/procedure questions, keep words like steps, procedure, sequence, battery replacement if relevant.\n"
```

新 prompt：

```python
"- Preserve technical domain terms, proper nouns, error/alarm IDs, "
"module/component names, and acronyms exactly as they appear.\n"
"- For procedure questions, keep words like steps, procedure, "
"sequence, workflow, configuration, setup if relevant.\n"
"- Do not introduce domain assumptions or terms not present in the "
"original query (do not add product/system names the user did not write).\n"
```

`tests/test_rag_runner_agent_mode.py::TestRewriteQueryPromptNeutrality`
4 测全过。

---

## 六、实施清单（未来某 sprint）

实施 Option A（双层扩展架构）时的 checklist：

- [ ] docx_parser 重切时为每个 chunk 写入 `prev_chunk_id` / `next_chunk_id`
- [ ] 重建 agv_demo / ifs_docs 的 chunks.jsonl 和 Qdrant collection
- [ ] 实现 `_expand_short_chunks_with_neighbors`（Layer 1，借鉴 WeKnora `merge_expand.go`）
- [ ] `RagRunner.init()` 预计算 `_step_heavy_docs` 集合（per-doc 探测，阈值 ≥ 5 STEP）
- [ ] `_docs_to_expand` 加 `allow_only` 参数，仅在 step-heavy doc 上扩展（Layer 2 门卫）
- [ ] `_prepare_chat_context` 入口加双层调用
- [ ] 单测：
  - [ ] doc 含 ≥5 STEP → 命中后走 Layer 2 整本扩展
  - [ ] doc 无 STEP（如 Alarm Block Battery Low SOP）→ 不走 Layer 2
  - [ ] 短 chunk → 走 Layer 1 邻居扩展
  - [ ] 长 chunk → Layer 1 跳过
  - [ ] 邻居链跨 doc 边界防御（PreChunkID 指向别家 doc 时丢弃）
- [ ] 跑 agv_demo 评测确认：
  - [ ] BatteryChangeSequenceSOP 类换电问题 Hit@5 不退化
  - [ ] 其余 19 份告警 SOP 类问题 Hit@5 应**上涨**（不再被 STEP 扩展挤占）
- [ ] 跑 ifs_docs 评测确认 Hit@5 仍 ≥ 1.00（应略涨：短 chunk 补足后语境更全）
- [ ] 用 gen_test KB 跑一遍确认正常
- [ ] 更新 rag_runner.py 模块 docstring：移除 "AGV 场景" 字样

---

## 七、相关文件

- [`custom_app/services/rag_runner.py`](../custom_app/services/rag_runner.py) — 硬编码所在
- [`custom_app/api/kb.py`](../custom_app/api/kb.py) — KB type 字段读取（_kb_type / KB_TYPE_*）
- [`custom_app/services/parsers/factory.py`](../custom_app/services/parsers/factory.py) — 解析器路由（已按 type 分流）

---

## 八、不要做的事

- **不要现在就动**——这次修复 commit `d60987e` 已经把 quick 模式核心检索 bug 修了；硬编码侥幸有效，重构会带来回归风险
- **不要简单删除 STEP 正则**——agv_demo 评测会回归
- **不要把所有逻辑改成 if/else 散在主流程**——用 Option A 的"门卫"模式集中分流
