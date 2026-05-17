# Phase 11.1 —— 生产化必需

> **状态**：方向锚点（2026-05-16），详细计划待 Phase 10 完成后展开
> **前置**：Phase 8 + Phase 9 + Phase 10 全部完成
> **工时估算**：4-5 周
> **退出条件**：6 个主项全部上线；审计 + Rate limit 通过双租户验证

---

## 一、阶段目标

补齐「能否投产」的最小集——这些不做，系统就不能交付给业务部门。

---

## 二、6 个主项

### 11.1.1 结构化日志 + 归档（2-3 天）

**目标**：替换当前单文件 `logs/app.log`，按类型分文件、按日滚动、JSON 格式

**关键设计**：

| 日志类型 | 文件名 | 内容 |
|---------|--------|------|
| 应用通用 | `logs/app.YYYY-MM-DD.log` | 启动、配置加载、未分类错误 |
| 审计 | `logs/audit.YYYY-MM-DD.log` | 见 11.1.2 |
| Chat 流 | `logs/chat.YYYY-MM-DD.log` | 每次问答的 kb_id / query / latency |
| Ingest | `logs/ingest.YYYY-MM-DD.log` | 文档解析 / embedding / KG 抽取 |
| KG 抽取 | `logs/kg.YYYY-MM-DD.log` | Phase 6 已有，纳入统一格式 |
| 错误聚合 | `logs/error.YYYY-MM-DD.log` | 所有 ERROR/CRITICAL 级别再独立一份 |

**改动点**：
- [`custom_app/logging_setup.py`](../../custom_app/logging_setup.py) 重写：多 handler、JSON formatter、TimedRotatingFileHandler
- 各模块 `logger = logging.getLogger("ingest")` / `logger = logging.getLogger("audit")` 命名空间化
- 保留 7-30 天（按文件类型配置）；超期自动删除

**JSON 字段约定**：
```json
{"ts": "2026-XX-XX 10:30:00", "level": "INFO", "logger": "chat", "tenant_id": 1, "user_id": "u123", "kb_id": "agv_demo", "session_id": "s456", "msg": "...", "extra": {...}}
```

**待讨论**：是否上 ELK / Loki 集中存储？建议**本期仅做文件分类 + 归档**，集中存储推 Phase 12+ 视规模。

---

### 11.1.2 审计日志（3-4 天）

**目标**：合规所需 —— who-when-what 完整追溯链

**记录范围**：

| 类别 | 事件 |
|------|------|
| **认证** | 登录 / 登出 / 登录失败 / Token 刷新 |
| **数据操作** | KB CRUD / Document 上传删除 / Chunk 修改 / KG 重建 |
| **问答** | 每条 query + retrieved chunks + answer 摘要（含 tenant/user/session） |
| **共享变更** | KB 分享 / 撤销 / 权限变更（Phase 10.3） |
| **配置变更** | Admin 改模型 / 改 prompt / 改 agent 配置 |

**存储**：
- 主存储：**Postgres** 表 `audit_logs`，便于查询
- 镜像：同时落 `logs/audit.YYYY-MM-DD.log` 作为冷备 / 防篡改备份
- 保留：法规建议 ≥6 个月，**待你确认行业要求**

**Admin 查询界面**：
- 按 tenant / user / 时间区间 / 事件类型过滤
- 导出 CSV

**改动点**：
- 新增 `custom_app/repositories/audit_repository.py`
- Flask `before_request` 中间件统一打点
- Admin 页加「审计日志」标签

**待讨论**：
1. 问答记录是否存全文 / 还是只存 query 哈希 + chunk ids？影响存储和合规
2. 审计能否被删除？建议**只允许追加，禁止删改**（数据库层加约束）

---

### 11.1.3 FAQ 库（1-1.5 周）

**目标**：高置信度精确问答短路 LLM，**降低延迟 + 降低 token 成本 + 提升准确率**

**关键设计**：

| 维度 | 设计 |
|------|------|
| 存储 | 独立 Qdrant collection `custom_app__<kb_id>__faq`，与 chunk collection 平行 |
| 数据来源 | 业务方手工录入 `(question, answer)` 对；后期可从高频 query 自动提取 |
| 检索路径 | query → 同时召回 FAQ + Chunk；FAQ 相似度 **≥ 阈值（默认 0.85）** 时直接返答案不走 LLM；否则 FAQ 作为 reference 一起送 LLM |
| Schema | `{id, kb_id, question, answer, question_vec, hits_count, created_at, updated_at}` |
| 阈值可调 | 配置 yaml；不同 KB 可独立阈值 |

**Admin 界面**：
- FAQ CRUD 页（每条手动录入）
- 「从高频 query 推荐」按钮（后期，从 audit 日志统计）

**改动点**：
- 新增 `custom_app/services/faq/`：`store.py`（FAQ Qdrant 读写）+ `matcher.py`（检索路径融合）
- [`rag_runner.py`](../../custom_app/services/rag_runner.py) chat 入口前置 FAQ 路径
- Admin UI 加 FAQ 管理

**待讨论**：
1. 阈值 0.85 是否合理？需要业务方先录 20-30 条样本试跑
2. FAQ 命中时**是否记录命中日志**用于改进？建议是

---

### 11.1.4 标签系统（4-5 天）

**目标**：KB 多了之后，按标签过滤检索范围（如「设备类」/「软件类」/「安全类」）

**关键设计**：

| 粒度 | 用途 |
|------|------|
| **KB 级标签** | 整库标签；用户按标签筛选 KB |
| **Document 级标签** | 单文档标签；检索时按文档标签过滤 chunks |
| ~~Chunk 级标签~~ | 不做，过细 |

**数据模型**：
- `tags` 表：`(id, tenant_id, name, color, created_at)`
- `kb_tags` 关联表：`(kb_id, tag_id)`
- `document_tags` 关联表：`(document_id, tag_id)`
- 同 tenant 内 tag name 唯一

**检索时如何用**：
- 用户在 chat 界面选「只查含 X 标签的内容」
- 后端 `rag_runner.search(query, filter={"tags": ["safety"]})`
- Qdrant payload `tags: list[str]` + filter 子句

**改动点**：
- 新建 `tag_repository.py`
- chunks.jsonl + Qdrant payload 加 `tags` 字段（向下兼容空数组）
- Admin UI：KB 设置页 + Document 列表加 tag 管理
- Chat UI：query 输入框旁加 tag 筛选器

**待讨论**：
1. tag 是 tenant 内全局共享，还是 KB 内独立？建议**tenant 内全局**（避免重复定义）
2. 是否给 tag 加层级（父子）？本期建议**不加**，需要时再说

---

### 11.1.5 Query 意图理解（3-4 天）

**目标**：分流 —— 不该走 RAG 的不走，省钱省延迟

**意图分类**：

| 意图 | 处理 | 例子 |
|------|------|------|
| **闲聊** | 不走检索，直接 LLM 短答 | "你好"、"谢谢" |
| **知识问答** | 走完整 RAG | "AGV 怎么启动" |
| **数据查询**（推迟） | 走 SQL/API（接业务系统） | "本月 AGV 故障次数" |
| **元问题** | 系统介绍 / 帮助 | "你能做什么" |

**实现方式**：

| 候选 | 说明 |
|------|------|
| **A. LLM 分类**（推荐） | 用 Gemini Flash 一次轻量分类，token 成本可忽略 |
| B. 关键词 + 规则 | 简单场景够用，但维护成本高，泛化差 |
| C. 训练分类器 | 工时大、过度设计 |

**改动点**：
- 新增 `custom_app/services/intent/classifier.py`
- chat 入口 `rag_runner.chat()` 前置 intent 分类
- 不同意图走不同 prompt 模板
- 日志记录 intent 分布，用于优化

**待讨论**：
1. **数据查询意图**本期不实现路由，但分类要识别出来，让用户知道"这个功能开发中"；后续可接 IFS API 等
2. 元问题是写死回复还是 LLM 生成？建议写死（减少 token + 一致体验）

---

### 11.1.6 Rate Limiting + 配额计量（3-4 天）

**目标**：防滥用 + 按租户成本核算

**两件事**：

**Rate Limit（防滥用）**：
- per-user：每分钟最多 N 次 chat（防点击/脚本刷）
- per-tenant：每天最多 M 次 chat（防整租户滥用）
- 超出返 429，UI 友好提示

**配额计量（成本核算）**：
- 记录每次调用：tenant_id + 模型 + input_tokens + output_tokens + 时间
- 表 `usage_records`：`(id, tenant_id, user_id, model, kind, input_tokens, output_tokens, latency_ms, created_at)`
  - kind: `chat` / `embedding` / `kg_extract` / `image_caption`
- Admin Dashboard 显示：每 tenant 月度成本（按模型 token 价格换算）

**实现**：
- Rate limit 用 Redis（轻量；现有栈没 Redis，本期决定**新增 Redis 还是用 Postgres 简单计数**？）
- 计量直接写 Postgres

**改动点**：
- 新增 `custom_app/services/quota/`：`limiter.py` + `recorder.py`
- Flask 中间件：所有计费请求经过 limiter
- LLM 调用包装层：所有 Gemini 调用走 `recorder.record()`

**待讨论**：
1. 用 Redis 还是 Postgres 做 rate limit？Postgres 简单但精度差；Redis 准确但增运维成本
2. 超限是硬阻断还是降级（用便宜模型）？建议硬阻断 + UI 提示

---

## 三、改动点总览

| 类别 | 新增 | 改造 |
|------|------|------|
| **配置 / 基础设施** | 11.1.1 日志重写 | logging_setup.py |
| **数据库** | audit_logs / faq_<kb_id> / tags / kb_tags / document_tags / usage_records | Qdrant 加 tags payload |
| **服务层** | `services/faq/`、`services/intent/`、`services/quota/` | rag_runner.py |
| **Repository** | audit / tag / faq / usage_records | — |
| **API** | `/api/audit`、`/api/tags`、`/api/faq`、`/api/quota` | chat.py 前置 intent / rate limit |
| **Admin UI** | FAQ 管理 / Tag 管理 / 审计日志查询 / 配额仪表 | KB 设置加 tag |
| **Chat UI** | tag 筛选器 | 429 错误提示 |

---

## 四、待讨论清单（汇总）

| 主项 | 待定 |
|------|------|
| 11.1.1 | 本期是否上 ELK / Loki？建议否 |
| 11.1.2 | 审计保留期；问答记录是否存全文；是否禁止删除 |
| 11.1.3 | FAQ 阈值；是否记录命中日志 |
| 11.1.4 | tag 作用域（tenant 全局 vs KB 内）；是否做层级 |
| 11.1.5 | 数据查询路由是否本期实现 |
| 11.1.6 | Rate limit 存储（Redis vs Postgres）；超限硬阻断 vs 降级 |

---

## 五、风险

| 等级 | 风险 | 缓解 |
|------|------|------|
| 🔴 HIGH | FAQ 阈值过低 → 错误命中误导用户 | 业务方录入审核流程；上线前用 Phase 8.1 评测集测一遍 |
| 🟡 MED | 审计日志写入失败影响主链路 | 异步写 + buffer；主链路绝不阻塞 |
| 🟡 MED | 意图分类误判 → 知识问题被当闲聊 | 分类置信度阈值；不确定时默认走 RAG |
| 🟡 MED | Rate limit 误伤合法高频用户 | per-tenant 限额由 Admin 可调 |

---

## 六、验收清单

- [ ] 6 类日志文件按日滚动 + 7-30 天自动清理
- [ ] 审计日志覆盖 5 类事件（认证 / 数据操作 / 问答 / 共享 / 配置）
- [ ] FAQ 命中场景：阈值 ≥0.85 不走 LLM；阈值不够走完整 RAG
- [ ] 标签筛选：UI 选标签 → 只返回该标签下 chunks
- [ ] 意图分类：闲聊不走检索；日志能看到 intent 分布
- [ ] Rate limit：per-user / per-tenant 限额生效；超限返 429
- [ ] 配额计量：Admin 能看到每 tenant 月度成本
