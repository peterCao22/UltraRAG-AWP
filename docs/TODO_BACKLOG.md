# UltraRAG 任务 Backlog

> 最后更新：2026-05-29
> 用途：未来新会话启动时直接读这一份，立即知道有什么待办、当前优先级、对应文档在哪
> 维护规则：任何任务被实施或被取消，都要回来更新本表的状态

---

## 〇、本文档怎么用

**新会话首次进来**，按以下顺序读：

1. 本文 §一（**当前生产状态**）—— 30 秒掌握"现在在哪儿"
2. 本文 §二（**优先级清单**）—— 30 秒看下一个该做什么
3. 想做哪条就跳到对应的"详细任务卡"或外链文档

**完成一项时**：把对应任务卡前面的 🔴/🟡/🟢 改成 ✅，并在 §三 commit history 加一行。

---

## 一、当前生产状态（2026-05-29）

### 检索链路

```
用户 query
  ↓
api/chat.py 拉 session history（最近 6 轮）
  ↓
Phase 11.1.5 意图分类（规则优先 + Haiku 兜底）
  ├─ chitchat / help / data_query → 模板回复短路（零检索零生成）
  └─ knowledge → 继续 RAG
  ↓
RagRunner._prepare_chat_context(history=...)
  ├─ Phase 12.1: resolve_references（指代消解，Claude Haiku 4.5）
  └─ _rewrite_query（query rewrite，主对话模型）
  ↓
Qwen3-Embedding-8B (192.168.8.44:8021/v1) → Qdrant
  ↓
BM25 → RRF 融合
  ↓
bge-reranker-v2-m3 远程 (192.168.8.44:8022/v1/rerank)  ← 2026-05-29 切远程
  ↓
Phase 11.3 双层扩展：
  ├─ Layer 2（STEP 全文）：仅 step_heavy_docs（≥5 STEP）触发，agv_demo 20 doc 中 1 个
  └─ Layer 1（通用邻居）：短 chunk < 350 字按 prev/next_chunk_id 链补到 350-850
  ↓
Phase 12.2 prompt 拼接：[summary] + [最近 6 轮 history] + [检索段] + 当前 query
  └─ done 后异步：每 10 条 message 触发 maybe_summarize（Claude Haiku 4.5）
  ↓
Phase 12.3 反问判定（零 LLM，并行）：
  rerank top_score < 0.30 或 top-5 跨 ≥2 doc → yield clarification SSE 事件
  ↓
LLM 生成（按 admin chat_models 路由：Sonnet 4.6 默认 / Haiku / Opus 备选）
  ↓
Phase 11.1.2 审计：done 后 finally 写 audit_logs（query + answer + chunk_ids
  + clarification meta，append-only）
```

### KB 状态

| KB | type | chunks | Hit@5 |
|---|---|---|---|
| agv_demo | sop_docx | 56 | **0.9000** (Phase 11.3，原 0.8923) |
| ifs_docs | sop_docx (实际为 section 型，零 STEP) | 32 | 1.0000 (Phase 11.3 保持) |
| gen_test | general | — | 未评测 |
| phase_test | general | — | 未评测 |

### 关键运维提示

- ANTHROPIC_API_KEY 2026-05-29 已 rotate，旧 key (`tZ...xZZ-AAA`) 已失效
- 5/28 撞 spend cap 根因已查清：**其他 API 项目占用**（非本 RAG 系统）。已修复其
  他 API 后未再复发；Phase 11.1.6 配额计量任务因此取消（见 §四.3）
- bge-reranker 改远程后释放本机显存 ~3-5 GB
- 默认对话模型在 admin 后台 chat_models 表里是 **Claude Sonnet 4.6**（不是 Opus，避免高额费用）

---

## 二、优先级清单

按 **业务价值 / 工程急迫度** 排序。

### 🔴 高优先级（建议尽快做）

| # | 任务 | 工时 | 跳到详细卡 |
|---|---|---|---|
| ~~1~~ | ~~rag_runner.py 硬编码重构（WeKnora 双层扩展）~~ ✅ 2026-05-29 commit `58143b7` | — | §四.1 |
| ~~2~~ | ~~Phase 12.2 Session Memory~~ ✅ 2026-06-01 commit `1467047` | — | §四.2 |
| ~~3~~ | ~~Phase 11.1.6 Rate Limiting + 配额计量~~ ❌ 2026-06-01 用户决定不做（5/28 spend cap 根因是其他 API 而非本 RAG 系统；改完其他 API 后未复发） | — | §四.3 |

### 🟡 中优先级（按需做）

| # | 任务 | 工时 | 跳到详细卡 |
|---|---|---|---|
| ~~4~~ | ~~Phase 11.1.2 审计日志~~ ✅ 2026-06-01 commit `f9685ec`（**最小版**：仅 QA 事件 append-only；admin UI / 认证打点 / 数据操作打点延期） | — | §四.4 |
| ~~5~~ | ~~Phase 12.3 Clarification（主动反问）~~ ✅ 2026-06-01 commit `38e857b`（零 LLM 实现：rerank 低分 + 跨域 doc 双触发器） | — | §四.5 |
| ~~6~~ | ~~Phase 12.4 Multi-turn Agent 状态优化~~ ✅ 2026-06-02 commit `6348e5c`（选项 B：Haiku 摘要工作记忆 + SSE 推理链可视化） | — | §四.6 |
| ~~7~~ | ~~Phase 11.1.5 Query 意图理解~~ ✅ 2026-06-01 commit `d55a638`（规则 + Haiku 混合；闲聊/帮助短路 = 0 token） | — | §四.7 |
| 8 | Phase 11.1.3 FAQ 库 | 1-1.5 周 | §四.8 |

### 🟢 中低优先级（视团队节奏）

| # | 任务 | 工时 | 跳到详细卡 |
|---|---|---|---|
| 9 | Phase 11.1.1 结构化日志 + 归档 | 2-3 天 | §四.9 |
| 10 | Phase 11.1.4 标签系统 | 4-5 天 | §四.10 |
| 11 | Phase 11.2.1 Follow-up Suggestions | 2 天 | §四.11 |
| 12 | Phase 11.2.3 Query Expansion | 4-5 天 | §四.12 |
| 13 | Phase 11.2.4 向量版本化 + 重 embed 工具 | 1 周 | §四.13 |
| 14 | Phase 11.2.2 Dashboard 首页 | 1 周 | §四.14 |
| 15 | Phase 11.2.5 移动端适配 | 1-2 周 | §四.15 |
| 16 | Phase 11.2.6 i18n 国际化 | 1 周 | §四.16 |

### 🆕 后续 Phase 候选（未排期）

| # | 任务 | 工时 | 备注 |
|---|---|---|---|
| **P9** | **Phase 9 图文联动**（VLM 给图打 caption + 实体 → 图片参与检索 → KG 跨章节联动） | **预计 2-3 周** | docs/Phase9/README.md 方向锚点已就绪；用户 2026-06-03 确定下一个做这个 |
| P-Perm | 用户权限层（**轻量级，非 Phase 10 多租户**）：users 表 + 登录 + user↔role 绑定，复用 Phase 3 `roles/role_kb_permissions` | 5-7 天 | 用户 2026-06-03 确认 Phase 9 后做。**明确不做** Phase 10 多租户改造（业务紧迫性低；users + role binding 已够） |
| ~~Phase 10 多租户~~ | ~~多组织 + 数据隔离 + 跨租户共享 + 联邦检索~~ | ~~6-8 周~~ | ❌ 2026-06-03 决定**不做**。当前是车间内网单租户场景；用户的真实需求是"控制哪些用户能用哪个 KB"，等同于轻量级 RBAC，走 P-Perm 即可 |

### 杂项

| # | 任务 | 备注 |
|---|---|---|
| ~~Z1~~ | ~~18 M + 9 ?? 历史散留 uncommit 改动盘点~~ ✅ 2026-06-02 commit `9646408` / `f4d92fe` / `6c6f8ee` / `3d8a8b1` 全部处理 | working tree 干净 |

---

## 三、近期 commit 历史（按时间倒序）

| commit | 内容 | 日期 |
|---|---|---|
| `6348e5c` | feat(phase12.4): Agent Scratchpad — Haiku 摘要工作记忆 + SSE 推理链可视化；23 单测 | 2026-06-02 |
| `d55a638` | feat(phase11.1.5): Query 意图分类 — 规则 + Haiku 混合；闲聊/帮助短路 = 0 token；36 单测 | 2026-06-02 |
| `38e857b` | feat(phase12.3): Clarification 反问 — rerank 低分 + 跨域 doc 双触发器；零 LLM；17 单测 | 2026-06-01 |
| `f9685ec` | feat(phase11.1.2-min): 审计日志最小实现 — 仅记 QA 事件，append-only；10 单测全过 | 2026-06-01 |
| `1467047` | feat(phase12.2): Session Memory（长会话摘要）+ 主对话 history-in-prompt；多轮 80 条 Hit@5 0.9250→0.9250 不回归 | 2026-06-01 |
| `58143b7` | feat(phase11.3): rag_runner 双层扩展（WeKnora 邻居 + per-doc STEP 门卫）；agv_demo Hit@5 0.8923→0.9000 | 2026-05-29 |
| `85996c0` | docs(env): bge-reranker URL 提示需带 /v1 | 2026-05-29 |
| `bd0c8d5` | docs(tech-debt): 修订探测粒度为 per-doc | 2026-05-28 |
| `971c5fe` | docs(tech-debt): rag_runner 硬编码方案修订 — WeKnora 双层 | 2026-05-28 |
| `75ee17c` | refactor(rag): _rewrite_query 中性化 + 技术债文档 | 2026-05-28 |
| `d60987e` | feat(phase12.1): Context Resolution + 召回 bug 修复 | 2026-05-28 |
| `c4cd756` | docs(phase11.2): 补 agv_demo 评估结论 | 2026-05-27 |
| `fecadcf` | feat(phase11.2): 切块改造 + 切回 bge | 2026-05-27 |
| `470857b` | fix(rag): 长 chunk 检索 + 深度思考路径 LLM 答案被吞 + 图文交错 | 2026-05-27 |
| `6d3b056` | feat(phase11.1-D): 远程 Qwen3-Reranker + EvalRunner F1 修复 | 2026-05-27 |
| `d2fb5cd` | feat(phase11.1-C): 本地 Qwen3-Embedding 切换 + _docs_to_expand 修复 | 2026-05-27 |
| `aaffba3` | feat(phase11.1-A+B): Reranker 预热 + quick 流式输出 | 2026-05-27 |

---

## 四、任务详细卡

### §四.1 🔴 rag_runner.py 硬编码重构（WeKnora 双层扩展）

**目标**：消除 AGV/SOP 专用硬编码，按 chunk 结构自动分流。

**为什么做**：rag_runner.py 面向全部 KB，但目前硬编码"battery/电池/STEP"等 AGV 词，对其他 KB 不友好。agv_demo 20 份 docx 里**只有 1 份**真正含 STEP，但扩展机制对全部 KB 一刀切。

**完整方案**：[`docs/TECH_DEBT_RAG_RUNNER_HARDCODE.md`](TECH_DEBT_RAG_RUNNER_HARDCODE.md)（已写好，包含 §六 实施清单）

**关键设计**：
- **Layer 1（所有 KB）**：WeKnora-style 邻居扩展，短 chunk (<350 字) 按 `prev_chunk_id`/`next_chunk_id` 补足语境到 350-850 字
- **Layer 2（per-doc 门卫）**：仅对真正含 ≥5 个 STEP 块的 doc 启用整本扩展
- **零硬编码**：不识别 STEP/battery 任何领域词

**前置依赖**：
- docx_parser 重切时为每个 chunk 写入 `prev_chunk_id`/`next_chunk_id`（schema 升级）
- 重建 agv_demo / ifs_docs 的 Qdrant collection
- WeKnora 参考实现：`d:\Peter2025\myCursor\WeKnora\internal\application\service\chat_pipeline\merge_expand.go`

**风险**：chunks.jsonl schema 升级 → 必须重建所有 KB 索引；邻居链跨 doc 边界要严格防御

---

### §四.2 🔴 Phase 12.2 Session Memory

**目标**：长会话不丢前面关键信息；解决"LLM 被历史污染拒答"问题。

**为什么做**：用户截图实测发现，连续 3 轮"Alarm Block Battery Low" → "电池组下降" → "ID 34..."，T3 时虽然检索 top-1 命中正确 chunk，但 LLM 看到前 2 轮的换电池流程历史就拒答。

**方案**：
- 每 N 轮（如 10 轮）触发 LLM 摘要，把前段对话压成 200 字
- 摘要存 `kb_sessions.summary` 字段
- 后续 prompt 拼接：`[摘要] + [最近 N 轮] + [当前 query]`
- 关键信息（数字/实体）单独索引，避免摘要丢细节

**参考实现**：WeKnora `internal/application/service/llmcontext/context_manager.go` + `memory/service.go`

**评测**：摘要前后回答的 F1 一致性，不应下降

**待决策**：触发时机（固定 N 轮 / 按 token 量 / 按时间）

---

### §四.3 ❌ Phase 11.1.6 Rate Limiting + 配额计量（已取消，2026-06-01）

**取消原因**：2026-06-01 用户排查确认 5/28 spend cap 根因是**其他 API 项目**（非本
RAG 系统）。改完其他 API 的限额设置后未再复发，本系统不再需要单独做配额计量。
未来若 RAG 调用量级上升或多租户场景上线，再回头评估。

**~~目标~~**：~~防止单用户刷爆配额；统计每 tenant 的 LLM 调用。~~

**~~为什么做~~**：~~2026-05-28 撞 Anthropic spend cap（$100 → 100% used），key 已 rotate 但根因未完全查清。这个能直接挡住未来再发生。~~

**功能**：
- per-tenant / per-user 每分钟 / 每小时 / 每天 调用上限
- per-tenant LLM token 用量统计（落 Postgres）
- 超限返回 429 + 友好提示
- Admin 可调阈值

**风险**：与 11.1.2 审计日志有依赖（共用相同打点机制）

---

### §四.4 ✅ Phase 11.1.2 审计日志（最小版 2026-06-01 commit `f9685ec`）

**实际落地范围**（用户决定降级到 MVP）：
- ✅ Postgres / SQLite `audit_logs` 表，append-only（Repository 不暴露 update/delete）
- ✅ 仅记 `event_type='qa'`：query + answer + chunk_ids + meta（agent_mode / latency 等）
- ✅ chat.py done 后 finally 同步写入，失败静默降级不阻塞主链路
- ✅ 10 单测覆盖：append / 失败降级 / 过滤 / 不存在 update/delete 接口

**已延期到未来需要时再做**：
- ❌ 认证打点（登录/登出/失败/Token 刷新）
- ❌ 数据操作打点（KB CRUD / Document / Chunk / KG）
- ❌ 共享变更 / 配置变更打点
- ❌ Admin UI 查询界面 + CSV 导出
- ❌ 文件冷备 `logs/audit.YYYY-MM-DD.log`
- ❌ 保留期硬约束（6 个月清理脚本）

**触发再做的信号**：合规审计要求出现 / 多租户上线 / 客户索要使用记录

**完整方案残留**：[docs/Phase11/PHASE_11_1_PLAN.md](Phase11/PHASE_11_1_PLAN.md) §11.1.2

---

### §四.5 ✅ Phase 12.3 Clarification（2026-06-01 commit `38e857b`）

**实际落地**：零 LLM 调用，双触发器并发判定
- ✅ 触发器 1：rerank top_score < `ULTRARAG_CLARIFICATION_SCORE_THRESHOLD`（默认 0.30）
- ✅ 触发器 2：top-5 命中 ≥ `ULTRARAG_CLARIFICATION_MIN_CROSS_DOCS`（默认 2）个不同 doc
- ✅ 反问选项从命中 doc 名提取（剥 ' SOP' 后缀、下划线变空格、≤50 字截断）
- ✅ 反问与生成**并行**：SSE 事件流 `status → clarification → status → chunk* → meta → done`
- ✅ 审计：clarification meta 进 `audit_logs.meta`，供后续采纳率统计

**已决定不依赖 11.1.5**：检索分数 + 跨域 doc 两个稳健信号已够用；
未来 11.1.5 落地后可作为第 3 个触发器叠加。

**env 调阈值**：
- `ULTRARAG_CLARIFICATION_ENABLED=0` 全局关
- `ULTRARAG_CLARIFICATION_SCORE_THRESHOLD=0.5` 更保守阈值
- `ULTRARAG_CLARIFICATION_MAX_OPTIONS=2` 最多展示 2 个选项

**Smoke 结果**（agv_demo "怎么处理？"）：
- 触发：`cross_domain:4docs + low_score:0.033<0.300`
- 选项：`['Flexi Case', 'Right Arm FTC', 'Load Unit Not Found']`
- 不阻塞主流程，正常生成答案

**风险监控**（部署后注意）：
- 反问太频繁 → 用户烦 / 实际触发率检查（从 audit_logs 跑 `WHERE meta::jsonb->'clarification'->>'triggered'='true'`）
- 跨域 doc 命中是误报（实际不该跨域）→ 调高 score_threshold

---

### §四.6 ✅ Phase 12.4 Agent Scratchpad（2026-06-02 commit `6348e5c`）

**选项 B 落地**：Haiku 摘要工作记忆 + SSE 推理链可视化（折中方案）
- ✅ 每个 tool_result 调 Haiku 压成 1-2 条 fact 推入 scratchpad
- ✅ messages 数组中最近 1 轮 tool 保留 raw，更早改写为占位
- ✅ system_prompt 拼"已知事实摘要"段供 LLM 决策时看
- ✅ FIFO 上限 10 条（超过删最早）
- ✅ 失败一律降级：LLM 失败 / parse 失败 → agent 主流程不阻塞
- ✅ SSE 增量事件 `scratchpad_update`，前端可动画展示推理链演进
- ✅ done meta.scratchpad 全量统计同步进 audit_logs.meta
- ⏭ 选项 C 的 consolidator（合并相似条目）暂不实现，FIFO 已够

**env 调阈值**：
- `ULTRARAG_SCRATCHPAD_ENABLED=0` 全局关
- `ULTRARAG_SCRATCHPAD_MAX_FACTS=5` 更保守上限
- `ULTRARAG_SCRATCHPAD_KEEP_LATEST_N=2` 保留最近 2 轮 raw

**Smoke 结果**（agv_demo agent 复合 query "Alarm 16 是什么？处理？角色？"）：
- 6 轮 tool 调用，2 次 `scratchpad_update` 事件触发
- Anthropic 配额限制 → 自动 Gemini fallback
- 偶发 parse_error 安全吞掉，agent 正常生成 Master Link Down SOP 答案
- 总结：facts_count=2, total_calls=9, total_summarize_ms=50428

**风险监控**（部署后注意）：
- agent 调用量小：本期场景不该撞 max_iterations=12，scratchpad 上限 10 够用
- scratchpad LLM 偶发 parse_error：当前 Gemini 3.1 输出有时不严格 JSON，
  容错 parse 兜底；可加更严的 JSON-only system prompt

---

### §四.7 ✅ Phase 11.1.5 Query 意图分类（2026-06-02 commit `d55a638`）

**实际落地**：规则优先 + Haiku 兜底，4 类意图
- ✅ `chitchat / help / data_query` 短路模板回复（零检索零 LLM）
- ✅ `knowledge` 默认走 RAG
- ✅ 规则层用正则覆盖常见 pattern（zero-token）
- ✅ LLM 不确定时兜底，失败一律降级 knowledge
- ✅ intent 信息写入 audit_logs.meta，未来可统计意图分布
- ⏭ 12.3 反问的"置信度信号"未集成（12.3 已用检索分数 + 跨域信号，够用）

**env 调阈值**：
- `ULTRARAG_INTENT_ENABLED=0` 全局关
- `ULTRARAG_INTENT_LLM_FALLBACK=0` 规则未命中也走 RAG（节省 LLM 调用）
- `ULTRARAG_INTENT_MIN_CONFIDENCE=0.7` 提高 LLM 采纳门槛

**Smoke 结果**（agv_demo）：
- "你好" → chitchat (rule, 0.85, 0ms) → 模板回复，**0 检索 0 生成**
- "你能做什么？" → help (rule, 0.9, 0ms) → 能力清单
- "Alarm 16 怎么处理" → knowledge → 走 RAG（正常生成 + 触发 clarification）

**降本预期**：业务高峰期闲聊/帮助问询若占 5-10%，省同比例的 embedding + 检索 + 生成 + Haiku（指代消解 / 摘要）调用。

---

### §四.8 🟡 Phase 11.1.3 FAQ 库

**目标**：高置信度精确问答短路 LLM，**降延迟 + 降 token 成本 + 提准确率**。

**关键设计**：
- 独立 Qdrant collection `custom_app__<kb_id>__faq`
- 数据来源：业务方手工录入 `(question, answer)` 对；后期可从高频 query 自动提取
- 高置信度（≥0.85）直接命中不走 LLM

**详见**：[docs/Phase11/PHASE_11_1_PLAN.md](Phase11/PHASE_11_1_PLAN.md) §11.1.3

---

### §四.9 🟢 Phase 11.1.1 结构化日志 + 归档

**目标**：替换单文件 `logs/app.log`，按类型分文件、按日滚动、JSON 格式。

**分类**：app / audit / chat / ingest / kg / error

**保留**：7-30 天（按文件类型配置）

**详见**：[docs/Phase11/PHASE_11_1_PLAN.md](Phase11/PHASE_11_1_PLAN.md) §11.1.1

---

### §四.10 🟢 Phase 11.1.4 标签系统

**目标**：KB / Document 级标签；检索时按标签过滤范围。

**详见**：[docs/Phase11/PHASE_11_1_PLAN.md](Phase11/PHASE_11_1_PLAN.md) §11.1.4

---

### §四.11 🟢 Phase 11.2.1 Follow-up Suggestions

**目标**：每条回答下方自动生成 1-3 个推荐追问。

**详见**：[docs/Phase11/PHASE_11_2_PLAN.md](Phase11/PHASE_11_2_PLAN.md) §11.2.1

---

### §四.12 🟢 Phase 11.2.3 Query Expansion

**目标**：1→N 多 query 并行检索（与 Phase 8 Query Rewrite 配套）。

**详见**：[docs/Phase11/PHASE_11_2_PLAN.md](Phase11/PHASE_11_2_PLAN.md) §11.2.3

---

### §四.13 🟢 Phase 11.2.4 向量版本化 + 重 embed 工具

**目标**：Embedding 模型升级路径；Qdrant payload 加 `embedding_model` 标识。

**为什么**：以后想从 Qwen3-Embedding 切到 bge-m3 / 别的模型时有清晰升级路径。

**详见**：[docs/Phase11/PHASE_11_2_PLAN.md](Phase11/PHASE_11_2_PLAN.md) §11.2.4

---

### §四.14 🟢 Phase 11.2.2 Dashboard 首页

**目标**：用户登录后看：最近问答 / 热门 KB / 系统状态。

**详见**：[docs/Phase11/PHASE_11_2_PLAN.md](Phase11/PHASE_11_2_PLAN.md) §11.2.2

---

### §四.15 🟢 Phase 11.2.5 移动端适配

**目标**：响应式布局；车间用手机问 SOP。

**详见**：[docs/Phase11/PHASE_11_2_PLAN.md](Phase11/PHASE_11_2_PLAN.md) §11.2.5

---

### §四.16 🟢 Phase 11.2.6 i18n 国际化

**目标**：中英文切换（中外员工共用场景）。

**详见**：[docs/Phase11/PHASE_11_2_PLAN.md](Phase11/PHASE_11_2_PLAN.md) §11.2.6

---

### §四.Z1 杂项：历史散留 uncommit 改动盘点

**现状**（2026-05-29）：

```
M .claude/settings.json
M custom_app/db.py
M custom_app/frontend/__tests__/main.test.js
M custom_app/frontend/services/sessionApi.js
M custom_app/repositories/postgres_provider.py
M custom_app/services/agent_runner.py
M custom_app/services/session_store.py
M docs/MANUAL_TESTING.md
M tests/test_phase7_2_a_runner_agent_config.py
M tests/test_sessions_api.py
?? custom_app/services/language_policy.py
?? tests/test_language_policy.py
?? docs/AI Notes.txt
```

**为什么没 commit**：这些是早期遗留改动，跟最近任务不直接相关。需要逐个 review，确定哪些是有用的代码、哪些是临时调试痕迹。

**做法**：找半天时间逐个 `git diff` 看，把好的拆 commit，废的 reset。

---

## 五、关键参考资料

### 项目文档

- [docs/Phase11/PHASE_11_1_PLAN.md](Phase11/PHASE_11_1_PLAN.md) — Phase 11.1 6 个子项详细方案
- [docs/Phase11/PHASE_11_2_PLAN.md](Phase11/PHASE_11_2_PLAN.md) — Phase 11.2 6 个子项详细方案
- [docs/Phase12/README.md](Phase12/README.md) — Phase 12 对话智能化方向锚点
- [docs/Phase12/PHASE_12_1_PLAN.md](Phase12/PHASE_12_1_PLAN.md) — Phase 12.1 详细计划
- [docs/Phase12/PHASE_12_1_SUMMARY.md](Phase12/PHASE_12_1_SUMMARY.md) — Phase 12.1 收官总结
- [docs/TECH_DEBT_RAG_RUNNER_HARDCODE.md](TECH_DEBT_RAG_RUNNER_HARDCODE.md) — 硬编码重构方案（WeKnora 双层）

### WeKnora 参考实现

本地路径：`d:\Peter2025\myCursor\WeKnora\`

- 邻居扩展：`internal/application/service/chat_pipeline/merge_expand.go`
- 长会话记忆：`internal/application/service/llmcontext/context_manager.go` + `internal/agent/memory/`
- 切块：`docreader/splitter/splitter.py`（已借鉴在 Phase 11.2）

---

## 六、决策共识快查（避免重复讨论）

| 议题 | 决策 | 时间 |
|---|---|---|
| Embedding 模型 | Qwen3-Embedding-8B (远程) | Phase 11.1.C |
| Reranker | bge-reranker-v2-m3，2026-05-29 改远程 192.168.8.44:8022/v1 | Phase 11.1.D + 2026-05-29 |
| 默认对话模型 | Claude Sonnet 4.6（admin chat_models 表 is_default） | - |
| 指代消解模型 | Claude Haiku 4.5（廉价改写） | Phase 12.1 |
| 切块策略 | 递归分隔符 400 字目标 + protected regex（WeKnora 借鉴） | Phase 11.2 |
| KB type | 两类：`sop_docx` / `general`（已有 schema 字段） | 既有设计 |
| 指代消解结果展示 | **显示**给用户（灰色提示 + 修正按钮）而非静默 | Phase 12.1 |
| 评测裁判 | Claude Sonnet 4.6（不用 Haiku，判等过严） | Phase 12.1 |
| 召回 bug 修法 | per-doc STEP 探测（不是 per-KB） | 2026-05-28（待实施） |

---

> **本文档是"未来会话的入口"**。新会话进来读完这一份，应该能直接选一个任务开干，不需要再问"上次做到哪了"。
