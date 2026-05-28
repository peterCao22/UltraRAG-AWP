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

**翻译成技术语言**：

- 复用现有 `kb.type` 字段，**按 type 分流 RAG 行为**
- `sop_docx` 类：走 SOP 扩展机制（_docs_to_expand / _expand_hit_ids / STEP 解析）
- `general` 类：跳过所有 SOP 扩展，纯 RRF + rerank 输出

### 3.2 进一步发现

ifs_docs 暴露了一个 **额外维度**：即便都是 sop_docx，**结构也不同**——
- agv_demo 是"STEP 1/2/3 步骤型 SOP"
- ifs_docs 是"章节型操作手册"

可能需要细化为 3 类，或者用 **chunk 结构自动检测**而非依赖 type 字段。

---

## 四、重构方案

### 4.1 设计选项（按工作量从小到大）

#### Option A：纯 type-based 分流（最简单，1-2 天）

```python
# 在 _prepare_chat_context 入口
if self._kb_type == KB_TYPE_SOP_DOCX and self._has_step_chunks():
    # 走 SOP 扩展机制
    expanded_docs = self._docs_to_expand(hit_ids, q)
    hit_ids = self._expand_hit_ids(hit_ids, q, expanded_docs)
# 否则：纯 RRF + rerank 结果直接返回
```

**前置**：
- RagRunner.init() 时拉 `kb.type` 字段存到 `self._kb_type`
- 新增 `_has_step_chunks()` 自动探测（扫一遍 _rows 看是否有 _step_N）

**好处**：
- ifs_docs 自动跳过扩展（`_has_step_chunks()=False`）
- 新 KB 不用人工分类，靠 chunk 结构自动判断

**风险**：低。所有现在的硬编码逻辑**继续保留**但被"门卫"包住，agv_demo 行为不变。

#### Option B：servers/retriever/parameter.yaml 配置化（中等，3-5 天）

```yaml
sop_expansion:
  enabled: true                   # 全局开关
  per_kb_overrides:               # 可按 kb_id 覆盖
    agv_demo:
      enabled: true
      step_pattern: '_step_(\d+)'
      min_steps_for_expand: 2
      top_rank_for_single_step: 3
    ifs_docs:
      enabled: false              # 显式关闭
```

**好处**：完全数据驱动，运维可调
**风险**：配置膨胀，需要新加 admin 界面

#### Option C：策略模式重构（大改造，1-2 周）

每个 `kb.type` 对应一个 `RetrievalStrategy` 类：
- `SopExpansionStrategy`（agv-style，含 STEP 扩展）
- `SectionedManualStrategy`（ifs-style，section 全文）
- `GeneralRetrievalStrategy`（纯 RRF + rerank）

**好处**：未来加新 type 只加新类，不动主流程
**风险**：过度设计；可能 YAGNI

### 4.2 推荐路径

**Phase 11.3 或 12.2 时实施 Option A**（最简单，立刻消除"运气"）。
如果未来需要 per-KB 精调，再升级到 Option B。
Option C 留到第 4 个 KB 类型出现时再考虑。

---

## 五、_rewrite_query 中性化（优先级最高的快速修复）

L1252-1253 当前 prompt：

```python
"- Keep AGV domain terms and technical nouns.\n"
"- For SOP/procedure questions, keep words like steps, procedure, sequence, battery replacement if relevant.\n"
```

**问题**：所有 KB 的 query 都被注入 AGV 语境（"battery replacement"），ifs_docs 的"客户订单"query 都会被改写器加 AGV 偏见。

**修复**（5 分钟可以做）：

```python
"- Keep technical domain terms and proper nouns from the original query.\n"
"- For SOP/procedure questions, keep words like steps, procedure, sequence, workflow if relevant.\n"
```

去掉 "AGV" 和 "battery replacement" 即可。可单独 PR，不依赖其他重构。

---

## 六、实施清单（未来某 sprint）

实施 Option A 时的 checklist：

- [ ] RagRunner.init() 接收并保存 kb.type
- [ ] 实现 `_has_step_chunks()` 自动探测
- [ ] `_prepare_chat_context` 入口加分流逻辑
- [ ] 单测：sop_docx + step → 走扩展；sop_docx + 无 step → 跳过；general → 跳过
- [ ] 跑 agv_demo 评测确认 Hit@5 不退化
- [ ] 跑 ifs_docs 评测确认 Hit@5 仍 1.00
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
