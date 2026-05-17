# Phase 11.2 —— 用户体验扩展

> **状态**：方向锚点（2026-05-16），详细计划待 Phase 11.1 完成后展开
> **前置**：[Phase 11.1](./PHASE_11_1_PLAN.md) 完成
> **工时估算**：4-5 周
> **退出条件**：6 个主项全部上线；移动端真机验证；i18n 中英文双语界面验证

---

## 一、阶段目标

11.1 让系统**能用**，11.2 让用户**爱用**。覆盖前端体验提升 + 算法增强 + 运维能力。

---

## 二、6 个主项

### 11.2.1 Follow-up Suggestions（2 天）

**目标**：每条回答下方自动生成 1-3 个推荐追问，引导用户深入

**实现**：

- 回答生成后，用同一 LLM（Gemini Flash 即可）跑一个**轻量 prompt**：
  ```
  基于以下问答，生成 3 个用户可能想继续问的相关问题。
  每个 ≤30 字，直接列出，不要解释。
  Q: {query}
  A: {answer}
  ```
- 流式返回：先返回正文，正文流式完成后再返回 suggestions
- 前端显示为可点击 chip

**改动点**：
- [`api/chat.py`](../../custom_app/api/chat.py) `chat_stream` 末尾追加 suggestion 事件
- 前端 `index.html` 渲染 chip + 点击发送

**待讨论**：是否所有问题都生成？建议**仅知识问答 intent 生成**（11.1.5），闲聊不生成。

---

### 11.2.2 Dashboard 首页（1 周）

**目标**：用户登录后看到"我最近的问答 / 热门 KB / 系统状态"

**模块设计**：

| 模块 | 内容 | 数据源 |
|------|------|--------|
| **最近问答** | 当前用户最近 10 条 session | `kb_sessions` + `kb_session_messages` |
| **热门 KB** | tenant 内访问最多的 5 个 KB | `usage_records`（11.1.6） |
| **常用标签** | 热门标签云 | `tags` + 使用频次 |
| **系统状态**（admin 才显示） | Qdrant / PG / Neo4j 可达性 + 配额剩余 | 健康检查 + 11.1.6 配额 |
| **推荐 FAQ** | 热门 FAQ 入口（点了直接问） | `faq` 表 + 命中次数 |

**改动点**：
- 新增前端页面 `frontend/dashboard.html`
- 新增 API `/api/dashboard/summary`（聚合查询）
- 移动端版（与 11.2.5 协同）

**待讨论**：admin 看到的是全 tenant 还是仅本 tenant？建议**根据角色权限可切换**。

---

### 11.2.3 Query Expansion（4-5 天）

**目标**：补齐 Phase 8 Query Rewrite 的不足 —— 单 query 派生多 query

**和 Phase 8 Rewrite 的差异**：

| | Rewrite（已有） | Expansion（11.2.3 新增） |
|---|---|---|
| 数量 | 1 → 1 | 1 → N（典型 3） |
| 用途 | 让 query 更适合检索 | 提高召回 recall |
| 何时用 | 每次检索都先 Rewrite | 召回不足时 Expansion 兜底 |

**实现**：

- 用 Gemini Flash 把 query 派生 3 个变体：
  - 同义词替换："启动" → "开机" / "通电"
  - 上下位替换："AGV" → "搬运机器人"
  - 角度变化："故障 E03" → "E03 含义" / "E03 处理方法"
- 并行检索 N 路，结果用 RRF 融合（Phase 8 BM25 那套）

**改动点**：
- 新增 `custom_app/services/query/expansion.py`
- `rag_runner` 配置项 `query_expansion.enabled`（默认 false，评测后开）
- Phase 8.1 评测脚本加 Expansion 对照组

**待讨论**：
1. Expansion 增加 N 倍 embedding + 检索成本，是否所有 query 都跑？建议**仅检索 hits<K 时触发**（兜底而非默认）
2. 派生 query 数量 3 vs 5？经验值 3 足够

---

### 11.2.4 向量版本化 + 重 embed 工具（1 周）

**目标**：解决"换 embedding 模型，旧数据怎么办"的运维痛点

**设计**：

**向量版本化**：
- Qdrant 每个 point 的 payload 加 `embedding_model: "gemini-embedding-001"` 字段
- KB 元数据加 `current_embedding_model` + `embedding_model_versions: list`
- 检索时校验：query 模型必须 == collection 模型，否则失败

**重 embed 工具**：
- CLI：`python -m custom_app.scripts.reembed_kb --kb agv_demo --new-model bge-large-zh-v1.5`
- 工作流：
  1. 读 `chunks.jsonl`
  2. 用新模型批量重算 embedding
  3. 创建新 Qdrant collection（旧 collection 保留）
  4. 索引切换：原子更新 KB 元数据 → 新 collection 生效
  5. 验证：跑 Phase 8.1 评测集 → 分数不下降才保留新 collection
  6. 旧 collection 保留 7 天后删除（rollback 窗口）

**改动点**：
- chunks.jsonl + Qdrant payload schema 加 `embedding_model`
- `services/embedding_registry.py`：模型注册表
- `scripts/reembed_kb.py`：迁移脚本
- `rag_runner.search()` 加模型校验

**待讨论**：
1. 模型注册表是写死代码还是数据库？建议**先写死**（备选模型有限）
2. rollback 窗口 7 天是否合理？看磁盘成本

---

### 11.2.5 移动端适配（1-2 周）

**目标**：车间工人用手机问 SOP

**关键场景**：
- 车间环境：手机操作，可能戴手套，单手用
- 网络：内网 WiFi，但可能信号不稳
- 显示：屏幕小，长答案要好滚动；图片要适配竖屏

**改动点**：

| 类别 | 改造 |
|------|------|
| **响应式布局** | `frontend/index.html` 加 viewport + 媒体查询 |
| **触屏优化** | 按钮加大；侧边栏改为抽屉式 |
| **图片渲染** | 竖屏自动等比缩放；点击放大 |
| **输入框** | 加语音输入（移动端 Web Speech API） |
| **离线提示** | 网络断开时友好提示 |
| **PWA 可选** | 加 manifest + service worker 让用户能"安装"到桌面 |

**待讨论**：
1. 是否做原生 App？建议**先 PWA**，原生 App 工时太大
2. 语音输入是否本期实现？取决于厂区噪音环境实测

---

### 11.2.6 i18n 国际化（1 周）

**目标**：中英文切换；中外员工共用场景

**改造范围**：

| 类别 | 方案 |
|------|------|
| **前端文案** | 抽 i18n 字符串到 `frontend/i18n/zh-CN.json` + `en-US.json`；用 vanilla JS i18n（避免引入 Vue/React） |
| **后端错误消息** | 错误码体系：返回 code + lang 由前端翻译 |
| **生成回答的语言** | 跟随用户偏好或 query 语言自动判断 |
| **日期时间** | 按地区格式（YYYY-MM-DD vs MM/DD/YYYY） |
| **KB 内容本身** | **不翻译**（SOP 文档原文是中文就是中文）；但回答可指定语言 |

**改动点**：
- 前端：`i18n/` 目录 + `i18n.js` 工具函数
- 后端：错误返回 code 化
- LLM prompt：根据用户语言偏好选择回答语言
- 用户设置：UI 加语言切换器；存到 `users.preferences`

**待讨论**：
1. 自动检测语言 vs 用户手动设置？建议**自动 + 手动覆盖**
2. 是否本期就做日韩等其他语言？建议**只做中英**，留扩展接口

---

## 三、改动点总览

| 类别 | 新增 | 改造 |
|------|------|------|
| **服务层** | `services/query/expansion.py`、`services/embedding_registry.py` | rag_runner.py（多模型校验 + Expansion 路径） |
| **脚本** | `scripts/reembed_kb.py` | — |
| **API** | `/api/dashboard/summary` | chat_stream 加 suggestions 事件 |
| **前端** | `dashboard.html`、`i18n/` 目录、移动端 CSS | `index.html` 响应式 + suggestions chip |
| **数据** | `users.preferences`（含 lang）、`embedding_model` payload | chunks.jsonl schema |

---

## 四、风险

| 等级 | 风险 | 缓解 |
|------|------|------|
| 🟡 MED | Query Expansion 成本上涨 N 倍 | 仅 hits<K 时兜底触发 |
| 🟡 MED | 重 embed 期间检索不可用 | 双 collection 并存 + 原子切换 |
| 🟡 MED | 移动端 SOP 长答案体验差 | 默认折叠长答案；首屏只展开 200 字 |
| 🟢 LOW | i18n 漏翻译 | 加 fallback 到中文；CI 检查文案完整性 |

---

## 五、验收清单

- [ ] 每条回答下方有 1-3 个可点击推荐问题
- [ ] Dashboard 首页 5 个模块全部展示数据
- [ ] Query Expansion 在召回不足时触发；评测脚本能对比开关效果
- [ ] 换 embedding 模型后旧数据能重 embed；rollback 可用
- [ ] 移动端 iOS/Android 真机验证；横竖屏自适应
- [ ] 中英文界面双语切换；后端错误码体系完整

---

## 六、与 Phase 12 的衔接

11.2.1 Follow-up Suggestions 是「**单轮辅助**」—— 用户主动从推荐里选
Phase 12.1 Context Resolution 是「**跨轮辅助**」—— 用户随便说"它"系统也能懂

这两个能力**互补**：11.2 是 UI 引导，12 是 NLP 智能。先做 11.2（简单、收益快），再做 12（复杂、收益深）。
