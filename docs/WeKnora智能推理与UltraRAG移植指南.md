# WeKnora「智能推理」机制整理与 UltraRAG 移植指南

> 本文档基于对 Tencent WeKnora 源码的阅读整理（`D:\Peter2025\myCursor\WeKnora`），说明「智能推理」与「快速问答」的差异、核心流程与工程要点，并**对照 UltraRAG 现有实现**给出可落地的移植思路。  
> **范围**：设计与迁移说明；**不包含**具体代码修改（实施前需单独评审与排期）。

---

## 1. 术语对照

| WeKnora（产品/UI） | 配置字段 / 常量 | 含义 |
|-------------------|-----------------|------|
| 快速问答 | `agent_mode: quick-answer` | 单次 RAG 流水线：检索 → 重排 → 拼上下文 → 一次生成 |
| 智能推理 | `agent_mode: smart-reasoning` | ReAct 多轮：LLM 思考 + 工具调用 + 观察结果，循环直至 `final_answer` 或达到上限 |

内置智能体 ID（参考 `internal/types/custom_agent.go`）：`builtin-quick-answer`、`builtin-smart-reasoning`。

---

## 2. WeKnora「智能推理」在代码里是什么

### 2.1 请求路由

- HTTP：`POST .../agent-qa`（与知识问答路径区分）。
- 逻辑：`internal/handler/session/qa.go` 中根据 `CustomAgent.IsAgentMode()`（即 `agent_mode == smart-reasoning`）选择 `qaModeAgent`，否则退化为普通 `KnowledgeQA`。

### 2.2 执行主体：`AgentEngine`（ReAct 循环）

核心包：`internal/agent/`。

| 模块文件 | 职责（简述） |
|---------|-------------|
| `engine.go` | `Execute` → `executeLoop`：迭代上限、上下文 token 管理、取消与超时、步骤状态 `AgentState` |
| `think.go` | 流式调用带 **Tools** 的 LLM；将思考/工具待调用/最终答案分类型推到 `EventBus`（供 SSE 前端展示「N 个步骤」） |
| `act.go` | 执行工具（支持并行 `errgroup`）、参数 JSON 修复、单工具超时、友好中文 hint（如「关键词搜索」） |
| `observe.go` | 判断结束条件（自然 `stop` 无工具 / `final_answer` 工具）；把工具结果写回消息链并持久化到 context |
| `finalize.go` | 达到最大轮次或 LLM 失败时，用已有工具结果**二次合成**最终回答 |
| `prompts.go` + `config/prompt_templates/agent_system_prompt.yaml` | 系统提示模板、占位符（知识库列表、联网状态、语言等） |

单轮循环可概括为：

1. **Think**：`ChatStream` + `tools` + 可选 `Thinking`。
2. **Analyze**：是否已可结束（直接文本 或 `final_answer`）。
3. **Act**：执行本轮 `tool_calls`。
4. **Observe**：assistant + tool 消息追加到上下文；必要时压缩历史。

### 2.3 与「效果变好」强相关的提示词策略（Progressive Agentic RAG）

模板位置：`config/prompt_templates/agent_system_prompt.yaml` 中 `mode: rag` 的 **Progressive RAG Agent**。

要点（与产品行为一致）：

- **Evidence-First**：领域事实以 KB/联网证据为准，减少幻觉。
- **强制 Deep Read**：`grep_chunks` / `knowledge_search` 一旦返回 `chunk_id` / `knowledge_id`，**必须**再调 `list_knowledge_chunks` 拉**全文块**，不能只靠检索摘要答题。
- **检索顺序**：先 KB（含 Deep Read），再考虑 Web。
- **复杂任务**：可用 `todo_write` 拆步，且要求按顺序执行。
- **收尾**：必须通过 `final_answer` 工具提交答案（或自然结束流式答案），并对引用格式有明确要求。

这些规则与 UI 上「先搜 → 再读某几个 docx → 再综合」的轨迹一致。

### 2.4 工具层（Agent 的「手脚」）

实现目录：`internal/agent/tools/`。与知识相关的典型工具链：

1. `grep_chunks`：关键词/实体锚定。
2. `knowledge_search`：向量语义检索。
3. `list_knowledge_chunks`：**Deep Read**，读完整 chunk 文本（及图片等）。
4. 可选：`query_knowledge_graph`、`get_document_info`；联网：`web_search` / `web_fetch`；规划：`todo_write`；显式思考：`thinking`；收尾：`final_answer`。

Skills（`read_skill` / `execute_skill_script`）依赖沙箱环境变量，与「纯检索推理」可分开规划。

### 2.5 可观测性与前端「步骤条」

`think.go` 根据流式 chunk 的 `ResponseType` 发不同事件（如 `EventAgentThought`、`EventAgentToolCall`、`EventAgentFinalAnswer`），Handler 再转为 SSE。  
因此「已完成 7 个步骤」来自**服务端事件聚合**，不是前端虚构进度。

---

## 3. 「快速问答」在 WeKnora 里是什么（对照用）

走 `KnowledgeQA` → `internal/application/service/session_knowledge_qa.go` 与 `chat_pipeline/*`：

- 典型链路：查询理解 / 扩展 → 检索 → 重排 → 合并片段 → 填入上下文模板 → **单次**流式生成。
- 没有多轮工具循环；召回质量高度依赖**单次**检索与阈值配置。

**与智能推理的本质差异**：是否允许模型在**同一用户问题**下**多轮**主动补检索、读全文、再反思，而不是「一轮定胜负」。

---

## 4. UltraRAG 现状（移植锚点）

当前 AGV 定制链路（Phase 1 核心）大致为：

| 组件 | 路径 | 角色 |
|------|------|------|
| HTTP 对话 | `custom_app/api/chat.py` | `POST /api/chat`，按 `kb_id` 取 `RagRunner`，调用 `chat()` |
| RAG 执行器 | `custom_app/services/rag_runner.py` | 加载 `chunks` + FAISS；`embed_query` → 检索；Jinja 模板 → vLLM/OpenAI 兼容接口生成；返回 `sources`、`answer_blocks` |
| 数据 | `data/kb/<kb_id>/` | `chunks.jsonl` 等 |
| 配置 | `servers/retriever/parameter.yaml`、`servers/generation/parameter.yaml` | top_k、模型等 |

已有**单轮**增强逻辑示例：`rag_runner.py` 内基于正则的「流程/步骤类意图」判断，以及对同一文档的扩展策略（注释中写明的 SOP 相关启发式）。  
这属于**启发式单次扩展**，与 WeKnora **通用多轮 ReAct + 强制 Deep Read** 仍是不同范式。

---

## 5. 移植目标分层（建议按阶段采纳）

不必一次性复刻 WeKnora 全部能力；可按**投入/收益**分层。

### 5.1 层 A —「Deep Read 语义」最小落地（推荐先做）

**目标**：在不引入完整 Agent 框架的前提下，尽量复现「检索摘要不可靠 → 必须读全 chunk」的效果。

**思路**：

- 首轮向量检索得到 `chunk_id` 列表后，对 Top-N（或同一 `doc` 下全部相关 chunk）**无条件拉取完整 `contents`**（或当前管线中等价的「全文块」），再送入生成；避免仅用短 snippet。
- 与现有 `RagRunner` 中「流程类问题扩展同 doc」逻辑**统一**：用显式规则或轻量二次检索替代部分正则特例。

**优点**：改动面小，延迟与成本可控。  
**缺点**：没有多轮反思、没有显式「计划步骤」UI。

### 5.2 层 B — 轻量多轮编排（Python 内小循环）

**目标**：固定最多 K 轮（如 3～5），每轮允许模型选择「预定义操作」之一（非开放函数名也可），例如：

- `search_semantic(query)`  
- `grep_chunks(keywords)`（若 chunks 可全文扫描或有关键词索引）  
- `fetch_chunks(ids)`（Deep Read）  
- `finish(answer)`  

由编排器解析模型输出 → 执行本地函数 → 把结果拼回 messages → 再调模型。  
**不必**一开始就支持 MCP 全生态或并行工具。

**优点**：接近 WeKnora 的体验（步骤可展示、可二次检索）。  
**缺点**：需设计 JSON schema / 解析鲁棒性、token 与超时治理、与现有 SSE 事件模型对齐。

### 5.3 层 C — 对齐 WeKnora 的完整 Agent 产品形态

**目标**：工具注册表、并行工具、上下文压缩、`final_answer` 语义、EventBus 级事件、Skills 沙箱、联网搜索开关等与 WeKnora 同级。

**路径选项**（择一或组合）：

1. **进程内**：在 `custom_app` 新增 `agent_runner` 模块，复用现有 `RagRunner` 的索引与 chunk 存储作为工具实现后端。  
2. **进程外**：独立 Agent 微服务（甚至复用 Go WeKnora 子集），UltraRAG 只做网关与 KB 同步。  
3. **UltraRAG 原生 MCP**：若希望与 `ultrarag run` 管道统一，可把「检索 / 读 chunk / 生成」暴露为 MCP tools，由上游编排器驱动（与当前 Flask 应用解耦度更高）。

**优点**：能力上限高、可观测性强。  
**缺点**：开发与运维成本最高。

---

## 6. 与 UltraRAG 各层的对接清单（实施前设计评审用）

### 6.1 数据与检索

- **Chunk 粒度**：WeKnora 的 `list_knowledge_chunks` 依赖稳定 chunk id 与可拉取的全文；UltraRAG 需确认 `chunks.jsonl` 字段是否满足「按 id 批量取全文 + 图片路径」的接口形状。
- **关键词检索**：若无倒排索引，`grep_chunks` 等价物可以是内存/ SQLite 扫描或后续加轻量索引；产品预期需说明。
- **多知识库 / 租户**：若未来对齐 Phase 2 多租户，Agent 的 search target 需与 `kb_id`、权限一致（参考 `docs/Phase2/05_能力增强路线_参考WeKnora.md`）。

### 6.2 API 与前端（Phase 3）

- **已定稿契约**（请求字段、SSE 扩展类型、`meta` 降级约定、示例 `sendChatMessage`）：见 **`docs/Phase3/04_API对接设计.md`** 中 **`POST /api/chat`** 一节；界面线框与交互见 **`docs/Phase3/03_页面功能设计.md`**「智能体选择器」；开发任务见 **`docs/Phase3/05_开发任务清单.md`**。
- 当前后端：可先只实现 `quick`；对 `agent` 按 `04` 发送 **`meta` + 降级为 `quick`**，前端即可无阻塞联调。
- 对话落库：若需展示「历史步骤」，需存储 `AgentSteps` 或等价 JSON（WeKnora 在 `EventAgentComplete` 中带 `AgentSteps`）。

### 6.3 模型与成本

- 多轮 + 长 chunk 全文会显著增加 **prompt tokens**；需配置：最大轮次、每轮最大 chunk 字数、总上下文上限、是否二次压缩摘要。
- **rerank**：WeKnora Agent 路径在挂载 KB 时要求 rerank 模型；UltraRAG 已有 rerank 配置时需定义 Agent 是否每轮重排或仅首轮重排。

### 6.4 安全与合规

- WeKnora 在用户消息前注入带标签的 runtime 元数据，降低元数据被当作指令的风险；移植时建议保留类似「元数据与指令分离」习惯。
- 联网工具若引入，需 SSRF、域名白名单、出网审计（与 Phase 2 文档中的安全项一致）。

---

## 7. 推荐推进顺序（仍不写代码时的「路线图」）

1. **文档与接口**：冻结层 A/B 选型；更新 `docs/Phase3/04_API对接设计.md` 中的对话 SSE 事件模型草案。  
2. **层 A 验证**：离线评测集对比「仅 snippet」vs「强制全文 chunk」的准确率与延迟。  
3. **层 B 原型**：固定 K 轮 + 3～4 个工具的最小闭环；前端步骤条用占位事件驱动。  
4. **层 C 评估**：若需 Skills/多 MCP，再单独立项（依赖沙箱与运维）。

---

## 8. 参考路径速查

| 仓库 | 路径 | 说明 |
|------|------|------|
| WeKnora | `internal/agent/engine.go` | ReAct 主循环 |
| WeKnora | `config/prompt_templates/agent_system_prompt.yaml` | Progressive RAG 系统提示词 |
| WeKnora | `internal/handler/session/qa.go` | `AgentQA` / `KnowledgeQA` 路由 |
| UltraRAG | `custom_app/services/rag_runner.py` | 当前单轮 RAG |
| UltraRAG | `custom_app/api/chat.py` | 对话 API 入口 |
| UltraRAG | `docs/Phase2/05_能力增强路线_参考WeKnora.md` | 平台化与 WeKnora 参考总览 |

---

## 9. 文档维护

- 本文随 UltraRAG 实施进度更新「层 A/B/C」的完成状态与接口定稿版本号。  
- 若 WeKnora 上游行为变更，以对应版本仓库为准，同步修订第 2～3 节。
