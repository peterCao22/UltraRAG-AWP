# Phase 6.2 —— 单文件重建索引 + 删除即时清理

> **状态**：计划已确认（2026-05-15），待开工
> **前置**：[Phase 6.1](./PHASE_6_1_PLAN.md)（文档级状态徽章 + 详情面板）—— 复用同一文件列表 UI
> **后置**：[Phase 7](../Phase7/PHASE_7_PLAN.md)（对话模型可配置，独立）

---

## 一、问题

### 1.1 现状两个痛点

1. **全量重建太慢**：[kb.py:_run_ingest_job](../../custom_app/api/kb.py) 是"先清空 → 全量"。`agv_demo` 20 个 docx → 56 chunks → 56 次 Gemini embedding + 56 次 KG 抽取，单次重建 30+ 分钟。仅修改/新增 1 个文件也要走完整流程。
2. **单文件删除有"留尸"**：[kb.py:812 `delete_document`](../../custom_app/api/kb.py#L812) 当前只做：
   - 删 `kb_documents` 行
   - 删 `raw/` 下源文件
   - **不删 chunks.jsonl 对应行**
   - **不删 Qdrant 向量**（导致已删文件仍被语义检索召回）
   - **不删 Neo4j KG 实体/关系**（导致 KG 查询仍引用已删文档）

   对照[kb.py:584 整 KB hard_delete](../../custom_app/api/kb.py#L584)，整 KB 删除时**已经做了** Qdrant 和 Neo4j 清理。单文件路径漏掉了。

### 1.2 目标

| 项 | 验收 |
|---|---|
| 删除单文件 → Qdrant / Neo4j / chunks.jsonl / kb_documents 同步清理 | 删除后再问相关问题，无残留召回 |
| 新增/修改单文件 → 仅对该 doc 跑 parse → embed → Qdrant upsert → KG 抽取 | 仅 1 个文件变动时不再重跑全库 |
| 多文件批量勾选「重建所选」 | 批量比全量快、比逐个调快 |
| 整体「全量重建」按钮保留 | 一致性兜底 |
| 项目已弃用 FAISS（用户确认，2026-05-15） | 不再为 FAISS 单文件重建做兼容 |

---

## 二、设计决策（已确认）

| 议题 | 决策 |
|---|---|
| **FAISS 后端** | **彻底弃用**，本期清理调用点；`ULTRARAG_VECTOR_BACKEND=qdrant` 成为唯一支持 |
| **删除单文件的清理范围** | Qdrant + Neo4j + chunks.jsonl + kb_documents 行 + raw 文件 |
| **单文件重建的处理粒度** | 接受 `doc_id` 参数 → 端到端 parse / embed / qdrant / kg 都按该 doc 范围跑 |
| **KG 实体共享问题** | KG 实体可能在多文档间共享（`chunk_ids` 是 list）。删除某 doc 时**从 `chunk_ids` 移除该 doc 的 chunk_ids**，剩余空时再删实体本身 |
| **chunks.jsonl 编辑** | 整文件读 → 过滤掉该 doc_id 的行 → 追加新行 → 原子覆写（写 `.tmp` 后 rename）。20 个文件量级文件 < 1MB，无性能问题 |
| **kg_extractor scope** | `extract_kb(kb_id, chunks_path, doc_ids=None)` 默认全量；传 doc_ids 时只处理子集（chunks 仍按 chunks.jsonl 读，但只跑指定 doc 的 chunk） |

---

## 三、后端实现

### 3.1 新增接口

```text
POST   /api/kb/<kb_id>/documents/<doc_id>/reindex
DELETE /api/kb/<kb_id>/documents/<doc_id>             # 替代现有 ?doc_id=X 风格，路径参数更 RESTful
POST   /api/kb/<kb_id>/documents/batch-reindex        # body: {doc_ids: [...]} 批量
```

> 现有 `DELETE /api/kb/<kb_id>/documents?doc_id=X` 保留兼容（标 deprecated），新前端用路径参数版。

### 3.2 KgStore 接口扩展

`custom_app/services/kgstore/base.py`：

```python
def delete_by_doc(self, kb_id: str, doc_id: str) -> tuple[int, int]:
    """删除某文档相关 KG 数据。
    实现要点：
      - 关系：直接 DELETE r WHERE r.doc_id = $doc_id
      - 实体：先 SET e.chunk_ids = removed(...); 再 MATCH e WHERE size(parse(chunk_ids))=0 DELETE
    返回 (relations_deleted, entities_deleted)。
    """
```

- `neo4j_store.py` 实现：Cypher 语句
- `sqlite_store.py` 实现：JSON 字段过滤 + DELETE
- 测试：`tests/test_kgstore_delete_by_doc.py`

### 3.3 关系存储要带 `doc_id`

当前 KG `relations` 没有按 doc 索引的字段。`kg_extractor` 写入时需要：
- `Entity.chunk_ids`：已有（多文档共享）
- `Relation`：**需新增 `doc_id` 列/属性**

> 历史数据兼容：旧关系没有 `doc_id` → `delete_by_doc` 找不到就跳过；用户做一次全量重建即可补齐。

### 3.4 `_run_ingest_job` 重构

```python
def _run_ingest_job(
    kb: dict, kb_id: str, job_id: str,
    *,
    force_reindex: bool = False,
    target_doc_ids: list[str] | None = None,  # 新增；None=全量
) -> dict:
```

各 stage 改造：
- `_parse_stage`：若 `target_doc_ids` 不为空，只解析这些文档 → 把对应行写回 chunks.jsonl（原子替换）
- `_embed_stage`：只为新追加的 chunks 跑 embedding，与已有 embedding 合并
- `_qdrant_stage`：先 `vector_store.delete(chunk_ids_of_target_doc)` 再 upsert 新 chunks
- `_kg_stage`：先 `kg_store.delete_by_doc(kb_id, doc_id)` 再 `extract_kb(kb_id, chunks_path, doc_ids=target_doc_ids)`

### 3.5 `delete_document` 改造

```python
@kb_bp.route("/api/kb/<string:kb_id>/documents/<string:doc_id>", methods=["DELETE"])
def delete_document(kb_id: str, doc_id: str):
    # 1. 标 deleting（Phase 6.1 状态）
    # 2. Qdrant: vector_store.delete(chunk_ids)
    # 3. KG: kg_store.delete_by_doc(kb_id, doc_id)
    # 4. chunks.jsonl: 原子覆写（过滤掉该 doc）
    # 5. kb_documents 行: doc_repo.delete
    # 6. raw 文件: unlink
    # 任意一步失败：回滚 status 到原状态 + 返回 error；不留半成品
```

### 3.6 chunks.jsonl 原子覆写工具

```python
# custom_app/utils/chunks_io.py
def remove_doc_from_chunks(chunks_path: Path, doc_id: str) -> int:
    """过滤掉指定 doc 的行，原子覆写。返回删除的 chunk 数。"""
    tmp = chunks_path.with_suffix(".jsonl.tmp")
    removed = 0
    with chunks_path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
        for line in src:
            row = json.loads(line)
            if row.get("doc_id") == doc_id:
                removed += 1
                continue
            dst.write(line)
    tmp.replace(chunks_path)
    return removed

def append_chunks(chunks_path: Path, new_rows: list[dict]) -> None:
    """追加写。"""
```

### 3.7 FAISS 清理

弃用 FAISS：
- 删除 `custom_app/services/vectorstore/faiss_store.py`
- 删除 `_index_stage` 中 FAISS 写入逻辑
- `_get_runner` 不再 fallback FAISS；`ULTRARAG_VECTOR_BACKEND=faiss` 时启动报错并给出迁移提示
- 测试中 mock 的 FAISS 路径全删

> 这一项的工作量被 6.2 吃下，但**也可以拆**：先做单文件重建，FAISS 清理留到下一票。**建议一起做**，避免代码同时存在两条向量后端路径带来的认知负担。

---

## 四、前端实现（Phase 6.1 卡片基础上扩展）

### 4.1 单文件操作菜单

在 6.1 设计的文档卡片右侧 `more` 菜单加 3 个动作：

```
📄 PH Box Presence UDC SOP.docx                    [completed · 3 chunks]
                                                   [...]  ← dropdown
                                                     ├ 重建该文件
                                                     ├ 查看分块
                                                     └ 删除文件
```

- **重建该文件** → `POST /documents/<doc_id>/reindex` → 该行 status 立刻变 `parsing`，启动 6.1 的轮询
- **查看分块** → 打开 6.1 的详情面板
- **删除文件** → 二次确认弹窗，调 `DELETE`，行变 `deleting` → 完成后从列表移除

### 4.2 批量操作

列表顶部加复选框 + 工具栏：

```
[☐ 全选]  [🔄 重建所选 (3)]  [🗑 删除所选 (3)]  |  [🔄 全量重建]
```

- 「重建所选」→ `POST /documents/batch-reindex`，body `{doc_ids: [...]}`
- 「全量重建」保留，按 Phase 6.0 现行流程

### 4.3 进度展示

复用 6.1 的状态徽章 + 1.5s 轮询。单文件重建期间整个 KB 不阻塞，其它文档仍可查询（关键好处）。

---

## 五、风险与缓解

| 风险 | 缓解 |
|---|---|
| **chunks.jsonl 写竞争** | 加文件锁（`fcntl` / `msvcrt`）；同 KB 同时只允许一个 ingest job（已有 `_JOB_EXECUTOR` 单 KB 排队） |
| **KG 实体的 chunk_ids 删空后未级联删** | `delete_by_doc` 实现里**显式**两步：先 update chunk_ids，再 DELETE WHERE size=0；写测试覆盖 |
| **旧 KG 数据无 `doc_id`** | 新增 Cypher 时 `OPTIONAL MATCH ... WHERE r.doc_id = $doc_id`，匹配不到的旧关系跳过；用户做一次全量重建可补齐 |
| **Qdrant 删除部分成功** | `vector_store.delete()` 失败 → 整个删除 API 返回 5xx，document 行保留 `deleting` 状态；用户可点「重试删除」 |
| **embedding.npy 增量合并复杂** | 简化方案：单文件重建时**不**更新 embedding.npy（Qdrant 是权威，npy 仅在全量重建时生成）；详情见 §六 |

---

## 六、embedding.npy 处理（简化决策）

`embedding.npy` 当前是 FAISS 索引构建的输入。FAISS 弃用后，**npy 只剩"备份/重导入"用途**。

决策：**单文件重建不更新 npy；仅全量重建时重建 npy**。理由：
- Qdrant 已经是权威存储
- 增量合并 npy 复杂度高（要重 build 索引数组、确保顺序对齐）收益低
- 用户做一次全量重建即可补齐

如未来某天要恢复 FAISS / 加新向量库，先用 Qdrant 的 scroll 导出再生成 npy 即可。

---

## 七、测试

### 7.1 单测

- `tests/test_phase6_2_delete_by_doc_kgstore.py`：sqlite + neo4j 两个实现都测
- `tests/test_phase6_2_chunks_io.py`：`remove_doc_from_chunks` 原子性、`append_chunks`
- `tests/test_phase6_2_doc_reindex_stages.py`：mock 各 stage，验证 `target_doc_ids` 参数路由
- `tests/test_phase6_2_delete_document_api.py`：全链路（mock Qdrant/KG），删除后查询无残留

### 7.2 联调（追加到 MANUAL_TESTING.md "G 段"）

```
G. Phase 6.2 单文件操作（约 15 分钟）
  G.1 上传 1 个新 docx → 点击「重建该文件」→ 状态流转 parsing→embedding→indexing→completed
  G.2 重建期间问该新文档相关问题 → 短暂查不到正常，重建完成后可查到
  G.3 重建期间问其它老文档问题 → 不受影响
  G.4 删除某文件 → status 变 deleting → 完成后行消失
  G.5 删除后问该文件内容 → 答案应是「文档中未找到」（无 Qdrant 残留）
  G.6 删除后用 KG 工具查该文件涉及实体 → 不返回该实体（无 Neo4j 残留）
  G.7 批量勾选 2 个文件「重建所选」→ 两行同时进入 parsing
  G.8 「全量重建」按钮 → 走老流程，与 6.1 验收一致
```

---

## 八、工作量粗估

| 模块 | 粗估 |
|---|---|
| KgStore.delete_by_doc（两实现 + 测试） | 1 人日 |
| KG relations 加 doc_id + kg_extractor 改造 | 0.5 人日 |
| chunks_io 工具 + 测试 | 0.5 人日 |
| `_run_ingest_job` 重构（target_doc_ids 路由） | 1 人日 |
| delete_document 完整清理 + API | 0.5 人日 |
| 单文件 reindex API + batch API | 0.5 人日 |
| FAISS 清理（删代码 + 改测试） | 0.5 人日 |
| 前端：卡片菜单 + 批量工具栏 + 轮询接入 | 1 人日 |
| 联调 + 手册 G 段 | 0.5 人日 |
| **合计** | **约 6 人日** |

---

## 九、相关文件清单

**新建**
- `custom_app/utils/chunks_io.py`
- `custom_app/api/kb_documents.py`（拆 kb.py 中 document 子路由，避免 kb.py 过大）—— 可选
- 6 个 `tests/test_phase6_2_*.py`

**修改**
- `custom_app/services/kgstore/base.py`（新增 delete_by_doc 接口）
- `custom_app/services/kgstore/neo4j_store.py`、`sqlite_store.py`（实现）
- `custom_app/services/kg_extractor.py`（接受 doc_ids 参数 + 写入 relation.doc_id）
- `custom_app/api/kb.py`（_run_ingest_job 增 target_doc_ids；delete_document 全清理；新增 reindex/batch-reindex 路由）
- `custom_app/services/vectorstore/` —— 删除 faiss_store.py、改 base.py 移除 FAISS 引用
- `custom_app/frontend/admin.html` / `admin.js` / `style.css`（菜单 + 批量栏）
- `docs/MANUAL_TESTING.md`（追加 G 段）
- `CLAUDE.md`（弃用 FAISS 说明）

---

## 十、验收标准

1. 删除单文件后，问该文件相关问题 → 无 Qdrant/KG 残留召回
2. 修改单文件 + 「重建该文件」→ < 1 分钟（vs 全量 30+ 分钟）
3. 重建单文件期间其它文档查询不阻塞
4. 全量重建仍可用且结果与逐文件等价
5. FAISS 代码完全移除，单元测试无 FAISS 引用
6. `pytest tests/test_phase6_2_*.py` 全通过

---

## 十一、Phase 衔接

- **Phase 6.1** 必须先做：6.2 的前端按钮要挂在 6.1 的卡片菜单上
- **Phase 7** 独立：不依赖 6.2，可并行

执行顺序建议：**6.1 → 6.2 → 7**

---

*单文件重建是 Admin 体验的关键改进；FAISS 弃用是技术债清理。两件事一起做有协同：删 FAISS 路径让单文件重建的实现复杂度大幅下降。*
