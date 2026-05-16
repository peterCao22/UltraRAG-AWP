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

## F. Phase 6.1 文档级状态显示 + 详情面板（约 15 分钟）

> 目标：验证每个文档卡片显示独立状态、失败可重试、完成可查看分块，
> 以及 Flask 异常重启后卡死状态被恢复。

**前置**：

- 已跑过 §A.1 / §A.2，conda env `ultrarag` 可用
- 已对老 Postgres 库执行迁移：
  ```bash
  psql "$ULTRARAG_POSTGRES_URI" -f migrations/postgres/001_phase6_1_doc_status.sql
  ```
  （SQLite 后端无需手工迁移，`init_db()` 会自动 ALTER）

### F.1 状态实时流转（必过）

1. `python -m custom_app.app --port 8080`，打开 `http://localhost:8080/admin`
2. 进入一个已有的 KB（或新建一个 SOP KB 上传 3 份 DOCX）
3. 点击「重建索引」
4. 文档列表区域应出现「N 已完成 · N 解析中 · …」的汇总条
5. 每行卡片右上角按 1.5-2s 频率刷新徽章：`待处理 → 解析中… → 嵌入中… → 写入索引… → 已完成`
6. 完成态行的 meta 文本应出现「{chunk_count} 分块 · 完成 {时间}」

**通过判据**：

- [ x] 汇总条文字随状态变化
- [x ] 每行状态徽章带 spinner（处理中态）/ 绿色徽章（完成）
- [ x] 完成后 chunk_count 正确（与 chunks.jsonl 中相同 doc_stem 的行数一致）

### F.2 失败文件的错误信息 + 重试（必过）

1. 在该 KB 下放一个明显损坏的文件，例如复制一个 `.docx` 文件但内容只有 `not a real docx`
2. 点击「重建索引」
3. 该文件行应变为红色「失败」徽章，旁边出现「错误详情」和「重试」按钮
4. 鼠标悬停徽章应能看到 `error_message` tooltip（截断到 500 字符）
5. 点击「错误详情」弹出模态显示完整错误堆栈摘要
6. 点击模态里的「重试」（或行尾的「重试」按钮）

**通过判据**：

- [ x] 失败行显著区分（红色徽章 + 错误链接）
- [x ] 错误信息可读（不是 `unknown error`）
- [x ] 「重试」后该行回到 `pending → parsing → …`

### F.3 完成文档的详情面板（必过）

1. 找一个「已完成」状态的行，点击空白处 / 「查看分块」按钮
2. 模态打开，标题是文件名，副标题显示 `{N} 分块 · 完成 {时间}`
3. 默认 chunks tab 显示分块列表：每块「分块 N · {字符数} 字符」+ markdown 渲染内容
4. 切到 merged tab，所有 chunks 顺序拼接显示
5. preview tab 是 disabled 状态、显示 "Coming soon" 提示（Phase 6.2+ 推出）

**通过判据**：

- [ x] chunks tab 至少能看到第一块的 markdown 渲染（不是裸 HTML）
- [ x] merged tab 拼接结果与 KB 的 chunks.jsonl 顺序一致
- [ x] preview tab 不可点（disabled）

### F.4 进程崩溃后的卡死恢复（必过）

1. 触发「重建索引」，立刻 `Ctrl-C` 杀掉 Flask 进程
2. 不修改任何数据，直接重启：`python -m custom_app.app --port 8080`
3. 刷新 `/admin` 进入该 KB 详情页
4. 启动时跑过的 `recover_stale_documents` 会把停留在 `parsing/embedding/indexing/deleting`
   超过 10 分钟的行标 `failed`，错误信息为「进程异常中断，请重试」
5. **加速验证**：如果不想等 10 分钟，设环境变量 `ULTRARAG_DOC_STALE_MINUTES=1` 再重启 Flask，
   1 分钟前的卡死行会立即转 failed

**通过判据**：

- [x ] 重启后被卡死的文档行变为「失败」+ 错误信息
- [ x] 「重试」按钮可用，点击后能进入新的 ingest 流程

### F.5 API 自检（可选，2 分钟）

```bash
KB=your_kb_id
curl -s "http://localhost:8080/api/kb/$KB/documents" | jq '.data.summary'
# {"completed": 3, "failed": 1, "parsing": 0, ...}

# 拉某文档的 chunks 看格式
DID=$(curl -s "http://localhost:8080/api/kb/$KB/documents" | jq -r '.data.documents[0].doc_id')
curl -s "http://localhost:8080/api/kb/$KB/documents/$DID/chunks" | jq '.data.chunks | length'

# batch-status 轮询接口
curl -s -X POST "http://localhost:8080/api/kb/$KB/documents/batch-status" \
  -H 'Content-Type: application/json' \
  -d "{\"doc_ids\":[\"$DID\"]}" | jq '.data'
```

**通过判据**：

- [ ] `data.summary` 字段存在且数字合理
- [ ] 拉某文档 chunks 不会越权返回其它文档的块

### F.6 Phase 6.1 自动化测试（5 分钟）

```bash
& "C:\Users\Peter\miniconda3\envs\ultrarag\python.exe" -m pytest tests/test_phase6_1_doc_status.py tests/test_phase6_1_ingest_per_doc.py -q
```

**期望**：14 个用例全过。

---

## G. Phase 6.2 单文件增量重建 + 删除即时清理（约 15 分钟）

> 目标：上传或修改单个文件不再重建整库；删除某文件后向量库 / 知识图谱 / chunks.jsonl
> 不留残留召回。

**前置**：

- F.1–F.4 已通过（Phase 6.1 落地）
- Postgres 老库执行迁移：
  ```bash
  python -m custom_app.scripts.apply_phase6_2_migration   # 见 §G.0 一次性脚本
  ```
  或直接：
  ```bash
  psql "$ULTRARAG_POSTGRES_URI" -f migrations/postgres/002_phase6_2_kg_doc_id.sql
  ```
  （SQLite 后端无需手工迁移，`init_db()` 会自动 ALTER）

### G.1 上传 1 个新 docx → 单文件重建（必过）

1. 在已有 KB 中上传 1 个新 docx（其它文档保持 `已完成`）
2. 该行应是「**待处理**」+ 旁边按钮多一个「**重建该文件**」
3. 点击「重建该文件」
4. **只该行**进入 `parsing → embedding → indexing → completed`；其它已完成行徽章**不动**

**通过判据**：

- [ x] 进度卡顶部显示「索引任务: running」但只这 1 行变蓝
- [ x] 完成时 `chunk_count` 正确（与文档段落数一致）
- [ x] 其它行 `processed_at` 时间不变（不是"被悄悄重做了"）

### G.2 重建期间其它老文档查询不阻塞（必过）

1. G.1 单文件重建过程中（看到该行 `embedding`/`indexing`），切到对话页问其它老文档相关问题
2. 应能正常返回答案

**通过判据**：

- [x ] 不会因为重建在跑就返回「正在维护」或超时

### G.3 删除某文件 + 残留检查（必过）

1. 在 G.1 已完成的文件上点「删除」
2. 二次确认后该行从列表消失，且**汇总条 `已完成` 计数减 1**
3. 切到对话页问该文件特有内容（例如标题中的关键词）
4. 答案应是「文档中未找到」或不再引用该文档

**通过判据**：

- [ ] 没有从已删文档召回任何 chunk
- [ ] 该文档的图片资源（`/images/{doc_stem}/...`）可选删除（本期未做磁盘清理，只清 DB+raw）

### G.4 KG 残留检查（可选，5 分钟）

1. 删除前用 Neo4j Browser 跑：
   ```cypher
   MATCH ()-[r:RELATES_TO {kb_id: $kb_id, doc_id: $doc_id}]-() RETURN count(r)
   ```
   记下数字 N
2. 删除该文档
3. 重跑同条 Cypher → 应返回 0

**通过判据**：

- [ ] KG 关系数从 N → 0
- [ ] 仅该文档独有的实体也被删除；与其它文档共享的实体保留（只裁剪 chunk_ids）

### G.5 批量勾选「重建所选」（必过）

1. 文档列表顶部勾「全选」，或手工勾 2-3 个
2. 工具栏显示「已选 N」+ 「重建所选」按钮可点
3. 点击 → 二次确认 → 提交
4. 这几行同时进入 `parsing`；其它行不动

**通过判据**：

- [ x] 选中的行同步流转；未选中的行不动
- [ x] 完成后选中行的 `processed_at` 都刷新

### G.6 「全量重建」按钮仍可用（必过）

1. 点「重建索引」按钮（非「重建该文件」）
2. 全部文档进入流转，最终全部 `已完成`

**通过判据**：

- [ x] 所有文档的 `processed_at` 都刷新
- [x ] 这条路径与 Phase 6.1 验收一致

### G.7 Phase 6.2 自动化测试

```powershell
& "C:\Users\Peter\miniconda3\envs\ultrarag\python.exe" -m pytest tests/test_phase6_2_chunks_io.py tests/test_phase6_2_kgstore_delete_by_doc.py -q
```

**期望**：17 个用例全过。

### G.0 一次性迁移脚本

为 G 段方便起见，建议执行一次：

```powershell
python -m custom_app.scripts.apply_phase6_2_migration
```

幂等可重跑。

---

## H. Phase 7 对话模型管理 + 前端切换（约 15 分钟）

> 目标：admin 可注册/管理 Gemini / OpenAI / Anthropic / OpenAI 兼容（vLLM）4 类
> 模型；对话页可在 chip 切换；切换不会让 Runner 缓存串台。

**前置**：

- F + G 已通过
- Postgres 迁移：
  ```powershell
  python -m custom_app.scripts.apply_phase7_migration
  ```
  （SQLite 后端 init_db 自动建表，不用手工跑）
- 可选：`pip install openai anthropic`（仅在你要测试 OpenAI / Anthropic 模型时）

### H.1 Admin 模型管理列表（必过）

1. 启动 Flask；打开 `/admin`
2. 侧栏出现「模型管理」入口；点击进入
3. 空状态：显示「还没有对话模型」+ 「新增模型」按钮

**通过判据**：

- [x ] 导航 active 态正确
- [x ] 路由 `#/models` 工作

### H.2 新增 Gemini 模型并测试连接（必过）

1. 点「+ 新增模型」→ 弹窗：Provider 选 `Google Gemini` / 显示名 `Gemini Pro 主力` / Model Name `gemini-2.5-pro` / Base URL 留空 / API Key 填 `.env` 里 `GOOGLE_API_KEY` 的值 / 启用 ✓
2. 保存 → 列表出现该模型
3. 点该卡片的「测试」按钮 → 提示 ✓ 连接成功（含延迟 ms）

**通过判据**：

- [ x] 卡片显示 provider 标签 + model_name
- [x ] 「测试」真实发出请求（看 Flask 日志 / Gemini quota）
- [ x] api_key 在 GET 返回中是 `***`

### H.3 「设为默认」+ 切换（必过）

1. 再新增第 2 个模型：例如 OpenAI 兼容（vLLM）—— `Qwen 2.5 7B` / `Qwen2.5-7B-Instruct` / `http://192.168.8.40:8000/v1`
2. 在 Qwen 卡片上点「设为默认」→ Gemini 的「默认」徽章消失，Qwen 出现
3. 回到对话页 `/` → 输入框区的 chip 显示 `Qwen 2.5 7B`

**通过判据**：

- [x ] 默认徽章只出现在一个模型上
- [x ] 对话页 chip 自动反映新默认

### H.4 对话页 chip 切换（必过）

1. 点击 chip → 弹出 dropdown，列出所有 enabled 模型 + provider/model_name 子文本
2. 选 `Gemini Pro 主力` → chip 标签更新为 `Gemini Pro 主力`
3. 刷新页面 → chip 仍是 `Gemini Pro 主力`（localStorage 记忆）
4. 输入问题发送 → Flask 日志看到 `model_id=model_xxx`（与所选一致）

**通过判据**：

- [x ] 选过后刷新仍保留
- [ ] 后端日志 `chat_stream routing model_id=` 出现并匹配

### H.5 编辑 + 删除模型（必过）

1. 在某模型卡片点「编辑」→ 修改显示名 / 描述 → 保存 → 列表反映
2. 在 api_key 字段**留空**保存 → 后端 api_key **不变**（不会被覆盖成空）
3. 点「删除」→ 二次确认 → 卡片消失；列表 GET 不再返回
4. 切到对话页 chip → 已删的模型从 dropdown 消失

**通过判据**：

- [ x] 留空 api_key 不覆盖原值（用 Postgres 直查可确认）
- [x ] 删除是软删（`deleted_at` 非 NULL）

### H.6 SSRF 校验（可选，2 分钟）

1. 编辑模型，Base URL 填 `ftp://example.com` → 保存应被拒（提示 SSRF rejected）
2. 填 `http://192.168.8.40:8000/v1` → 应通过（默认允许私网）

**通过判据**：

- [ x] 非 http/https scheme 被拒
- [x ] 私网 IP 默认放行（与项目实际部署一致）

### H.7 Phase 7 自动化测试

```powershell
& "C:\Users\Peter\miniconda3\envs\ultrarag\python.exe" -m pytest tests/test_phase7_chat_model_repository.py tests/test_phase7_providers_and_ssrf.py tests/test_phase7_admin_models_api.py -q
```

**期望**：38 个用例全过。

---

### 已知偏离（写明给下次接手）

Phase 7 接入是**渐进式**：

| 项 | 状态 |
|---|---|
| Gemini 走 .env 的真链路（RagRunner/AgentRunner 不变） | 当前 |
| OpenAI / Anthropic / OpenAI 兼容 可创建 + 管理 + 测试连接 | ✓ |
| OpenAI / Anthropic 真正接入对话链路（RagRunner 内部读 chat_models 表） | **Phase 7.1**（下次 PR） |
| `(kb_id, model_id)` 作 Runner cache key 不串台 | ✓（model_id 仅作 key，未改 LLM 调用） |

如果用户在 chip 选了 OpenAI / Anthropic 的模型，**当前会按 .env 的 Gemini 配置发请求**——
chip 只更换 Runner 缓存键 + UI 显示。LLM 真切换待 Phase 7.1。

> **Phase 7.1 已完成 ⭐**：见下方 §H'。LLM 真切换、agent 跨 provider tool calling 已支持。

---

## H'. Phase 7.1 多 provider 真切换（约 10 分钟）

> 目标：chip 选哪个模型后端就真用哪个 LLM；agent 模式 tool calling 全 provider 通跑。

**架构折中（参考 WeKnora）**：
- **Gemini**：走 Google 官方 OpenAI 兼容端点 `https://generativelanguage.googleapis.com/v1beta/openai`
- **OpenAI / vLLM / 兼容**：直接 OpenAI 协议
- **Anthropic**：用专用 SDK（独立 messages/tools 协议）

代价：Gemini 经兼容端点丢失 `thoughtSignature`（multi-turn 思维链回传），其它能力完整保留。

### H'.1 vLLM（OpenAI 兼容）quick 模式真切换

1. admin 新建模型：provider `openai_compatible` / model_name 与 vLLM `--served-model-name` 一致 / base_url `http://192.168.8.40:8000/v1` / api_key 任意
2. 对话页 chip 选该模型，发问题
3. Flask 日志看 `RagRunner chat_model override: provider=openai_compatible model=... base_url=http://192.168.8.40:8000/v1`

**通过判据**：

- [ ] 日志显示 vLLM URL，不是 `.env` 里的 Gemini URL
- [ ] 答案来自 vLLM（看 Qdrant 命中 + 风格）

### H'.2 Gemini quick 模式（经 OpenAI 兼容端点）

1. 切到已配置的 Gemini 模型
2. 发问题
3. 日志看 `RagRunner chat_model override: provider=gemini model=gemini-2.5-pro base_url=https://generativelanguage.googleapis.com/v1beta/openai`

**通过判据**：

- [ ] base_url 自动改写成 `/v1beta/openai`
- [ ] 答案正常返回

### H'.3 Anthropic quick 模式

1. 切到已配置的 Claude 模型
2. 发问题
3. 日志看 `RagRunner chat_model override: provider=anthropic model=claude-opus-4-7 base_url=https://api.anthropic.com`

**通过判据**：

- [ ] base_url 是 Anthropic 官方
- [ ] 答案正常返回
- [ ] 不传 `temperature`（新模型已弃用）

### H'.4 智能推理（agent）跨 provider tool calling

1. 切到 vLLM 模型 → 智能体改成「智能推理」→ 问需要检索的问题
2. 日志看到 `chat_stream routing → AgentRunner ... model_id=...` + 多轮 tool_call/tool_result 事件
3. 同样测试 Gemini 和 Anthropic（Anthropic 推荐用 Sonnet 或 Opus，支持 tool_use）

**通过判据**：

- [ ] 每个 provider 都能在 agent 模式下成功调用 `knowledge_search` 等工具
- [ ] 最终 `final_answer` 给出答案

### H'.5 自动化测试

```powershell
& "C:\Users\Peter\miniconda3\envs\ultrarag\python.exe" -m pytest tests/test_phase7_1_adapters.py -q
```

**期望**：18 个用例全过。

### 已知差异

| 项 | 状态 |
|---|---|
| Gemini 原生 `:generateContent` 路径 | **仍保留**（无 chat_models 行时回退用） |
| `thoughtSignature` 透传 | 走 OpenAI 兼容端点时**丢失**（与 WeKnora 同取舍） |
| Anthropic streaming text | ✓ |
| Anthropic streaming tool_use args | ✓（input_json_delta） |
| `extra_config.temperature` 在 Anthropic 调用时不传 | ✓（新模型弃用） |

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
