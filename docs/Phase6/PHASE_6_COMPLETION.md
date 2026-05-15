# Phase 6.0 完成情况说明（Ingest 自动 KG）

本文档对应计划：`phase_6.0_ingest_kg_8e44ead3.plan.md`（Cursor Plans）。  
在计划所列目标**全部完成**的基础上，还做了若干**可观测性 / 排障**增强，便于生产环境确认 KG 是否真的写入、失败原因是什么。

---

## 1. 计划内目标（对照 plan）

| 计划项 | 状态 | 说明 |
|--------|------|------|
| `kb.py` 新增 `_should_extract_kg()` | ✅ | 按 KB 的 `enabled_tools`（`query_knowledge_graph`）决定是否跑 KG；无需 DB 迁移 |
| `kb.py` 新增 `_kg_stage()` | ✅ | 调用 `extract_kb(kb_id, chunks_path)`，写入 Neo4j / SQLite（由 `ULTRARAG_KG_BACKEND` 决定） |
| `_run_ingest_job` 在 index 之后插入 Stage 4 | ✅ | KG 失败**不**导致 ingest 整体失败，索引仍可用 |
| 新增 `tests/test_phase6_ingest_kg.py` | ✅ | 覆盖开启 / 跳过 / 失败仍 success / 结果字段等场景 |

架构与计划中的 mermaid 一致：`parse → embed → index → (可选) kg → mark_success`。

---

## 2. 实际改动清单（相对最小 plan 的扩展）

### 2.1 核心：`custom_app/api/kb.py`

- **`_should_extract_kg(kb_id)`**  
  通过 `get_enabled_tools(kb_id)` 判断是否包含 `query_knowledge_graph`。

- **`_kg_stage(kb_id, chunks_path)`**  
  封装 `extract_kb`，返回实体数、关系数、chunk 数、错误计数等统计。

- **`_run_ingest_job` 中 Stage 4**  
  - 成功时写入 `kg` stage，并在 `result_json` 中带上 **`kg_status`**（`ok` / `empty` / `errors`）、`kg_entity_count`、`kg_relation_count`、`kg_chunk_count`、`kg_error_count`、`kg_message`。  
  - 若实体与关系均为 0：标记为 **`empty`** 或 **`errors`**，并写可读 **`kg_message`**，避免「跑完但图谱静默为空」无人察觉。  
  - 异常时：`kg_failed` stage，含 **`kg_status: failed`**、`kg_error`、`kg_error_type`。

- **作业进度 API**（`get_job_progress` 等路径）  
  若 `result_json` 中含 `kg_status`，则响应中增加 **`progress["kg"]`** 结构，便于 Admin 进度页直接展示 KG 子状态。

- **排障端点 `GET /api/kb/<kb_id>/diagnostics`**  
  汇总 chunk 数量、向量侧状态、KG 统计、`enabled_tools` 是否开启 KG 抽取、最近一次 ingest job 摘要等，用于一键确认「库是否真的建好、KG 是否有数据」。

### 2.2 支撑与可观测（与 Phase 6 排障强相关）

- **`custom_app/logging_setup.py` + `custom_app/app.py` 启动时 `setup_logging()`**  
  控制台 + `logs/app.log`，KG 相关可写 `logs/kg_ingest.log`（若模块已接 logger），避免「有 logger 无文件」的盲区。

- **`custom_app/services/kg_extractor.py`**（与 ingest 链路的 LLM 调用一致）  
  Gemini REST 使用 **`x-goog-api-key` 请求头**，与官方文档 REST 示例一致，避免 URL 携带 `?key=`。

> 说明：Agent 模式里 `GeminiLLMAdapter`、`thoughtSignature`、function calling 闭环等属于**对话 / Agent 链路**，不是 Phase 6 plan 的「仅 kb + 测试」范围；若需单独成文可放在 `docs/Phase6.5-agent.md` 或 Sprint 文档中，此处不展开。

### 2.3 测试

- **`tests/test_phase6_ingest_kg.py`**：按计划对 `_run_ingest_job` 的 KG 分支做 mock 级单测（SQLite KG 后端、避免真实 Neo4j/Gemini）。

---

## 3. 配置与环境变量（运维备忘）

| 变量 / 配置 | 作用 |
|-------------|------|
| `ULTRARAG_KG_BACKEND` | `neo4j` / `sqlite` 等，决定 KG 落库位置 |
| KB `enabled_tools` 含 `query_knowledge_graph` | 为 true 时 ingest 末尾才跑 `_kg_stage` |
| `logs/kg_ingest.log`、`logs/app.log` | KG 抽取与排障日志 |

---

## 4. 如何自验「Phase 6 已生效」

1. 在 Admin 为某 KB 开启含 **`query_knowledge_graph`** 的 Agent 工具配置。  
2. 触发一次 **ingest / 重建索引**，观察 job 的 `stages_done` 是否出现 **`kg`** 或 **`kg_failed`**。  
3. 调用 **`GET /api/kb/<kb_id>/diagnostics`**，确认 `kg` 区块实体/关系计数与预期一致。  
4. 若为空，查看 **`kg_message`** 与 **`logs/kg_ingest.log`**。

---

## 5. 后续阶段（独立文档）

| 阶段 | 文档 | 说明 |
|------|------|------|
| **Phase 6.1** | [PHASE_6_1_PLAN.md](./PHASE_6_1_PLAN.md) | KB Admin **入库/重建索引进度条**与细粒度打点（与对话模型无关） |
| **Phase 7** | [../Phase7/PHASE_7_PLAN.md](../Phase7/PHASE_7_PLAN.md) | **思考/对话模型**多配置 + 前端下拉 + Runner 缓存 `(kb_id, model_id)` |

---

*文档生成日期：以仓库当前迭代为准；计划源：`phase_6.0_ingest_kg_8e44ead3.plan.md`。*
