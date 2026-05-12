# Phase 5.1 实施记录与验收

> 完成时间：2026-05-11
> 前置：[Phase 4](../Phase4/PHASE4_PLAN.md)
> 概要：[PHASE5_OUTLINE.md](PHASE5_OUTLINE.md)

---

## 一、Phase 5.1 完整内容

### 5.1.0-5.1.4：Qdrant 向量库迁移

| 子项 | 状态 |
| --- | --- |
| 5.1.0 服务连通性 + .env 配置 | ✅ Qdrant 1.16.2 + PostgreSQL 16.13 (192.168.8.40) |
| 5.1.1 QdrantVectorStore 实现 | ✅ 22 单元测试 + 真实 Qdrant 跑通 |
| 5.1.2 VectorStore 工厂 | ✅ resolve_vector_backend / build_vector_store |
| 5.1.3 RagRunner 接入工厂 | ✅ YAML `vector_backend` + env `ULTRARAG_VECTOR_BACKEND` |
| 5.1.4 FAISS → Qdrant 迁移 | ✅ agv_demo 23 + ifs_docs 16 chunks → Qdrant；7/7 query Top-5 一致 |

### 5.1.5-5.1.8：PostgreSQL + Repository 层

| 子项 | 状态 |
| --- | --- |
| 5.1.5 Repository 层抽象 | ✅ 7 个 Repository + 41 SQLite 单元测试 |
| 5.1.6 PostgresProvider + 数据迁移 | ✅ 544 行全量迁移（含 KG entity ID 重映射） |
| 5.1.7 全套 SQL 调用点改用 Repository | ✅ 6 文件 × 146 调用 → 0 raw SQL |
| 5.1.8 双后端 E2E 验收 | ✅ 7/7 一致性检查通过 |

---

## 二、架构成果

### 2.1 双后端抽象

```
┌─────────────────────────────────────────────────────┐
│                  Application Layer                  │
│  custom_app/api/{kb,roles}.py + services/*          │
└────────────────┬──────────────────┬─────────────────┘
                 │                  │
        ┌────────▼────────┐  ┌──────▼─────────┐
        │ Repository 层    │  │ VectorStore    │
        │ (8 个 Protocol)  │  │ Protocol       │
        └────────┬────────┘  └──────┬─────────┘
                 │                  │
       ┌─────────┴─────────┐  ┌────┴────┬──────────┐
       ▼                   ▼  ▼         ▼          ▼
  SqliteProvider     PostgresProvider  FAISS    Qdrant
  (db/app.sqlite)    (192.168.8.40)    (本地)   (192.168.8.40)
```

### 2.2 切换方式

通过 `.env` 或 `servers/retriever/parameter.yaml` 单行切换：

```bash
# .env
ULTRARAG_VECTOR_BACKEND=qdrant      # faiss | qdrant
ULTRARAG_DB_BACKEND=postgres        # sqlite | postgres
```

```yaml
# servers/retriever/parameter.yaml
vector_backend: qdrant
```

### 2.3 数据流（Phase 5 后）

```
docx → docx_parser → chunks.jsonl
                      ↓
                  embedding (Gemini)
                      ↓
        ┌─────────────┴─────────────┐
        │                           │
   FaissVectorStore           QdrantVectorStore
   (本地 .index 文件)         (Qdrant 服务)
```

```
api/kb.py → KbRepository.create()
              ↓
              SqliteConnectionProvider 或 PostgresConnectionProvider
              ↓
              SQL（统一 ? placeholder，adapt_sql 翻译）
              ↓
              db/app.sqlite 或 PostgreSQL
```

---

## 三、关键文件清单

### 新增

```
custom_app/repositories/
  __init__.py
  base.py                       # ConnectionProvider Protocol + adapter helpers
  postgres_provider.py          # PostgresConnectionProvider + Postgres schema DDL
  kb_repository.py              # knowledge_bases
  job_repository.py             # kb_jobs
  document_repository.py        # kb_documents
  session_repository.py         # kb_sessions + kb_session_messages
  role_repository.py            # roles + role_kb_permissions
  agent_config_repository.py    # kb_agent_configs
  kg_repository.py              # kg_entities + kg_relations

custom_app/services/vectorstore/
  qdrant_store.py               # QdrantVectorStore + QdrantConfig

custom_app/scripts/
  probe_phase5_services.py      # Docker 服务连通性探测
  migrate_faiss_to_qdrant.py    # FAISS → Qdrant 迁移 + 一致性验证
  migrate_sqlite_to_postgres.py # SQLite → Postgres 全表迁移
  verify_phase5_dual_backend.py # 双后端 E2E 验收
  verify_queries_agv.txt        # 一致性验证查询样本

tests/
  test_qdrant_vectorstore.py    # 22 项（含 1 项真实 Qdrant 集成）
  test_repositories.py          # 41 项 SQLite Repository CRUD
  test_repositories_postgres.py # 6 项真实 Postgres 集成

docs/Phase5/
  PHASE5_OUTLINE.md
  PHASE5_PLAN.md                # 本文档
```

### 修改

```
custom_app/db.py                # init_db 不变；不再被业务层直接调用
custom_app/api/kb.py            # 46 处 SQL → Repository（0 raw SQL）
custom_app/api/roles.py         # 14 处 SQL → Repository（0 raw SQL）
custom_app/services/agent_config_store.py    # 2 处 → AgentConfigRepository
custom_app/services/session_store.py         # 11 处 → SessionRepository
custom_app/services/kg_extractor.py          # 8 处 → KgRepository
custom_app/services/kg_search.py             # 6 处 → KgRepository
custom_app/services/rag_runner.py            # FaissVectorStore 集成 + backend 工厂
custom_app/services/vectorstore/base.py      # resolve_vector_backend + build_vector_store
custom_app/services/vectorstore/__init__.py  # 新增导出
servers/retriever/parameter.yaml             # vector_backend 字段
pyproject.toml                  # [storage] extras + 新 marker
.env / .env.example             # ULTRARAG_QDRANT_* / POSTGRES_URI / *_BACKEND
```

---

## 四、迁移验证记录

### 4.1 服务可达性（5.1.0）

```
[OK] qdrant version=1.16.2 at http://192.168.8.40:6333
[OK] PostgreSQL 16.13 at 192.168.8.40:5432
```

### 4.2 Qdrant 数据迁移（5.1.4）

```
agv_demo: 23 chunks → custom_app__agv_demo collection
ifs_docs: 16 chunks → custom_app__ifs_docs collection
一致性验证 (7 queries, top-5):
  顺序完全一致: 7/7
  Top-K 集合一致: 7/7
```

### 4.3 PostgreSQL 数据迁移（5.1.6）

```
knowledge_bases       5 rows  [OK]
kb_jobs               20 rows [OK]
kb_documents          16 rows [OK]
kb_sessions           26 rows [OK]
kb_session_messages   110 rows [OK]
kb_agent_configs      2 rows  [OK]
kg_entities           188 rows [OK]
kg_relations          177 rows [OK]
共 544 行 + KG FK 重映射成功
```

### 4.4 Repository 双后端 E2E（5.1.8）

```
KB list 顺序:        SQLite == Postgres ✓
Job list:            SQLite == Postgres ✓
Document count:      SQLite == Postgres ✓
Session count:       SQLite == Postgres ✓
KG stats (188/177):  SQLite == Postgres ✓
AgentConfig:         SQLite == Postgres ✓
Qdrant ifs_docs:     16 points 在线 ✓

总：7/7 通过，0 失败
```

---

## 五、测试覆盖

| 测试集 | 数量 | 备注 |
| --- | --- | --- |
| `test_qdrant_vectorstore.py` | 22 | 含真实 Qdrant 集成 |
| `test_repositories.py` | 41 | SQLite Repository CRUD |
| `test_repositories_postgres.py` | 6 | 真实 Postgres |
| `test_faiss_vectorstore.py` | 17 | 含工厂函数测试 |
| 其他 Phase 5 相关测试 | 全套 398 passed | |

**已知 5 项 fails 在 Phase 5 范围外**：
- 2 项 `test_phase2_kb_api.py`：FakeRagRunner.chat() 不接受 agent_mode（Phase 3 遗留）
- 3 项 `test_rag_runner_agent_mode.py`：mock 风格不兼容新 VectorStore 抽象（Phase 4.0 遗留）

---

## 六、后续工作

### Phase 5.2（已规划，按需启动）

- Neo4j 替代 SQLite kg_entities/kg_relations 两表
- 触发条件：Agent 工具需要多跳推理（"故障 → 关联部件 → 维修 SOP" 链式查找）

### Phase 6（规划中）

- BM25 混合检索（Qdrant + Tantivy / Elasticsearch）
- Multi-query rewrite

---

## 七、生产部署 checklist

切换到 Phase 5 后端：

1. **数据迁移**（仅一次）：
   ```bash
   # SQLite → Postgres
   .venv/Scripts/python.exe -m custom_app.scripts.migrate_sqlite_to_postgres --truncate

   # FAISS → Qdrant（按 KB 迁移）
   .venv/Scripts/python.exe -m custom_app.scripts.migrate_faiss_to_qdrant --kb agv_demo --recreate
   .venv/Scripts/python.exe -m custom_app.scripts.migrate_faiss_to_qdrant --kb ifs_docs --recreate
   ```

2. **切换配置**：
   ```bash
   # .env
   ULTRARAG_VECTOR_BACKEND=qdrant
   ULTRARAG_DB_BACKEND=postgres
   ```

3. **验证**：
   ```bash
   .venv/Scripts/python.exe -m custom_app.scripts.verify_phase5_dual_backend
   # 应输出：通过：7 失败：0
   ```

4. **回滚**（如需）：把上面 2 行环境变量改回 `faiss` / `sqlite` 重启 Flask 即可。

---

> **Phase 5.1 完成**：存储栈双后端切换就绪；可继续 Phase 5.2 或 Phase 6。
