# Phase 3: 知识图谱与 Agent ReAct 增强

> 更新日期: 2026-05-06

## 改动概览

本阶段在 UltraRAG `custom_app` 中新增了两个主要能力：

1. **知识图谱** — 基于 SQLite 零依赖方案，实现实体/关系抽取和 1-hop 邻居查询
2. **Agent ReAct 增强** — 参考 WeKnora 修复 Agent 耗尽轮次后无答案、前端轮次不更新等问题

---

## 一、知识图谱 (Knowledge Graph)

### 背景

UltraRAG 原有检索只有 FAISS 向量搜索、关键词搜索、CrossEncoder 重排。
新增知识图谱后，Agent 可以**发现跨文档的实体关联**（如"中间层服务依赖 Oracle 数据库"），
这是纯向量搜索无法做到的。

### 技术方案

| 项目 | 选择 |
|------|------|
| 图谱存储 | SQLite（原型阶段，`kg_entities` / `kg_relations` 表） |
| 实体抽取 | Gemini API（gemini-2.0-flash），JSON 输出 |
| 11 种实体类型 | Person, Organization, Location, Product, Event, Date, Work, Concept, Resource, Category, Operation |
| 关系强度 | 5-10 分级（10=直接创建/隶属，5=松散关联） |
| 查询方式 | SQL JOIN 模拟 Neo4j 的 `MATCH (n)-[r]-(m)` |

### 涉及文件

| 文件 | 作用 | 状态 |
|------|------|------|
| `custom_app/db.py` | 新增 `kg_entities` / `kg_relations` 表及索引 | 修改 |
| `custom_app/services/kg_extractor.py` | 实体/关系抽取，调用 Gemini API，写入 SQLite | 新增 |
| `custom_app/services/kg_search.py` | 1-hop 邻居查询（outgoing + incoming 双向） | 新增 |
| `custom_app/services/tools/query_knowledge_graph.py` | Agent 工具，供 LLM 调用图谱查询 | 新增 |
| `custom_app/services/agent_runner.py` | 注册 `QueryKnowledgeGraphTool` + 工具提示 | 修改 |
| `custom_app/services/agent_config_store.py` | 白名单加入 `query_knowledge_graph` | 修改 |
| `custom_app/api/kb.py` | 工具标签 `_TOOL_LABELS` 新增条目 | 修改 |
| `prompt/kg_extract_entities.jinja` | 实体抽取 Prompt 模板 | 新增 |
| `prompt/kg_extract_relations.jinja` | 关系抽取 Prompt 模板 | 新增 |
| `prompt/agv_agent_system.jinja` | 检索策略增加第 4 步"知识图谱查询" | 修改 |

### 知识图谱构建流程

```
文档上传 → ingest → 分块(chunk)
                    │
                    ▼
          kg_extractor.extract_kb() (后台任务)
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
   逐 chunk 调用 Gemini LLM 提取实体/关系
         │
         ▼
   写入 SQLite: kg_entities / kg_relations
```

### 图谱查询流程

```
用户提问
  │
  ▼
search_graph(kb_id, entity_names)
  │
  ├─ self 段:   查找种子实体
  ├─ outgoing:  种子→邻居 (MATCH n-[r]->m WHERE n IN seeds)
  └─ incoming:  邻居→种子 (MATCH m-[r]->n WHERE m IN seeds)
  │
  ▼
返回: 实体列表 + 关系列表 + 邻居实体 + chunk_ids
```

### 已知 Bug 修复记录

**2026-05-06 kg_search.py SQL 列序 Bug**

- **问题 1**: incoming 段 SQL 第 83 行过滤条件写错，`WHERE t.kb_id=? AND e.entity_name IN(...)` 应为 `t.entity_name IN(...)`，导致 incoming 方向永远无结果
- **问题 2**: outgoing 段 SELECT 列把种子实体 `e` 放主列位置，但 Python 处理逻辑期望主列是邻居。与 incoming 段修复后的列序不一致，导致邻居实体不被添加
- **修复**: 统一三段语义——self 段主列=种子，outgoing/incoming 段主列=邻居
- **测试**: `tests/test_hotfix_kg_search_incoming.py`（6 个测试用例）

---

## 二、Agent ReAct 增强

### 问题背景

Agent 达到 `max_iterations=6` 上限后：
1. 没有调用 `final_answer` 工具
2. 后端输出一句占位文字"已达到最大推理轮次"，**没有利用已检索的内容**
3. 前端只显示"第 1 轮"，因为后续 LLM 没有返回 thought 文本

### 参考方案

对比 WeKnora（Tencent 开源 RAG 框架）的做法：

| 行为 | WeKnora | UltraRAG（修复前） | UltraRAG（修复后） |
|------|---------|-------------------|-------------------|
| `max_iterations` 默认值 | 20 | 6 | **12** |
| 耗尽后 | 调用 LLM 强制合成答案 | 输出占位文字 | **调用 LLM 强制合成答案** |
| 空响应重试 | 最多 2 次 | 无 | **最多 2 次** |

### 涉及文件

| 文件 | 作用 | 状态 |
|------|------|------|
| `custom_app/services/agent_runner.py` | 增加 `_synthesize_final_answer()`、`_retry_empty_response()`、修改主循环耗尽逻辑 | 修改 |
| `custom_app/frontend/main.js` | `toolCall()` 增加新轮次触发逻辑 | 修改 |

### 代码改动要点

**1. `agent_runner.py` — `_synthesize_final_answer()`**

耗尽轮次时，不再输出占位文字，而是：
```python
# 提取所有 "[工具结果 xxx]" 消息
# 构建 synthesis_messages = 原始问题 + 所有工具结果 + 合成 prompt
# 不带工具再次请求 LLM（纯文本输出）
```

**2. `agent_runner.py` — `_retry_empty_response()`**

LLM 返回空文本且无工具调用时，追加提示重试最多 2 次：
```
"Please provide your answer by calling the final_answer tool."
```

**3. `main.js` — `toolCall()` 新轮次触发**

```javascript
// 如果上一轮已有 tool_result，tool_call 应开启新轮次
if (currentRound && hasToolResult) {
  currentRound = null
}
```

---

## 三、部署与运维

### 图谱抽取

目前图谱抽取是**手动触发**的，调用方式：

```python
from custom_app.services.kg_extractor import extract_kb
result = extract_kb('ifs_docs', 'data/kb/ifs_docs/corpora/chunks.jsonl')
# 返回: {"entity_count": 53, "relation_count": 47, "chunk_count": 6, "errors": 0}
```

**后续规划**：
- 在 ingest job 流程中增加第四阶段 `graph_extract`，文档入库后自动抽取
- 新增管理 API：手动触发抽取、查看统计、清除图谱

### 环境变量

图谱抽取使用 Gemini API，复用现有环境变量：

| 变量 | 说明 |
|------|------|
| `GOOGLE_API_KEY` | Gemini API 密钥（嵌入和生成共用） |
| `ULTRARAG_GEMINI_API_KEY` | 备选 API 密钥 |
| `ULTRARAG_GEMINI_MODEL` | 默认 `gemini-2.0-flash` |

### 数据库

SQLite `db/app.sqlite`，无需安装额外数据库：

```sql
-- 实体表
CREATE TABLE kg_entities (id, kb_id, entity_name, entity_type, description, chunk_ids, created_at)
-- 关系表
CREATE TABLE kg_relations (id, kb_id, source_id, target_id, relation_type, description, strength, created_at)
```

### 迁移到 Neo4j（后续）

如需迁移到 Neo4j，只需实现 `kg_extractor.py` 和 `kg_search.py` 的存储层适配：
- `kg_extractor.py` 的 `_upsert_entity()` / `_add_relation_if_not_exists()` → Neo4j driver
- `kg_search.py` 的 SQL JOIN → Cypher `MATCH`
- 业务接口不变
