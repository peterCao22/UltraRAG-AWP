# Phase 4 验收清单

> 制定时间：2026-05-11
> 验收范围：Phase 4.0 ~ 4.3 全部功能 + 4.4 端到端验证
> 前置文档：[PHASE4_PLAN.md](./PHASE4_PLAN.md)

本清单分四级：

- **L1 自动单元测试**：CI 内即可跑，无外部依赖
- **L2 自动集成测试**：需 .venv 装齐 `torch / mineru / docling`（可选）
- **L3 手动 API 测试**：需启动 Flask + 真实 KB
- **L4 用户验收**：你准备真实查询集，对比 Phase 3 / Phase 4

---

## 一、L1 自动单元测试（必过）

仅依赖 `.venv` 中已有的 `python-docx / numpy / pytest`，**5 秒内跑完**。

```powershell
.venv\Scripts\python.exe -m pytest `
  tests\test_docx_parser_schema.py `
  tests\test_faiss_vectorstore.py `
  tests\test_reranker_protocol.py `
  tests\test_markdown_parser.py `
  tests\test_parser_factory.py `
  tests\test_mineru_parser.py `
  tests\test_docling_parser.py `
  tests\test_kb_type_routing.py `
  tests\test_embedder_heading_path.py `
  tests\test_phase4_integration.py `
  -v
```

**期望**：149 passed + 5 skipped（skipped 来自缺 torch/mineru/docling 的环境）。

| 测试集 | 数量 | 验证目标 |
|--------|------|---------|
| `test_docx_parser_schema.py` | 5 | docx_parser 输出新 schema 字段，老字段保留 |
| `test_faiss_vectorstore.py` | 9 | VectorStore Protocol + FaissVectorStore 行为 |
| `test_reranker_protocol.py` | 5 | Reranker Protocol 合规性（3 项 torch 缺时 skip） |
| `test_markdown_parser.py` | 13 | MarkdownParser 标题切块、heading_path、图片 |
| `test_parser_factory.py` | 24 | (kb_type, ext) 路由表 + DocxParserAdapter |
| `test_mineru_parser.py` | 13 | MineruParser mock CLI + content_list 切块（1 项 mineru 缺时 skip） |
| `test_docling_parser.py` | 9 | DoclingParser mock docling（1 项 docling 缺时 skip） |
| `test_kb_type_routing.py` | 8 | KB type → \_parse_stage 双路径分发 |
| `test_embedder_heading_path.py` | 14 | compose_doc_embedding_text 拼接规则 |
| `test_phase4_integration.py` | 9 | 跨阶段端到端（parse → jsonl → embed） |

---

## 二、L2 自动集成测试（可选，需真实依赖）

需先安装可选依赖：

```powershell
uv sync --extras parsing
```

然后启用 marker：

```powershell
# MinerU 集成（GB 级模型首次下载）
.venv\Scripts\python.exe -m pytest -m requires_mineru -v

# Docling 集成
.venv\Scripts\python.exe -m pytest -m requires_docling -v
```

**期望**：每个集成测试用真实样本跑通，无 CLI/模型错误。

| 测试 | 准备 | 验证 |
|------|------|------|
| `test_integration_real_mineru_pdf` | 准备 `data/kb/general/raw/sample.pdf` | MinerU 真实跑通 PDF → 含 heading_path 的 chunks |
| `test_integration_real_docling_docx` | 已存在 `data/kb/ifs_docs/raw/*.docx` | Docling 真实跑通 DOCX → chunks |

---

## 三、L3 手动 API 测试（需启动 Flask）

启动后端：

```powershell
.venv\Scripts\python.exe -m custom_app.app --port 8080
```

### 3.1 创建 SOP KB（零回归）

```powershell
$body = '{ "kb_id": "sop_test", "name": "SOP 测试", "type": "sop_docx" }'
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/kb `
  -Method POST -ContentType "application/json" -Body $body
```

**期望**：返回 `{ kb_id, type: "sop_docx", status: "active" }`。`data/kb/sop_test/` 目录创建。

### 3.2 创建 general KB（Phase 4 新能力）

```powershell
$body = '{ "kb_id": "gen_test", "name": "通用测试", "type": "general" }'
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/kb `
  -Method POST -ContentType "application/json" -Body $body
```

**期望**：返回 `{ type: "general", ... }`。

### 3.3 无效 type 拒绝

```powershell
$body = '{ "kb_id": "bad_test", "name": "Bad", "type": "invalid" }'
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/kb `
  -Method POST -ContentType "application/json" -Body $body
```

**期望**：HTTP 400 + `error_code: "KB_TYPE_INVALID"`。

### 3.4 默认 type（兼容旧客户端）

```powershell
$body = '{ "kb_id": "legacy_test", "name": "Legacy" }'   # 不传 type
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/kb `
  -Method POST -ContentType "application/json" -Body $body
```

**期望**：返回 `type: "sop_docx"`（默认值）。

### 3.5 upload 白名单（SOP）

向 `sop_test` 上传 `.docx`：通过；上传 `.pdf` / `.md`：拒绝（400 + `NO_VALID_FILE`，提示 "allowed: .docx"）。

### 3.6 upload 白名单（general）

向 `gen_test` 上传 `.pdf` / `.png` / `.md`：通过；上传 `.xyz`：拒绝。

### 3.7 list_kb 返回 type 字段

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/kb -Method GET
```

**期望**：每个 KB 对象含 `type` 字段。

### 3.8 老 KB 自动迁移到 sop_docx

```powershell
# 用 sqlite 直接看：所有老库的 type 列应为 'sop_docx'
.venv\Scripts\python.exe -c "import sqlite3; conn = sqlite3.connect('db/app.sqlite'); print(list(conn.execute('SELECT kb_id, type FROM knowledge_bases')))"
```

**期望**：所有 KB 的 `type = 'sop_docx'`，行为不变。

### 3.9 前端 admin 页面

打开 `http://127.0.0.1:8080/admin.html`：

- 点击「新建知识库」→ 应看到「知识库类型」下拉，默认「SOP 知识库」，可切到「通用知识库」
- 进入任意 KB 详情 → 头部应显示「类型 SOP / 通用（创建后不可改）」
- 创建后 type 不可改（无 UI 入口）

---

## 四、L4 用户验收（你准备数据）

### 4.1 SOP 库零回归

**目的**：确认 Phase 4 改动不影响 `agv_demo` / `ifs_docs` 等现有 SOP KB。

```powershell
# 重新 ingest 现有 KB（按 admin 页面「重建索引」按钮，或直接 POST）
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/kb/agv_demo/ingest `
  -Method POST -ContentType "application/json" -Body '{ "force_reindex": true }'
```

**验证**：

- 重建过程不报错
- chunks.jsonl 数量与 Phase 3 一致或非常接近（小幅变化可能来自 STEP 检测精度）
- 现有问答查询答案语义与 Phase 3 等价（用户自行准备 5-10 个对照查询）

### 4.2 General KB 端到端

1. 创建 general KB
2. 上传：1 个 PDF + 1 个 PNG（含中文）+ 1 个 MD
3. 点击「入库」
4. 入库完成后从对话页问：
   - 关于 PDF 内容的问题
   - 关于 PNG 中文字的问题（OCR）
   - 关于 MD 内容的问题
5. 检查 SSE 返回的 chunk 引用是否来自正确文档

**前置**：用户已 `uv sync --extras parsing` 装好 MinerU + Docling。

### 4.3 heading_path 嵌入效果

```powershell
# 准备 query 文件
copy custom_app\scripts\eval_queries_example.txt custom_app\scripts\eval_queries.txt
# 编辑成你关心的真实问题

# 跑 A/B 验证（先小规模）
.venv\Scripts\python.exe -m custom_app.scripts.eval_heading_path `
  --kb agv_demo `
  --queries custom_app\scripts\eval_queries.txt `
  --top-k 5 `
  --max-chunks 30 `
  --json eval_result.json
```

**期望**：

- 至少部分 query 有「*」标记（enhanced 新增命中）或「^」（排名提升）
- 平均 Jaccard 在 0.5-0.9 范围（变化适中，既有改动又不至于全替换）

### 4.4 reranker 模型搬家验证

修改 `servers/retriever/parameter.yaml`：

```yaml
rag_rerank:
  model_name_or_path: \\fileserver\models\bge-reranker-v2-m3  # 或局域网共享路径
```

重启 Flask，对话流程正常 → 验证 reranker YAML 化成功。

---

## 五、必过项 vs 可选项

| 等级 | 内容 | 状态 |
|------|------|------|
| **必过** | L1 全部 149 项自动单元测试 | ✅ 已通过 |
| **必过** | L3.1 / L3.2 / L3.3 / L3.7 / L3.8 / L3.9 API + 前端 | ⏳ 需启动 Flask |
| **必过** | L4.1 SOP 库零回归 | ⏳ 用户验证 |
| 可选 | L2 真实 MinerU / Docling 集成 | ⏳ 装可选依赖 |
| 可选 | L4.2 general KB E2E（依赖 L2） | ⏳ 装可选依赖 |
| 可选 | L4.3 heading_path A/B | ⏳ 需 Google API key |
| 可选 | L4.4 reranker 路径切换 | ⏳ 模型搬家时 |

---

## 六、问题排查

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| `import faiss` 失败 | .venv 没装 faiss | `uv sync --extras retriever` 或在测试中 mock |
| MinerU CLI 找不到 | 未装 `mineru[core]` | `uv sync --extras parsing` |
| Docling 找不到 | 未装 `docling` | `uv sync --extras parsing` |
| reranker 启动慢 | 首次加载 GB 级模型 | 正常；后续单例缓存 |
| general KB 上传 .docx 失败 | 走 DoclingParser，需要 docling | 装可选依赖 |
| `KB_TYPE_INVALID` 错误 | 传了不在枚举中的 type | 检查前端表单或 POST body |

---

## 七、Phase 4 完成判据

下面三条任一不满足，Phase 4 视为未完成：

1. ✅ L1 自动测试 100% 通过（149 passed + 5 skipped 是预期结果）
2. ⏳ L3 手动 API 测试至少 6 项必过项跑通（3.1/3.2/3.3/3.7/3.8/3.9）
3. ⏳ L4.1 SOP 库零回归（重建 `agv_demo` 索引后查询答案语义一致）

满足后即可进入 Phase 5。
