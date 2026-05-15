# Phase 6.1 —— 文档级状态显示 + 详情预览（WeKnora 风格）

> **状态**：计划已确认（2026-05-15），待开工
> **前置**：[Phase 6.0](./PHASE_6_COMPLETION.md)（Ingest + KG stage 已就绪）；项目已固定 Postgres
> **与 Phase 7 边界**：Phase 7 管对话模型切换；本阶段管 **KB Admin 页的文档状态与预览**，互不合并验收
> **参考实现**：`D:\Peter2025\myCursor\WeKnora`
> - 后端状态枚举：`internal/types/knowledge.go`（`ParseStatus*`）
> - 卡片渲染：`frontend/src/views/knowledge/KnowledgeBase.vue`（行 1858-1953）
> - 详情面板：`frontend/src/components/doc-content.vue`（merged / chunks / preview 三视图）
> - 轮询：`KnowledgeBase.vue` 行 720-777（`setInterval(1500ms)` + `batchQueryKnowledge`）

---

## 一、目标与现状

### 1.1 问题

删除文件 → 重新上传 → 「重建索引」时若文件多，界面只能看到粗粒度 `running · 阶段 qdrant`，用户无法判断：
- 具体哪个文件已完成、哪个失败、哪个卡住
- 失败的话错在哪一步、错误信息是什么
- 完成的文档究竟拆成了几块、分块内容是什么

### 1.2 目标

1. **每个文档独立状态**：列表里每行显示当前所处阶段（解析中 / 嵌入中 / 索引中 / 完成 / 失败），完全照 WeKnora 风格
2. **失败有上下文**：失败行可悬停查看 `error_message`，支持单文件重试
3. **完成可看内容**：点击文档卡片打开详情面板，**chunks** + **merged** 两视图（preview 推后，见 §六）
4. **不做"百分比进度条"**（你已确认）；用 `"3/12 已完成 · 1 失败 · 8 处理中"` 这种汇总文字替代

### 1.3 现状基线

- `kb_documents` 表（Postgres + SQLite 双后端）已有 `status` 字段（默认 `pending`），枚举原本只有 `pending/done/...`，本期需要**扩展枚举值**
- `error_message` 字段已存在但未充分利用
- `_run_ingest_job` 已按 stage 推进（parse / embed / index / qdrant / kg），但只更新 `kb_jobs` 表的整体 stage，**没有逐文档写状态**

---

## 二、数据模型变更

### 2.1 `kb_documents` 表迁移

```sql
-- migrations/postgres/00X_phase6_1_doc_status.sql
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS chunk_count  INTEGER NOT NULL DEFAULT 0;

-- 同步 SQLite（custom_app/db.py 的 CREATE TABLE 已用 IF NOT EXISTS，需补 ALTER 兼容老 db）
```

SQLite 后端在 `db.py` 启动时检测列是否存在，没有则 `ALTER TABLE ADD COLUMN`。

### 2.2 `status` 枚举（扩展，向后兼容）

| 值 | 含义 | 来自 |
|---|---|---|
| `pending` | 已登记，待处理 | 上传完成时写入（已有） |
| `parsing` | 正在解析（docx → chunks） | 新增；`_parse_stage` 进入时写 |
| `embedding` | 正在生成 embedding | 新增；`_embed_stage` 进入时写 |
| `indexing` | 正在写入向量索引（FAISS/Qdrant） | 新增；`_index_stage` / `_qdrant_stage` 进入时写 |
| `completed` | 完成 | 已有（兼容老值 `done`，repo 读取时映射） |
| `failed` | 失败 | 已有；同时写 `error_message` |
| `deleting` | 删除中（防异步任务冲突） | 新增；删除 API 入口时写 |

> 与 WeKnora 对照：WeKnora 只有粗粒度 `processing`，我们拆成 `parsing/embedding/indexing` 三态，让用户看到"卡在哪一步"。完成态用 `completed`（不用 `done`），repo 层做双向兼容。

### 2.3 不动 `kb_jobs` 表

KG 状态（`kg_status`）已在 Phase 6.0 落到 `kb_jobs.result_json`，本期保留不动。文档级状态只写 `kb_documents.status`。

---

## 三、后端实现

### 3.1 `kb_repository.py` 新增

```python
def update_document_status(kb_id: str, doc_id: str, status: str, error_message: str | None = None,
                           chunk_count: int | None = None, processed_at: bool = False) -> None:
    """单文档状态原子更新；processed_at=True 时写当前时间"""

def batch_get_documents(kb_id: str, doc_ids: list[str]) -> list[dict]:
    """轮询用：只取指定 doc_ids 的 status/error_message/chunk_count/updated_at"""

def list_documents_with_status(kb_id: str) -> list[dict]:
    """文档列表 API 用：所有字段 + 派生的 summary（每状态计数）"""
```

### 3.2 `api/kb.py` 的 `_run_ingest_job` 改造

按子阶段在循环内**逐文档**更新状态：

```python
# 伪代码
for doc in documents_to_process:
    doc_id = doc['doc_id']
    try:
        update_document_status(kb_id, doc_id, status='parsing')
        chunks = parse_docx(doc['file_path'])

        update_document_status(kb_id, doc_id, status='embedding')
        embeddings = embed(chunks)

        update_document_status(kb_id, doc_id, status='indexing')
        index_write(embeddings)

        update_document_status(kb_id, doc_id, status='completed',
                               chunk_count=len(chunks), processed_at=True)
    except Exception as e:
        logger.exception(f"doc {doc_id} failed")
        update_document_status(kb_id, doc_id, status='failed', error_message=str(e)[:500])
        # 不抛出 —— 其它文档继续
```

KG stage（Phase 6.0 加的）继续按 KB 整体跑，**不**关联到具体 doc。

### 3.3 启动时的"卡死恢复"

`app.py` 启动时扫一遍：

```python
# custom_app/services/doc_status_recovery.py
def recover_stale_documents():
    """Flask 进程崩溃后，把超过 10 分钟仍在 parsing/embedding/indexing 的标 failed"""
    threshold = now - 10min
    rows = kb_repo.find_stale_processing_documents(threshold)
    for row in rows:
        update_document_status(row['kb_id'], row['doc_id'], status='failed',
                               error_message='进程异常中断，请重试')
```

### 3.4 API 路由

| 方法 | 路径 | 用途 | 备注 |
|---|---|---|---|
| `GET`    | `/api/kb/<kb_id>/documents`             | 列表（已存在），**补**返回 `status/error_message/chunk_count/processed_at` + 顶部 summary `{pending:0, parsing:1, ..., failed:2}` | 派生字段 |
| `POST`   | `/api/kb/<kb_id>/documents/batch-status` | body `{doc_ids:[...]}` → 返回这几个的最新状态 | 轮询用，比全列表轻 |
| `POST`   | `/api/kb/<kb_id>/documents/<doc_id>/retry` | 重新跑这个文档的 ingest 子流程 | 单文件失败重试 |
| `GET`    | `/api/kb/<kb_id>/documents/<doc_id>/chunks` | 取该文档全部 chunks（**新增**或确认已存在） | 详情面板 chunks 视图用 |

### 3.5 取消 / 终止

MVP 不做"用户手动取消整个 job"（要做就要插 cancel flag + 主循环 check）。**软性方案**：用户删除 KB → `_run_ingest_job` 下一轮迭代发现 KB 状态变了就退出。完整取消推到后续。

---

## 四、前端实现

### 4.1 KB 详情页 - 文档列表（admin.js + admin.html + style.css）

**汇总条**（顶部）：
```
[ifs_docs] 共 12 个文档 · 8 已完成 · 1 解析中 · 1 嵌入中 · 0 索引中 · 0 待处理 · 2 失败 · 0 删除中
```

**每行文档卡片**：
- 左侧：文件名 + 文件大小 + 上传时间
- 右侧状态徽章（参考 WeKnora `card-analyze` 块）：

| status | 显示 |
|---|---|
| `pending` | 灰色徽章「待处理」 |
| `parsing` | 蓝色 + 旋转图标「解析中…」 |
| `embedding` | 蓝色 + 旋转图标「嵌入中…」 |
| `indexing` | 蓝色 + 旋转图标「写入索引…」 |
| `completed` | 绿色徽章「N 个分块」 |
| `failed` | 红色 ❌「解析失败」+ hover 显示 `error_message` + 「重试」按钮 |
| `deleting` | 灰色徽章「删除中」 |

- **最右侧 `more` dropdown 菜单**（本期只放「查看分块」+「删除」；Phase 6.2 会扩 "重建该文件" / 批量操作，UI 框架本期一次性铺好）：
  - 「查看分块」→ 打开详情面板（§4.2）
  - 「删除文件」→ 二次确认 → 调现有 `DELETE /api/kb/<kb_id>/documents?doc_id=X`
  - Phase 6.2 会在这里追加：「重建该文件」+ 顶部加批量勾选工具栏

**轮询逻辑**（仿 `KnowledgeBase.vue` 行 720-777）：

```javascript
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    const pendingIds = cardList
      .filter(c => ['pending','parsing','embedding','indexing','deleting'].includes(c.status))
      .map(c => c.doc_id);
    if (pendingIds.length === 0) {
      clearInterval(pollTimer); pollTimer = null;
      return;
    }
    const res = await fetch(`/api/kb/${kbId}/documents/batch-status`, {
      method: 'POST', body: JSON.stringify({doc_ids: pendingIds})
    });
    const updates = (await res.json()).data;
    for (const u of updates) {
      const idx = cardList.findIndex(c => c.doc_id === u.doc_id);
      if (idx >= 0) Object.assign(cardList[idx], u);
    }
    renderList();
  }, 1500);
}
```

进入 KB 详情页 → 拉一次完整列表 → 如果有非终态项就 `startPolling()`。

### 4.2 文档详情面板（doc-content 简化版）

点击列表中 `completed` 状态的文档 → 右侧抽屉 / 模态打开详情面板：

- 头部：文件名 + chunk 总数 + 完成时间
- Tab 切换（**preview 标签 disabled 并标 "Coming soon"**，见 §六）：
  - **chunks** 视图（**默认**）：
    - 分块卡片列表，每块显示「分块 N · 字符数」+ 内容（markdown 渲染，复用现有 `marked + DOMPurify`）
    - 块之间有 80px overlap 高亮（如果元数据带 overlap 区间，仿 WeKnora `getChunkClass`）
  - **merged** 视图：把所有 chunks 按 `chunk_idx` 顺序拼起来（**不**像 WeKnora 那样按 start_at/end_at 去 overlap，我们的 chunker 没产出这俩字段；后续可补）

**失败状态点击行为**：弹一个小 popup 显示完整 `error_message` + 「重试」按钮，不打开详情面板。

### 4.3 不引入新依赖

- 沿用 `marked` + `DOMPurify` 渲染 markdown
- 不引入 highlight.js / mermaid（doc-content.vue 的高级功能）—— 留到后续
- 旋转 loading 用纯 CSS `@keyframes spin`

---

## 五、测试

### 5.1 后端单测

- `tests/test_phase6_1_doc_status.py`：
  - `update_document_status` 状态机：合法迁移 OK，非法迁移（completed → parsing）拒绝
  - `batch_get_documents` 只取指定 ids
  - 启动恢复：mock 一个 10 分钟前的 parsing 行，调 `recover_stale_documents` 后变 failed
- `tests/test_phase6_1_ingest_per_doc.py`：
  - `_run_ingest_job` mock 解析失败 → 该文档 failed，其它继续
  - 单文档 retry API 触发新 job

### 5.2 手工验收（追加到 MANUAL_TESTING.md "F 段"）

```
F. Phase 6.1 文档级状态（约 15 分钟）
  F.1 上传 3 个 docx 触发 ingest → 列表每行实时展示 parsing/embedding/indexing/completed
  F.2 上传一个故意损坏的 docx → 该行 failed + 悬停看到 error_message
  F.3 点击 failed 行的「重试」→ 重新进入 parsing
  F.4 点击 completed 行 → 详情面板打开，chunks tab 看到分块；切到 merged tab 看合并文本
  F.5 重启 Flask 中途（模拟崩溃）→ 启动后看到原本 parsing 的文档变成 failed「进程异常中断」
```

---

## 六、Preview 视图（**推后**，留作 backlog）

> **本期不做**，但承诺记录在此，下次迭代时直接接入。

**功能描述**：在详情面板加第三个 tab「预览」，直接显示原文件：
- PDF → `<iframe>` 加载 `/api/kb/<kb_id>/documents/<doc_id>/file`
- DOCX → 转 PDF 后预览（或用 [docx-preview](https://github.com/VolodymyrBaydalka/docxjs) 浏览器侧渲染）
- 图片 → `<img>` 标签

**所需工作**：
1. 新增 `GET /api/kb/<kb_id>/documents/<doc_id>/file`，按 `file_type` 决定 Content-Type，注意防路径穿越
2. 详情面板 tab 加 `preview`，根据 file_type 选渲染方式
3. 安全：仅 admin 鉴权可访问；防 SSRF / 路径穿越
4. （可选）DOCX 服务器端转 PDF（依赖 LibreOffice headless / docx2pdf），或纯前端 docx-preview
5. 工作量预估：1-2 人日

**记录位置**：本文件 §六 + `docs/BACKLOG.md`（如有，没有就维持这里）。

---

## 七、工作量粗估

| 模块 | 粗估 |
|---|---|
| DB 迁移 + repo 新增方法 | 0.5 人日 |
| `_run_ingest_job` 逐文档状态更新 + 启动恢复 | 0.5 人日 |
| API（batch-status / retry / chunks） | 0.5 人日 |
| 前端文档列表卡片 + 状态徽章 + 轮询 | 1 人日 |
| 前端详情面板（chunks + merged tab） | 0.5 人日 |
| 测试 + 联调 + 手册 F 段 | 0.5 人日 |
| **合计** | **约 3.5 人日** |

---

## 八、相关文件清单

**新建**
- `migrations/postgres/00X_phase6_1_doc_status.sql`
- `custom_app/services/doc_status_recovery.py`
- `tests/test_phase6_1_doc_status.py`
- `tests/test_phase6_1_ingest_per_doc.py`

**修改**
- `custom_app/repositories/kb_repository.py`（3 个新方法）
- `custom_app/db.py`（SQLite ADD COLUMN 兼容）
- `custom_app/repositories/postgres_provider.py`（CREATE TABLE 加新列 + 迁移注释）
- `custom_app/api/kb.py`（`_run_ingest_job` 逐文档 + 3 个新路由）
- `custom_app/app.py`（启动调用 `recover_stale_documents`）
- `custom_app/frontend/admin.html` / `admin.js` / `style.css`（文档列表 + 详情面板 + 轮询）
- `docs/MANUAL_TESTING.md`（追加 F 段）

---

## 九、验收标准

1. 重建索引时，每个文档实时显示 parsing → embedding → indexing → completed 流转
2. 单文档失败不影响其它文档；失败行可看错误信息、可重试
3. 完成的文档点击可看分块（chunks）和合并视图（merged）
4. Flask 异常重启后，原 processing 文档自动标 failed（不会永久卡住）
5. `pytest tests/test_phase6_1_*.py` 全通过
6. 旧 `status='done'` 数据兼容显示为 `completed`（repo 层做映射）

---

*Phase 6.1 与 Phase 7 可并行，建议先 6.1（工作量小，6.0 顺势补；不动对话链路风险低）。preview 视图记录在 §六，下个迭代再开。*

---

## 十二、会话续接上下文（2026-05-15 压缩点）

> 之前的会话已被压缩；新会话从这里开始即可。下面是接着做 Phase 6.1 需要知道的全部状态。

### 12.1 本会话已完成的代码改动（**未提交 git**）

均已写入工作区，等用户手工验证后再 commit：

| 文件 | 改动 | 目的 |
|---|---|---|
| `custom_app/services/rag_runner.py` 第 1410-1414 行 | rerank 后再做一次 `_merge_preferred_hit_ids(keyword_hit_ids, hit_ids)` | 解决 PH Box / Error UDC 检索串答 |
| `custom_app/services/llm_adapter.py` | `GeminiLLMAdapter` 加 `(connect=10, read=90)` 双超时 + 1 次重试 + 新异常 `GeminiServiceUnavailable` + 请求体大小日志 | 网络抖动时 10-20s 内反馈，而不是等满 5 分钟 |
| `custom_app/services/agent_runner.py` | 1) `init` 加 `source_builder` 参数 + `_id_to_row_idx` 映射；2) `chat_stream` 在 ReAct 循环收集 `cited_chunk_ids`；3) 最终答案前 yield 带图 `sources` 事件；4) 捕获 `GeminiServiceUnavailable` 转成用户可读文案；5) `_ensure_attrs` 加默认值 | 智能推理模式也能挂图（之前只有快速回答有） |
| `custom_app/api/chat.py` | 1) `_get_agent_runner` 把 `rag._build_sources` 作为 `source_builder` 注入；2) 新增 `invalidate_runner_cache(kb_id)` 函数 | 重建索引后失效 Runner 缓存 |
| `custom_app/api/kb.py` | `_run_ingest_job` 成功结束时调 `invalidate_runner_cache(kb_id)` | 同上 |
| `custom_app/app.py` | `create_app()` 顶部加 `setup_logging()` 调用 | 修复 `logs/app.log` 一直没写入的 bug |
| `.markdownlint.json`（新增） | 关闭 MD013/024/031/032/033/034/036/040/041/046/060 | 文档警告噪音清理 |

### 12.2 当前已知的运行时问题（**与 6.1 无关，但可能影响测试**）

1. **Gemini `gemini-3.1-pro-preview` 在大 body 时 write timeout**：用户机器到 Google API 短 body 能通（探针 200 OK），但 agent 真实请求（含 history + tools schema）经常 `('Connection aborted.', TimeoutError)`。已加 body size 日志，但**还没拿到一次完整数据**。**用户已声明"网络没问题"，留作 Phase 7 上 vLLM 适配器时彻底绕开。**
2. `_HISTORY_LIMIT = 6` 当前没改，做 6.1 不需要动它。
3. `.env` 当前配置：
   - `ULTRARAG_CHAT_BACKEND=gemini`
   - `ULTRARAG_GEMINI_MODEL=gemini-3.1-pro-preview`
   - `ULTRARAG_VECTOR_BACKEND=qdrant`
   - `ULTRARAG_DB_BACKEND=postgres`
   - `ULTRARAG_KG_BACKEND=neo4j`

### 12.3 与 6.1 直接相关的现状摘要

- `kb_documents` 表 schema（**Postgres 权威**，SQLite 不再扩展）：
  - 已有：`id / kb_id / tenant_id / doc_id / file_name / file_type / file_path / channel / status / error_message / created_at / updated_at / UNIQUE(kb_id, doc_id)`
  - 6.1 要加：`processed_at TIMESTAMPTZ`、`chunk_count INTEGER NOT NULL DEFAULT 0`
  - `status` 枚举要扩到：`pending / parsing / embedding / indexing / completed / failed / deleting`（旧 `done` repo 层映射成 `completed`）
- `_run_ingest_job` 当前位于 `custom_app/api/kb.py:390`，按 stage 推进，**未逐文档写状态**——6.1 要在循环里逐 doc 调 `update_document_status`
- `delete_document` 位于 `custom_app/api/kb.py:812`，**当前只删 DB 行 + raw 文件**（Qdrant / KG / chunks.jsonl 留尸）—— 这个 bug 已记录在 Phase 6.2 计划，**6.1 不动**
- `kb_jobs.result_json` 的 `kg_status` 字段（Phase 6.0 已加）保留不动

### 12.4 紧接着做的下一步

按本文档 §三 顺序，第一件事是：

1. **写 Postgres 迁移脚本** `migrations/postgres/00X_phase6_1_doc_status.sql` 加 `processed_at` + `chunk_count` 两列
2. 在 `custom_app/repositories/kb_repository.py` 加 3 个方法（`update_document_status` / `batch_get_documents` / `list_documents_with_status`）
3. 改 `_run_ingest_job` 主循环：每文档进入 parse/embed/index 时调 `update_document_status`，异常时写 `failed` + `error_message`，不抛
4. 新建 `custom_app/services/doc_status_recovery.py`（启动时把卡死的 parsing/embedding/indexing > 10 分钟标 failed），`app.py` 启动调用
5. API 3 路由：`POST /batch-status` / `POST /retry` / `GET /chunks`（如已存在跳过）
6. 前端 admin.js + style.css 加状态徽章、轮询、详情面板（chunks + merged tab，preview 推 Phase 6.2 之后）

### 12.5 Phase 6.2 / Phase 7 的状态

- **Phase 6.2**（单文件重建 + 删除即时清理 + FAISS 弃用）：计划文档 `docs/Phase6/PHASE_6_2_PLAN.md` 已写完，待 6.1 完成后开
- **Phase 7**（对话模型可配置 + Admin 模型管理）：计划文档 `docs/Phase7/PHASE_7_PLAN.md` 已写完，独立可并行

### 12.6 测试基线

- conda env `ultrarag`（**不**用 `.venv` / `uv`）
- 运行测试：`& "C:\Users\Peter\miniconda3\envs\ultrarag\python.exe" -m pytest tests/ -q --ignore=tests/test_chat_stream_profile.py`
- 已知遗留 fails（与 6.1 无关，参见 `docs/MANUAL_TESTING.md` §A.2）：
  - `tests/test_phase2_kb_api.py::TestChatRunnerThreadSafety` / `TestChatStreamSse`
  - `tests/test_rag_runner_agent_mode.py::test_*`（mock 风格不兼容 VectorStore 抽象）
  - `tests/test_sprint1_agent_sse_events.py::TestQuickModeNoReasoningEvents`（无 GOOGLE_API_KEY 环境）
