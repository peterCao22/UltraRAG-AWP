# Phase 5 概要规划 — 存储栈迁移

> 制定时间：2026-05-11
> 状态：**概要** — 详细任务拆分待 Phase 4 完成后再做
> 前置依赖：[Phase 4 完成](../Phase4/PHASE4_PLAN.md)
> 预期工时：1-3 周（视范围而定）

---

## 一、Phase 5 目标

把 custom_app 的存储栈从**单机文件型**升级到**网络服务型**，提升性能、稳定性、扩展性。

| 当前 | 迁移目标 | 优先级 |
|------|---------|--------|
| FAISS（内存加载 `.index` 文件） | **Qdrant**（局域网已部署） | **P0 必做** |
| SQLite（`db/app.sqlite`） | **PostgreSQL**（局域网已部署） | **P0 必做** |
| SQLite KG 两表（`kg_entities` + `kg_relations`） | Neo4j | **P1 视情况** |

---

## 二、为什么要做这次迁移

### 2.1 FAISS 的真实痛点

| 痛点 | 表现 | Qdrant 如何解决 |
|------|------|----------------|
| 内存占用 | 每个 KB 启动时全量加载 `.index` 到内存，KB 多了内存爆 | 持久化 + 按需加载 |
| 缺元数据过滤 | 按 `source_type` / `doc` 过滤要在内存中 post-filter | payload 原生过滤（`source_type=sop_docx`） |
| 无并发写入保护 | 入库时其他查询会读到不一致状态 | 服务端事务 |
| 增量 upsert 笨重 | 改动一个 chunk 要重建 `IndexIDMap2` | 原生 upsert API |
| 部署耦合 | FAISS 和 Flask 同进程，扩容只能整体扩 | HTTP API，可独立扩容 |

### 2.2 SQLite 的瓶颈（中等）

- 当前 `JobExecutor` 是 FIFO 单线程，**还没碰到 SQLite 写锁瓶颈**
- 但未来要多机部署 / 多副本时，SQLite 走不通
- `payload_json` 是 TEXT，查询要 `json_extract`，Postgres 有 JSONB 原生索引

**判断**：既然 Qdrant 都要换了，**顺手换 Postgres 边际成本低**。

### 2.3 Neo4j 的价值（待评估）

- **多跳遍历**："找 A 实体的 N 跳邻居" —— SQLite 要递归 CTE，慢且复杂
- **路径查询**："A 到 B 之间的关系链" —— SQLite 几乎做不了
- Cypher 比 SQL JOIN 可读性高很多

**触发条件**：
- 如果 Agent 的 `QueryKnowledgeGraphTool` 未来要做**多跳推理**（"故障 → 关联部件 → 维修 SOP"链式查找），Neo4j 必要
- 如果只是单跳过滤（"找含实体 X 的 chunk"），SQLite 够用

**决策**：Phase 5.1 不动 KG；用 Phase 5.1 上线后 1-2 月观察 Agent 工具实际使用模式，再决定是否进 Phase 5.2。

---

## 三、阶段拆分（高层）

### Phase 5.1 — Qdrant + PostgreSQL（P0）

| 子任务 | 工时估算 | 复杂度 |
|--------|---------|--------|
| 5.1.1 实现 `QdrantVectorStore`（实现 Phase 4 的 Protocol） | 2-3 天 | MEDIUM |
| 5.1.2 数据迁移脚本：FAISS → Qdrant（已有 chunks.jsonl 重新 upsert） | 1-2 天 | LOW |
| 5.1.3 双写灰度：同时写 FAISS 和 Qdrant，对比检索结果 | 2-3 天 | MEDIUM |
| 5.1.4 SQLite → PostgreSQL 迁移（schema + 数据） | 3-4 天 | **HIGH** |
| 5.1.5 db.py 抽象 Repository 层 | 2-3 天 | MEDIUM |
| 5.1.6 配置切换 + 健康检查 + 文档 | 1-2 天 | LOW |
| **总计** | **11-17 天（约 2-3 周）** | **MEDIUM-HIGH** |

### Phase 5.2 — Neo4j（P1，按需）

| 子任务 | 工时估算 | 复杂度 |
|--------|---------|--------|
| 5.2.1 设计 Neo4j schema（节点/关系类型） | 1 天 | LOW |
| 5.2.2 KG 抽取代码改造（写 Neo4j 替代 SQLite 两表） | 2-3 天 | MEDIUM |
| 5.2.3 `QueryKnowledgeGraphTool` 改造（Cypher 替代 SQL） | 1-2 天 | LOW |
| 5.2.4 数据迁移脚本 + 验证 | 1-2 天 | LOW |
| **总计** | **5-8 天（约 1-1.5 周）** | **MEDIUM** |

---

## 四、Phase 4 已经为 Phase 5 做的准备

| 准备项 | 位置 | Phase 5 如何用 |
|--------|------|---------------|
| `VectorStore` Protocol | `custom_app/services/vectorstore/base.py` | 直接加 `QdrantVectorStore` 实现 |
| `FaissVectorStore` | `custom_app/services/vectorstore/faiss_store.py` | 作为双写期间的对照参考 |
| `chunks.jsonl` 加 `vector_id` 字段 | `custom_app/services/parsers/schema.py` | 迁移时填 Qdrant point id |
| RagRunner 依赖注入 VectorStore | `custom_app/services/rag_runner.py` | 切换实现零侵入 |
| `kb_documents.file_type` 记录文档类型 | `custom_app/db.py` | 迁移到 Postgres 时直接 1:1 |

---

## 五、关键风险（先识别，详细缓解 plan 阶段做）

| 等级 | 风险 | 备注 |
|------|------|------|
| 🔴 HIGH | SQLite → Postgres 数据迁移过程中数据不一致 | 双写期 + checksum 验证 |
| 🔴 HIGH | Qdrant 网络故障导致全系统不可用 | 健康检查 + 降级到 FAISS（短期保留） |
| 🟡 MED | Qdrant payload 索引设计（哪些字段建索引） | 看实际查询模式决定 |
| 🟡 MED | 局域网部署 Qdrant/Postgres 的认证/权限管理 | 一开始可以内网信任，后续加 |
| 🟡 MED | Repository 层抽象错误会让所有 db 调用都要改 | 先 spike 一个示例，确认设计可行再大范围迁 |
| 🟢 LOW | 多机部署的会话亲和性 | 暂不上多机，先单机 |

---

## 六、Phase 5 之前需要确认的事

实施前要回答的开放问题（**Phase 5 plan 阶段再细聊**）：

1. **数据迁移策略**：双写灰度多久？硬切？什么时候彻底关掉 FAISS / SQLite？
2. **Qdrant payload 索引设计**：哪些字段需要索引（`kb_id` / `source_type` / `doc` ...）
3. **Postgres 部署细节**：版本、扩展（pgvector？JSONB GIN 索引？）、连接池
4. **错误处理与降级**：网络分区时是 fail-fast 还是 fallback
5. **是否引入 ORM**：SQLAlchemy 还是继续手写 SQL（Repository 层下）

---

## 七、与 Phase 4 / Phase 6 的关系

```
Phase 3 (现有)
    ↓
Phase 4 (解析层 + 检索准确率 + VectorStore 抽象)
    ↓
Phase 5.1 (Qdrant + Postgres)    ← 你在这里
    ↓
Phase 5.2 (Neo4j, 视需求)
    ↓
Phase 6 (BM25 混合检索 + Query 增强)
```

**Phase 5 完成后**，BM25 混合检索（Phase 6）的实现会容易很多 —— Qdrant 配合外置 BM25 是成熟方案（如 Qdrant + Tantivy / Elasticsearch）。

---

> **本文档是 Phase 5 的占位与方向锚点**。详细任务拆分等 Phase 4 完成后用 `/plan` 重新生成。
