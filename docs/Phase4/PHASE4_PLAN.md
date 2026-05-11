# Phase 4 实施计划 — 解析层扩展 + 检索准确率提升

> 制定时间：2026-05-11
> 预期工时：11.5-16.5 天（约 2.5-3.5 周）
> 前置文档：[comparing_custom_app_vs_rag_anything.md](./comparing_custom_app_vs_rag_anything.md)
> 后续阶段：[../Phase5/PHASE5_OUTLINE.md](../Phase5/PHASE5_OUTLINE.md)

---

## 一、目标与范围

### 1.1 核心目标

1. **扩展文档解析能力**：从仅 DOCX 扩展到 PDF / 图片 / 通用 DOCX / Markdown
2. **提升检索准确率**：通过 `heading_path` 嵌入增强，强化标题语义
3. **保留 SOP 路径精度**：现有 `docx_parser.py` 业务定制分块零回归
4. **为 Phase 5 预留接口**：抽象 `VectorStore` Protocol，Phase 5 迁移 Qdrant 时零侵入

### 1.2 不在范围

- ❌ VLM 多模态检索（未来 CLIP 路线，与 RAG-Anything 的 VLM 路线不同）
- ❌ 检索/Agent 层重写（保留现有 `RagRunner` / `AgentRunner`）
- ❌ 嵌入后端替换（保留 Gemini，仅在拼接文本上做增强）
- ❌ 存储栈迁移（Qdrant / Postgres / Neo4j 留给 Phase 5）
- ❌ BM25 混合检索（留给 Phase 6 或 Phase 4.5 候选）

### 1.3 验收标准

| # | 验收项 | 标准 |
| --- | --- | --- |
| 1 | SOP 库零回归 | `agv_demo` / `ifs_docs` 重建索引后命中率与 Phase 3 一致或更高 |
| 2 | 新格式可摄取 | 新建 `general` 库能成功跑通 PDF / 图片 / MD / 通用 DOCX 入库流程 |
| 3 | 检索准确率提升 | 用户准备 10 个典型查询，Phase 4 Top-3 命中率 ≥ Phase 3 |
| 4 | Phase 5 接口就绪 | `VectorStore` Protocol 落地，`FaissVectorStore` 完整实现 |

---

## 二、已确认设计决策

### 2.1 三大基线决策（用户拍板，2026-05-11）

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 通用解析器 | **MinerU（PDF/图片）+ Docling（DOCX）** | MinerU 处理 PDF/扫描件最强；通用 DOCX 走 Docling 避免 PDF 中转有损 |
| Phase 4 范围 | **解析层扩格式 + heading_path 嵌入增强** | 实打实提升解析覆盖和检索准确率 |
| KB 类型暴露 | **创建时下拉选择，事后不可改** | `sop_docx` 和 `general` 作为两种产品形态 |

### 2.2 三个 plan 阶段补充决策（用户拍板）

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 老库迁移策略 | **软迁移**（补 schema 默认字段，不重新解析） | 老库精度已验证，重解析有回归风险 |
| 依赖管理 | **可选依赖** `uv sync --extras parsing` | 避免普通用户被 GB 级模型下载吓退 |
| heading_path 拼接 | **自然语言路径** `"故障处理 > 电池告警\n<title>\n<contents>"` | 与现有 Gemini 嵌入风格一致 |

### 2.3 Phase 5 衔接决策（用户拍板）

| 决策点 | 选择 |
| --- | --- |
| Phase 5 启动时机 | Phase 4 完成后紧接着做 |
| VectorStore 接口预留 | **预留** —— Phase 4.0 顺手抽象出 Protocol + FaissVectorStore |
| Phase 5 优先级 | 先 Qdrant + PostgreSQL；Neo4j 后续按需评估 |

---

## 三、统一 chunks.jsonl Schema

```json
{
  "id": "doc_stem_chunk_001",
  "title": "...",
  "contents": "...",
  "doc": "原始文件名",
  "kb_id": "agv_demo",
  "images": [
    {"path": "...", "caption": "...", "page_idx": 12, "img_id": "..."}
  ],

  "source_type": "sop_docx | general_pdf | image | markdown",
  "parser": "docx_parser | mineru | docling | markdown",

  "structure": {
    "heading_path": ["第3章 故障处理", "3.2 电池告警"],
    "heading_level": 2,
    "step_number": 5,
    "page_idx": 12
  },

  "tables": [
    {"markdown": "| ... |", "caption": "...", "page_idx": 12}
  ],

  "vector_id": null
}
```

**字段设计要点**：

- 老字段 `id / title / contents / doc / images` 完全保留 → 老 chunks.jsonl 仍可读
- `images[]` 从字符串数组升级为对象数组（带 `img_id` 为未来 CLIP 留钩子）
  - 兼容性：解析侧统一输出对象数组；嵌入/检索侧 `.get` 兼容旧字符串
- `structure.heading_path` 是数组形式的标题链 → 检索时可做"父级标题加权"
- `structure.step_number` 仅 SOP 文档有
- `vector_id: null` —— Phase 4 留空，Phase 5 迁 Qdrant 时填 point id
- `source_type` / `parser` —— 调试/回归追踪字段

---

## 四、阶段拆分与依赖图

### 4.1 依赖关系

```
Phase 4.0 (基础设施)
    ↓
    ├── Phase 4.1 (新 parser)  ─┐
    ↓                            ↓
Phase 4.3 (嵌入增强)         Phase 4.2 (KB 路由)
    ↓                            ↓
    └────────→ Phase 4.4 (测试) ←┘
```

**并行机会**：4.1 和 4.3 互不依赖可并行；4.2 必须等 4.1 完成。

### 4.2 工时估算

| 阶段 | 内容 | 工时 | 复杂度 |
| --- | --- | --- | --- |
| 4.0 | 基础设施 + VectorStore 抽象 + rerank 统一 | 2.5-3.5 天 | LOW |
| 4.1 | 新 parser 接入 | 4-6 天 | **HIGH** |
| 4.2 | KB 管理层路由 | 2-3 天 | MEDIUM |
| 4.3 | heading_path 嵌入增强 | 1 天 | LOW |
| 4.4 | 测试与验收 | 2-3 天 | MEDIUM |
| **总计** | | **11.5-16.5 天** | **MEDIUM-HIGH** |

---

## 五、Phase 4.0 — 基础设施

**目标**：DB schema 升级 + 解析器抽象 + VectorStore 抽象。后续所有工作的地基。

### 5.1 任务清单

| # | 任务 | 文件 | 说明 |
| --- | --- | --- | --- |
| 0.1 | DB 加 `type` 列 | `custom_app/db.py` | `ALTER TABLE knowledge_bases ADD COLUMN type TEXT NOT NULL DEFAULT 'sop_docx'`；按现有 `reasoning_json` 同款迁移模式实现 |
| 0.2 | 定义 Parser Protocol | `custom_app/services/parsers/base.py`（新增） | `Protocol` 定义 `parse(file_path, kb_root) -> List[Chunk]` |
| 0.3 | 定义新 Chunk schema | `custom_app/services/parsers/schema.py`（新增） | `@dataclass` 定义 `Chunk` / `ChunkStructure` / `ChunkImage` / `ChunkTable`；含 `vector_id` 字段；序列化函数 `to_jsonl_dict()` |
| 0.4 | docx_parser 适配新 schema | `custom_app/services/docx_parser.py` | `pack_chunk` 内补 `source_type="sop_docx"` / `parser="docx_parser"` / `structure.step_number` / `structure.heading_path`；老字段完全保留 |
| 0.5 | VectorStore Protocol + FaissVectorStore | `custom_app/services/vectorstore/base.py` + `faiss_store.py`（新增） | Protocol 定义 `upsert / search / delete`；FaissVectorStore 把 `rag_runner.py` 里散落的 FAISS 加载/检索逻辑包装进来；RagRunner 改依赖注入接收 VectorStore |
| 0.6 | rerank 统一到 LocalReranker + Reranker Protocol 抽象 | `custom_app/services/reranker/base.py`（新增） + `custom_app/utils/local_reranker.py` + `custom_app/services/rag_runner.py` | **(a)** 新增 `Reranker` Protocol（`rerank_items(query, items, content_key, top_k)`），为 Phase 4 之后 reranker 服务化（HttpReranker）预留接口；**(b)** `LocalReranker` 实现 Protocol；**(c)** 把 `rag_runner._ensure_rerank_model` + `_rerank_hit_ids` (rag_runner.py:1043-1145) 改写为通过 Protocol 调用 `LocalReranker`；**(d)** 模型路径从 `servers/retriever/parameter.yaml` 的 `rag_rerank.model_name_or_path` 读取（替换硬编码 `C:\reranker\...`），便于未来模型搬家；**(e)** 保留 `enabled` / `batch_size` / `device` 配置；**(f)** 把 CUDA 失败 fallback CPU 的逻辑搬进 LocalReranker；**(g)** 删除 `sentence_transformers.CrossEncoder` 依赖（如其他地方未引用） |

### 5.2 风险

| 等级 | 风险 | 缓解 |
| --- | --- | --- |
| 🟡 | `kb_documents` / `kb_jobs` 表是否需要存 source_type | 暂不需要，doc-level 类型从 `file_type` 列推断 |
| 🟢 | ALTER TABLE 失败 | SQLite ALTER ADD COLUMN 是安全操作，按现有迁移模式 |
| 🟡 | VectorStore 抽象破坏 RagRunner 现有行为 | FaissVectorStore 是纯包装层，逻辑完全等价，先写单元测试覆盖 |

### 5.3 测试

- 单元：DB 迁移后老 KB 仍能读取（type 默认 `sop_docx`）
- 单元：FaissVectorStore 与原 FAISS 直调结果一致（同 query 同 chunk 命中相同）

---

## 六、Phase 4.1 — 通用 Parser 接入

**目标**：MinerU / Docling / Markdown 三个新 parser 落地，输出统一 schema chunk。

### 6.1 任务清单

| # | 任务 | 文件 | 说明 |
| --- | --- | --- | --- |
| 1.1 | 实现 `MineruParser` | `custom_app/services/parsers/mineru_parser.py`（新增） | `subprocess.run(["mineru", "-p", ...])`，读取 `<stem>_content_list.json`；**直接在 content_list 上跑分块**，不调用 RAG-Anything 的 `separate_content()`；按 `text_level` 重建 `heading_path` |
| 1.2 | 实现 `DoclingParser` | `custom_app/services/parsers/docling_parser.py`（新增） | Docling 直接解析 DOCX 输出 markdown 或结构化对象；按 heading 级别切块；重建 `heading_path` |
| 1.3 | 实现 `MarkdownParser` | `custom_app/services/parsers/markdown_parser.py`（新增） | 轻量实现，按 `#` 级别切块，无外部依赖 |
| 1.4 | Parser 工厂 | `custom_app/services/parsers/factory.py`（新增） | 输入 `(kb_type, file_ext)`，返回 Parser 实例 |
| 1.5 | 可选依赖配置 | `pyproject.toml` | `[project.optional-dependencies] parsing = ["mineru>=...", "docling>=..."]` |

### 6.2 Parser 路由表

| kb_type | 文件扩展名 | Parser | 备注 |
| --- | --- | --- | --- |
| `sop_docx` | `.docx` | `DocxParser`（现有） | SOP 业务定制 |
| `general` | `.docx` | `DoclingParser` | 通用 DOCX 直读 |
| `general` | `.pdf` | `MineruParser` | 含扫描件 OCR |
| `general` | `.png` / `.jpg` / `.jpeg` / `.bmp` / `.tiff` | `MineruParser` | OCR 模式 |
| `general` | `.md` / `.markdown` | `MarkdownParser` | 轻量解析 |
| `general` | `.txt` | `MarkdownParser` | 按段落切块 |

### 6.3 风险

| 等级 | 风险 | 缓解 |
| --- | --- | --- |
| 🔴 | MinerU 模型首次下载 GB 级 | README + Phase4 doc 明示；可选依赖避免强制下载 |
| 🔴 | Windows 下找不到 `mineru` 可执行 | `shutil.which("mineru")` 前置检测 + 友好报错 |
| 🟡 | Docling 中文 DOCX 实测效果未知 | Phase 4.1 第一步用 `data/kb/ifs_docs/raw/` 样本实测；不行 fallback 到 docx_parser 通用模式 |
| 🟢 | MarkdownParser 复杂度 | 零外部依赖，纯字符串处理 |

### 6.4 测试

- 集成：`tests/test_mineru_parser.py` —— 一个小 PDF 样本，断言 chunk 数 > 0、heading_path 至少有一个非空
- 集成：`tests/test_docling_parser.py` —— `data/kb/ifs_docs/raw/` 样本
- 单元：`tests/test_markdown_parser.py` —— 多级标题切分正确性
- CI 策略：`@pytest.mark.requires_mineru` / `requires_docling` 标记，默认 skip，本地手动验证

---

## 七、Phase 4.2 — KB 管理层路由

**目标**：让 API/前端理解 KB type，改造硬编码 `.docx` 为多扩展名 + parser 路由。

### 7.1 任务清单

| # | 任务 | 文件 | 说明 |
| --- | --- | --- | --- |
| 2.1 | KB 创建 API 加 type | `custom_app/api/kb.py` `create_kb()` | 接受 `type` 入参，校验枚举值 `sop_docx`/`general`，写入 DB |
| 2.2 | 修复硬编码 `.docx` | `custom_app/api/kb.py` `_register_documents`/`_parse_stage` | 改成扫描支持的扩展名集合；按 KB type + 扩展名通过工厂分发 |
| 2.3 | upload 白名单扩展 | `custom_app/api/kb.py:463` `_ALLOWED_EXTENSIONS` | 按 kb.type 动态返回允许扩展名 |
| 2.4 | 前端 KB 创建对话框加下拉 | `custom_app/frontend/admin.html` + `admin.js` | 字段 `type`，选项：`SOP 知识库 (sop_docx)` / `通用知识库 (general)` |
| 2.5 | 前端编辑禁用 type 字段 | `custom_app/frontend/admin.html` | 编辑模式 type 字段 disabled 灰显 |

### 7.2 风险

| 等级 | 风险 | 缓解 |
| --- | --- | --- |
| 🟡 | `_parse_stage` 现在调 `parse_directory`（批量），新工厂是文件级 | 在 `_parse_stage` 里改成逐文件分发，专门加测试覆盖 SOP 路径回归 |
| 🟡 | 前端 admin.html 是 Vue 3 | 先看清现有组件结构再改 |
| 🟢 | upload_documents 改动小 | — |

### 7.3 测试

- 单元：`tests/test_kb_type_routing.py` —— mock factory，断言不同 (type, ext) 组合返回正确 parser
- 集成：手动 —— 新建一个 general KB，上传 PDF + PNG + MD 跑完整流程

---

## 八、Phase 4.3 — heading_path 嵌入增强

**目标**：把标题层级链拼到嵌入输入文本前面，提升检索语义。

### 8.1 任务清单

| # | 任务 | 文件 | 说明 |
| --- | --- | --- | --- |
| 3.1 | 修改嵌入文本拼接 | `custom_app/services/google_embedder.py:134` | `heading_str = " > ".join(chunk.get("structure", {}).get("heading_path", []))`<br>最终文本 = `f"{heading_str}\n{title}\n{contents}".strip()`（无 heading_path 时退化原行为，老库零回归） |
| 3.2 | Query 侧保持不变 | 同上 | 只在 doc 侧增强，query 仍用原文嵌入 |
| 3.3 | A/B 验证脚本 | `custom_app/scripts/eval_heading_path.py`（新增） | 用现有 KB 跑同一组查询，比较有/无 heading_path 的 Top-K 命中 |

### 8.2 风险

| 等级 | 风险 | 缓解 |
| --- | --- | --- |
| 🟢 | 老库 chunk 无 heading_path | 字段为空数组时退化原行为 |
| 🟢 | 改动只有一个函数 | 回滚成本极低 |

### 8.3 测试

- 单元：`tests/test_embedder_heading_path.py` —— mock `embed_texts`，断言 chunk 有 heading_path 时输入文本含 `" > "`，没有时与 Phase 3 一致

---

## 九、Phase 4.4 — 测试与验收

### 9.1 测试清单

| # | 测试 | 文件 | 类型 |
| --- | --- | --- | --- |
| 4.1 | docx_parser 新 schema 兼容性 | `tests/test_docx_parser_schema.py`（新增） | 单元 |
| 4.2 | MineruParser 集成 | `tests/test_mineru_parser.py`（新增） | 集成（标记 skip） |
| 4.3 | DoclingParser 集成 | `tests/test_docling_parser.py`（新增） | 集成（标记 skip） |
| 4.4 | MarkdownParser 单元 | `tests/test_markdown_parser.py`（新增） | 单元 |
| 4.5 | KB type 路由 | `tests/test_kb_type_routing.py`（新增） | 单元 |
| 4.6 | heading_path 嵌入 | `tests/test_embedder_heading_path.py`（新增） | 单元 |
| 4.7 | VectorStore 等价性 | `tests/test_faiss_vectorstore.py`（新增） | 单元 |
| 4.8 | 端到端冒烟 | 手动 | 手动验收 |
| 4.9 | SOP 回归 | 手动 | 重建 `agv_demo` 索引，对比 Phase 3 |

### 9.2 验收流程

1. 跑所有 unit + integration（CI 默认 skip MinerU/Docling）
2. 本地启动 MinerU + Docling，跑 skip 标记的集成测试
3. 重建 `agv_demo` 索引，对比 Phase 3 命中率
4. 新建 general KB，跑 PDF + 图片 + MD 摄取
5. 用户准备 10 个典型查询，对比 Phase 3 vs Phase 4 Top-3

---

## 十、关键文件清单

### 10.1 新增（13 个）

```
custom_app/services/parsers/__init__.py
custom_app/services/parsers/base.py
custom_app/services/parsers/schema.py
custom_app/services/parsers/factory.py
custom_app/services/parsers/mineru_parser.py
custom_app/services/parsers/docling_parser.py
custom_app/services/parsers/markdown_parser.py
custom_app/services/vectorstore/__init__.py
custom_app/services/vectorstore/base.py
custom_app/services/vectorstore/faiss_store.py
custom_app/scripts/eval_heading_path.py
tests/test_docx_parser_schema.py
tests/test_mineru_parser.py
tests/test_docling_parser.py
tests/test_markdown_parser.py
tests/test_kb_type_routing.py
tests/test_embedder_heading_path.py
tests/test_faiss_vectorstore.py
```

### 10.2 修改（7 个）

```
custom_app/db.py                        # 加 type 列 + 迁移
custom_app/services/docx_parser.py      # pack_chunk 写入新 schema
custom_app/services/rag_runner.py       # 改用 VectorStore 依赖注入
custom_app/services/google_embedder.py  # heading_path 拼接
custom_app/api/kb.py                    # type 路由 + 多扩展名
custom_app/frontend/admin.html          # type 下拉
custom_app/frontend/admin.js            # type 字段绑定
pyproject.toml                          # parsing 可选依赖
```

### 10.3 文档

```
docs/Phase4/comparing_custom_app_vs_rag_anything.md  # 已存在，补 Phase 5 预告
docs/Phase4/PHASE4_PLAN.md                           # 本文档
docs/Phase4/IMPLEMENTATION_NOTES.md                  # 实施过程记录（实施时新增）
docs/Phase5/PHASE5_OUTLINE.md                        # Phase 5 概要
CLAUDE.md                                            # 更新 Phase 4 开发命令
```

---

## 十一、风险总览

| 等级 | 风险 | 缓解 |
| --- | --- | --- |
| 🔴 HIGH | MinerU 部署成本（GB 模型 + LibreOffice） | 可选依赖 + 文档明示 + 健康检查 |
| 🔴 HIGH | MinerU Windows CLI 找不到 | `shutil.which("mineru")` 前置检测 |
| 🟡 MED | Docling 中文 DOCX 实测未知 | 实测优先；不行 fallback |
| 🟡 MED | `_parse_stage` 改动破坏 SOP 流 | 专门覆盖 SOP 路径回归测试 |
| 🟡 MED | 老库缺新字段导致下游报错 | schema 用 `.get(default)` 防御性读取 |
| 🟡 MED | VectorStore 抽象包装错误 | FaissVectorStore 等价性测试 |
| 🟢 LOW | heading_path 影响嵌入分布 | 老库空数组退化为原行为 |
| 🟢 LOW | DB ALTER TABLE | 沿用现有迁移模式 |

---

## 十二、向后兼容矩阵

| 场景 | 处理方式 |
| --- | --- |
| 老 KB（无 `type` 字段） | DB 迁移自动填 `'sop_docx'`，行为不变 |
| 老 chunks.jsonl（无新字段） | 嵌入侧 `.get("structure", {}).get("heading_path", [])` 默认空，退化 Phase 3 |
| 老 doc 不重新解析 | 软迁移策略，老库检索质量保持 |
| 新 KB 设为 `sop_docx` | 仍走 `docx_parser`，分块逻辑不变，chunk 多了 schema 字段 |
| 老 chunk `images` 是字符串数组 | 解析侧统一输出对象数组；下游 `.get` 兼容 |
| RagRunner 未来切 Qdrant | 通过 VectorStore Protocol 注入，RagRunner 代码不变 |

---

## 十三、执行注意事项

1. **每阶段完成后做 git commit**，便于回滚
2. **MinerU/Docling 实测前先准备样本**：从 `data/kb/ifs_docs/raw/` 选 2-3 个有代表性的中文文档
3. **VectorStore 抽象优先于新 parser**：地基稳了再盖楼
4. **前端改动单独提 PR**：方便用户评审 UI
5. **`docs/Phase4/IMPLEMENTATION_NOTES.md` 实时更新**：记录实施中遇到的偏差和决策

---

> **本文档作为 Phase 4 执行基线**。每个阶段开始前再 review 一次本计划，确认无偏差。
