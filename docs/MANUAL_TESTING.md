# 手工集中测试清单

> 整理时间：2026-05-12
> 覆盖范围：Phase 4 + Phase 5（4.1 ~ 5.2）所有需要用户人工验证的项目
> 自动测试见各 Phase 文档中的 L1/L2 章节，本文档**只列出真正需要人工执行**的步骤

---

## 怎么用这个文档

按顺序跑完 **A → B → C → D → E** 五大块，每块结束有 PASS / FAIL 判定。
全部通过 = Phase 4 + Phase 5 完全可上线。

每步前面的 `[ ]` 是 checkbox，验证完打勾 → `[x]`。

预计总时长：**约 60-90 分钟**（含模型首次加载 + 数据观察）。

---

## A. 环境准备（一次性，10 分钟）

### A.1 服务可达性探测

```powershell
.venv\Scripts\python.exe -m custom_app.scripts.probe_phase5_services
```

**预期**：

```
[OK]   Qdrant      (192.168.8.40:6333)
[OK]   Postgres    (192.168.8.40:5432/awprag)
[OK]   Neo4j       (192.168.8.40:7687)
```

- [ ] 三个服务都返回 `[OK]`
- [ ] **FAIL 处理**：检查 `.env` 中 `ULTRARAG_*_URI` 配置；检查局域网防火墙

### A.2 自动测试 baseline

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q --tb=line `
  --ignore=tests/test_chat_stream_profile.py
```

**预期**：约 **460 passed + 8 skipped**（视环境装的可选依赖而定）

**已知 5 项 fails（Phase 3 老遗留，不影响功能）**：
- `tests/test_phase2_kb_api.py::TestChatRunnerThreadSafety / TestChatStreamSse` —— FakeRagRunner.chat() 不接受新增的 agent_mode 参数
- `tests/test_rag_runner_agent_mode.py::test_*` —— mock 风格不兼容 Phase 4.0 引入的 VectorStore 抽象

- [ ] 总数符合预期（passed >= 440）
- [ ] 失败的测试不超过 5 项，且全在上面已知列表里

---

## B. Phase 4 解析层验收（约 20 分钟）

### B.1 SOP 库零回归（用现有 agv_demo / ifs_docs）

```powershell
.venv\Scripts\python.exe -m custom_app.scripts.verify_sop_regression --kb agv_demo
.venv\Scripts\python.exe -m custom_app.scripts.verify_sop_regression --kb ifs_docs
```

**预期**：

```
agv_demo: 7 docx → 23 chunks（与 Phase 3 chunks.jsonl 100% 一致）
ifs_docs: 4 docx → 16 chunks（与 Phase 3 chunks.jsonl 100% 一致）
schema_issues: 0
diff_vs_reference: OK
```

- [ ] `agv_demo` 输出 `[OK] Phase 4 SOP 回归通过`
- [ ] `ifs_docs` 输出 `[OK] Phase 4 SOP 回归通过`

### B.2 general KB 烟囱（markdown 路径）

```powershell
.venv\Scripts\python.exe -m custom_app.scripts.verify_general_kb_smoke
```

**预期**：3 个 md 样本 → 7 chunks，heading_path 6/7 命中。

- [ ] 输出 `[OK] Phase 4 general KB 烟囱测试通过`

### B.3 真实 MinerU / Docling（可选，需先装可选依赖）

**前置**：装可选依赖（首次会下载 GB 级模型）：

```powershell
uv sync --extras parsing
```

**MinerU 测试**：

```powershell
.venv\Scripts\python.exe -m pytest -m requires_mineru tests\test_mineru_parser.py -v
```

- [ ] 至少有一个 `data/kb/general/raw/sample.pdf` 样本可被解析
- [ ] 解析后 chunk 数 > 0，且至少有一个 chunk 有 heading_path

**Docling 测试**：

```powershell
.venv\Scripts\python.exe -m pytest -m requires_docling tests\test_docling_parser.py -v
```

- [ ] 用 `data/kb/ifs_docs/raw/*.docx` 跑通；source_type=general_docx

### B.4 heading_path A/B 验证（可选，需 Google API key）

```powershell
copy custom_app\scripts\eval_queries_example.txt custom_app\scripts\eval_queries.txt
# 编辑成你关心的 10 个真实查询

.venv\Scripts\python.exe -m custom_app.scripts.eval_heading_path `
  --kb agv_demo `
  --queries custom_app\scripts\eval_queries.txt `
  --top-k 5 `
  --max-chunks 30 `
  --json eval_result.json
```

**预期**：

- 至少一部分 query 有 `*` 或 `^`（命中提升）
- 平均 Jaccard 在 0.5-0.9（适度改动，不是全替换）

- [ ] 跑完无报错
- [ ] enhanced 至少在 1 个 query 上有命中改进（`*` 标记）

---

## C. Phase 5.1 存储栈切换验收（约 20 分钟）

> 前置：服务可达（A.1 通过）；数据已迁移到 awprag + Qdrant（参见 [PHASE5_PLAN.md](Phase5/PHASE5_PLAN.md) §七部署 checklist）

### C.1 三栈双后端一致性

```powershell
.venv\Scripts\python.exe -m custom_app.scripts.verify_phase5_dual_backend
```

**预期**：

```
=== 总结 ===
  通过：8
  失败：0
[OK] Phase 5 双后端验证通过
```

- [ ] 通过 8 失败 0
- [ ] KG stats SQL == Neo4j（188E/177R 等）

### C.2 切换到 qdrant + postgres + neo4j 后再启动 Flask

1. 编辑 `.env`：

   ```bash
   ULTRARAG_VECTOR_BACKEND=qdrant
   ULTRARAG_DB_BACKEND=postgres
   ULTRARAG_KG_BACKEND=neo4j
   ```

2. 启动 Flask：

   ```powershell
   python -m custom_app.app --port 8080
   ```

3. 浏览器打开 [http://127.0.0.1:8080/admin.html](http://127.0.0.1:8080/admin.html)
   - [ ] 知识库列表显示正确（5 个老 KB：agv_demo / ifs_docs 等）
   - [ ] 每个 KB 详情页头部显示「类型 SOP / 通用」
   - [ ] 点击 KB 进入后能看到文档列表

4. 浏览器打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)（对话页）
   - [ ] 切换 KB 到 `agv_demo`，问一个 SOP 类问题（如"更换 AGV 电池的步骤"）
   - [ ] SSE 返回正常，含 sources 引用
   - [ ] 答案与 Phase 4 / Phase 3 时代质量相当

5. 通过 sqlite 客户端确认**实际查询走的是 Postgres（awprag）**：

   ```powershell
   # 直接连 awprag 看是否有新的 kb_sessions 行
   .venv\Scripts\python.exe -c "import psycopg, os; from dotenv import load_dotenv; load_dotenv(); print(psycopg.connect(os.environ['ULTRARAG_POSTGRES_URI']).execute('SELECT COUNT(*) FROM kb_sessions').fetchone())"
   ```

   - [ ] 计数应**大于** 26（说明对话页新建会话写到了 Postgres）

### C.3 回滚到 faiss + sqlite + sqlite（验证回退路径）

1. 编辑 `.env` 改回：

   ```bash
   ULTRARAG_VECTOR_BACKEND=faiss
   ULTRARAG_DB_BACKEND=sqlite
   ULTRARAG_KG_BACKEND=sqlite
   ```

2. **重启 Flask**

3. 在对话页重复 C.2 步骤 4 同一个问题

- [ ] 答案仍正常返回（说明数据未损坏）
- [ ] 看 Flask 日志中应有 `vector_backend=faiss` 字样

### C.4 KB type 路由（创建 general KB）

启动 Flask 后在 admin 页面：

1. 点击「新建知识库」
   - [ ] 表单显示「知识库类型」下拉
   - [ ] 默认选中「SOP 知识库 (sop_docx)」
   - [ ] 可切到「通用知识库 (general)」

2. 创建一个 `gen_test`，type 选 `general`，name 任意

3. 进入 `gen_test` 详情
   - [ ] 头部显示「类型 通用（创建后不可改）」

4. 在 admin 上传一个 .md 文件
   - [ ] 上传成功，文件出现在列表里

5. 上传一个 .docx
   - [ ] 上传成功（general 路径下走 Docling）

6. 上传一个不支持的 .xyz 文件
   - [ ] 应被拒绝，错误信息列出允许的扩展名

---

## D. Phase 5.2 Neo4j KG 后端验收（约 15 分钟）

### D.1 KG 数据一致性

C.1 中第 3 部分 `Neo4j KG 与原 SQL KG 一致性` 应已通过。

### D.2 Neo4j Browser 手动检视

打开 [http://192.168.8.40:7474](http://192.168.8.40:7474) → 登录 neo4j / password

跑下面三个 Cypher 查询：

```cypher
// 1. 节点总数（应该 = 188）
MATCH (e:Entity {kb_id: 'ifs_docs'}) RETURN count(e) AS total;

// 2. 关系总数（应该 = 177）
MATCH ()-[r:RELATES_TO {kb_id: 'ifs_docs'}]->() RETURN count(r) AS total;

// 3. 可视化某个枢纽实体的 1-hop 邻居（例如 "出库类型"）
MATCH (e:Entity {kb_id: 'ifs_docs', name: '出库类型'})-[r]-(n)
RETURN e, r, n LIMIT 20;
```

- [ ] 实体总数 = 188
- [ ] 关系总数 = 177
- [ ] 第 3 个查询返回非空 graph（可视化能看到星形结构）

### D.3 切到 KG_BACKEND=neo4j 后跑 KG-aware Agent

1. `.env` 设置 `ULTRARAG_KG_BACKEND=neo4j`，重启 Flask
2. 在对话页（任意 KB 但 ifs_docs 最佳）问：

   > "IFS 系统中出库类型是什么？"

3. **预期 Agent 工具会调用 `query_knowledge_graph`**：在 SSE 事件流中应看到 `tool_call: query_knowledge_graph`

- [ ] Agent 模式下看到 `query_knowledge_graph` 工具被调用
- [ ] 返回结果含 "Issue Type 页签" 这类邻居实体（说明 Neo4j 查询有效）

### D.4 切回 KG_BACKEND=sqlite 跑同一个查询

1. `.env` 改回 `ULTRARAG_KG_BACKEND=sqlite`
2. 重启 Flask
3. 重复同一个查询

- [ ] 答案结构相同（说明两后端语义等价）

---

## E. 已知 fails 不影响功能的最后确认（5 分钟）

这 5 个测试**预期会 fail**，但**不影响实际运行**：

| 测试 | 失败原因 | 修复优先级 |
| --- | --- | --- |
| `test_phase2_kb_api.py::TestChatRunnerThreadSafety::test_concurrent_kb_switch_no_race` | FakeRagRunner.chat() 不接受 agent_mode | 低 |
| `test_phase2_kb_api.py::TestChatStreamSse::test_stream_passes_agent_mode_to_runner` | 同上 | 低 |
| `test_rag_runner_agent_mode.py::test_prepare_agent_degraded_when_no_doc_on_hits` | mock `r._index.search` 不兼容 VectorStore 抽象 | 低 |
| `test_rag_runner_agent_mode.py::test_quick_chat_stream_uses_non_streaming_generation` | 同上 | 低 |
| `test_rag_runner_agent_mode.py::test_generation_backend_accepts_backend_alias` | 同上 | 低 |

**判定方法**：
- [ ] B.1（SOP 回归）通过
- [ ] C.2（实际 Flask 对话）正常返回答案

只要这两项过了，5 个测试 fail 就是纯测试层 mock 不兼容，**生产功能不受影响**。

---

## 最终结论

```
A. 环境准备       ___ / 2
B. Phase 4 解析层  ___ / 4 (B.3/B.4 可选)
C. Phase 5.1 存储  ___ / 4 (含 type 路由)
D. Phase 5.2 Neo4j ___ / 4
E. 已知 fails 确认 ___ / 2

合计： ___ / 16
```

**Phase 4 + Phase 5 通过判据**：
- A.1 / A.2 全过
- B.1 / B.2 必过；B.3 / B.4 视可选依赖
- C.1 / C.2 / C.4 全过；C.3 可选
- D.1 / D.2 必过；D.3 / D.4 视 Agent 工具配置
- E 自动满足

---

## 排障速查

| 现象 | 可能原因 | 解决 |
| --- | --- | --- |
| `import faiss` 失败 | venv 没装 faiss | `uv pip install faiss-cpu` |
| `import psycopg` 失败 | 没装 storage extras | `uv sync --extras storage` |
| `import neo4j` 失败 | 同上 | 同上 |
| Qdrant 连接 timeout | 局域网防火墙 / Docker 未启动 | 检查 192.168.8.40:6333 |
| Postgres 密码错误 | URI 中 `#` 未 URL-encode | 改成 `%23` |
| Neo4j auth failed | 密码改过 | 检查 `.env` 中 `ULTRARAG_NEO4J_PASSWORD` |
| Flask 启动报 KB not found | DB 切换后数据没迁 | 跑对应 migrate_* 脚本 |
| chunks.jsonl 中文乱码 | Windows 控制台 cp936 | 用 IDE / 浏览器看，不要直接 cat |
| 双后端验收 KG 数 DIFF | SQLite/PG 数据漂移 | 重跑 `migrate_sqlite_to_postgres.py --truncate` |
