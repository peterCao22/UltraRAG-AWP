# Phase 7.2.A —— Agent 配置与 system_prompt 管理

> **状态**：✅ 已完成 + 联调验收（2026-05-16）
> **前置**：[Phase 7.1](./PHASE_7_1_COMPLETION.md)（多 provider 真切换）
> **参考实现**：Tencent WeKnora `internal/types/custom_agent.go` / `config/builtin_agents.yaml` / `config/prompt_templates/`
> **范围**：仅做最小价值版（7.2.A）；完整 agent 配置（推荐问题、检索策略 per-agent 覆盖、VLM、IM 集成等）推后 7.2.B

---

## 一、问题

Phase 7.1 完成后，用户切到 vLLM 模型对话能正常工作，但发现：

> 同样的问题，Claude 输出有粗体标题、嵌套 bullet、分段空行，**排版漂亮**；vLLM（Qwen3.6）输出是朴素单层 bullet，**排版扁平**。

根因有二：

1. **模型能力差异**：Claude 训练时强调结构化输出，自带 markdown 排版能力；开源模型（Qwen / Llama / DeepSeek）默认输出朴素
2. **system_prompt 一刀切**：`servers/generation/parameter.yaml` 里的 system_prompt 是 AGV SOP 专用、要求"严肃汇报"风格，对所有模型生效，限制了模型的排版自由度

> ```
> You are a professional AGV (Automated Guided Vehicle) operations assistant for SOP-based Q&A.
> Follow the user message instructions exactly: use only the provided excerpts.
> Never omit procedural steps or safety items from those excerpts (faithful translation / rephrasing only).
> ```

Claude 因为模型强势，仍能自发美化排版；vLLM 上的模型直接按 prompt 字面服从，输出最朴素。

---

## 二、目标

| 项 | 验收 |
|---|---|
| 把 system_prompt 从 yaml 移到数据库，per-agent 独立配置 | `chat_models` 之外建 `agent_configs` 表 |
| 至少两个内置 agent | `builtin-quick` / `builtin-agent`，可在 admin 编辑（含 prompt） |
| 用户可自定义 agent | 创建第 N 个 agent，独立 prompt + 关联 model_id |
| Placeholder 渲染 | 支持 `{{language}}` / `{{current_time}}` / `{{kb_name}}` 等 |
| 对话页保持原 chip + dropdown 不变 | dropdown 的 "智能体：快速问答 / 智能推理" 改为读 agent_configs 表 |
| 向后兼容 | 无 agent_configs 行时回退到 yaml 老路径 |

---

## 三、设计决策（已确认）

| 议题 | 决策 |
|---|---|
| **Agent 与 Model 的关系** | Agent 配置 `model_id` 引用 chat_models 表；切 agent 时模型可能联动切换。MVP **不强制 agent.model_id**（空时用对话页 chip 选的模型） |
| **Agent 与 KB 的关系** | MVP **不绑定**（agent 全局可用）；WeKnora 的 `kb_selection_mode` 留到 7.2.B |
| **Built-in agent 来源** | 不像 WeKnora 用 YAML，直接在 init_db 时插入两条种子数据；用户可在 admin 编辑（包括 prompt） |
| **Placeholder MVP 子集** | `{{language}}` / `{{current_time}}` / `{{kb_name}}` / `{{kb_description}}`；**不**做 `{{contexts}}` `{{query}}`（这些是 RagRunner 内部拼接的，不应让用户自己写） |
| **agent_mode 字段** | 'quick' / 'agent'，与现有前端 dropdown 保持一致；不引入 WeKnora 的 'quick-answer' / 'smart-reasoning' |
| **多租户** | 同 chat_models，预留 `tenant_id INTEGER DEFAULT 1` |
| **i18n** | MVP 不做；name/description 直接用中文，需要时再加 i18n 字段 |

---

## 四、数据模型

### 4.1 `agent_configs` 表（Postgres + SQLite 双后端）

```sql
CREATE TABLE agent_configs (
  id                    SERIAL PRIMARY KEY,
  agent_id              TEXT NOT NULL UNIQUE,              -- 业务主键：'builtin-quick' / 'builtin-agent' / 'agent_xxx'
  tenant_id             INTEGER NOT NULL DEFAULT 1,
  name                  TEXT NOT NULL,                     -- 显示名："快速问答" / "商业资料助手"
  description           TEXT DEFAULT '',
  avatar                TEXT DEFAULT '',                   -- emoji 或 icon
  agent_mode            TEXT NOT NULL,                     -- 'quick' / 'agent'
  is_builtin            BOOLEAN NOT NULL DEFAULT FALSE,
  system_prompt         TEXT DEFAULT '',                   -- quick 模式主 prompt
  agent_system_prompt   TEXT DEFAULT '',                   -- agent 模式 prompt（可空，空则用默认 ReAct）
  model_id              TEXT DEFAULT '',                   -- 关联 chat_models.model_id；空=不绑定
  temperature           REAL DEFAULT 0.7,
  max_tokens            INTEGER DEFAULT 4096,
  enabled               BOOLEAN NOT NULL DEFAULT TRUE,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  deleted_at            TEXT DEFAULT NULL
);

CREATE INDEX idx_agent_configs_tenant_enabled
  ON agent_configs (tenant_id, enabled);
```

迁移脚本：`migrations/postgres/004_phase7_2_a_agent_configs.sql`

### 4.2 种子数据（init_db 时插入；已存在则跳过）

```python
# custom_app/db.py 末尾 init_db() 加：
if not conn.execute("SELECT 1 FROM agent_configs WHERE agent_id='builtin-quick'").fetchone():
    conn.execute("INSERT INTO agent_configs (...) VALUES ('builtin-quick', '快速问答', 'quick', TRUE, "
                 "<复制现有 yaml system_prompt>, '', ...)")
if not conn.execute("SELECT 1 FROM agent_configs WHERE agent_id='builtin-agent'").fetchone():
    conn.execute("INSERT INTO agent_configs (...) VALUES ('builtin-agent', '智能推理', 'agent', TRUE, "
                 "'', <ReAct system prompt>, ...)")
```

---

## 五、后端实现

### 5.1 目录结构

```
custom_app/
  repositories/
    agent_config_repository.py     # ⚠️ 已存在但用途不同（kb_agent_configs 工具配置）
                                   # 新建 chat_agent_repository.py 避免名字冲突
    chat_agent_repository.py       # 新：agent_configs 表 CRUD
  api/
    admin_agents.py                # 新：admin agent CRUD 路由
    chat.py                        # 改：body 接 agent_id；agent_id 用来取 system_prompt + (可选)覆盖 model_id
  services/
    prompt_renderer.py             # 新：placeholder 渲染（{{language}} / {{current_time}} / {{kb_name}}）
    rag_runner.py                  # 改：接 agent_config 参数；优先级 agent_config > chat_model > yaml
    agent_runner.py                # 改：同上
  scripts/
    apply_phase7_2_a_migration.py  # 新：跑迁移 + 种子数据
migrations/
  postgres/
    004_phase7_2_a_agent_configs.sql
```

### 5.2 ChatAgentRepository 接口

```python
class ChatAgentRepository:
    def create(*, agent_id, name, agent_mode, system_prompt, ...): ...
    def get(agent_id): ...
    def list_active(*, tenant_id=1, include_disabled=False): ...
    def update(agent_id, *, name?, system_prompt?, ...): ...
    def soft_delete(agent_id): ...
    def get_builtin_quick(): ...      # 返回 builtin-quick 行
    def get_builtin_agent(): ...      # 返回 builtin-agent 行
```

### 5.3 PromptRenderer

```python
# custom_app/services/prompt_renderer.py
def render_prompt(template: str, context: dict) -> str:
    """把 {{key}} 替换为 context[key]；未识别的 placeholder 保留原样。

    自动填充（context 未显式给时）：
        {{current_time}}  → ISO timestamp
        {{language}}      → 'Chinese (Simplified)'（MVP 硬编码）
    """
```

### 5.4 RagRunner / AgentRunner 接 agent_config

```python
class RagRunner:
    def __init__(self, ..., chat_model=None, agent_config=None):
        self._agent_config = agent_config

    def _apply_agent_config_override(self):
        if self._agent_config:
            sp = self._agent_config.get("system_prompt") or ""
            if sp:
                # 渲染 placeholder
                ctx = {"kb_name": self.kb_id, "kb_description": ...}
                self._chat_cfg["system_prompt"] = render_prompt(sp, ctx)
            t = self._agent_config.get("temperature")
            if t is not None:
                self._chat_cfg["temperature"] = float(t)
            ...
```

优先级：`agent_config.system_prompt` > `chat_model.extra.system_prompt`（如果有）> `yaml.system_prompt`。

### 5.5 chat.py body 字段

```json
POST /api/chat/stream
{
  "kb_id": "agv_demo",
  "question": "...",
  "agent_mode": "quick",         // 老字段保留兼容
  "agent_id": "builtin-quick",    // 新增；缺省时按 agent_mode 取 builtin
  "model_id": "model_xxx"
}
```

`agent_mode` 字段保留向后兼容（无 agent_id 时按 agent_mode 取 builtin-quick / builtin-agent）。

### 5.6 Admin API 路由

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET`    | `/api/admin/agents`               | 列表（含 disabled 不含已软删）|
| `GET`    | `/api/admin/agents/<agent_id>`    | 单条 |
| `POST`   | `/api/admin/agents`               | 创建 |
| `PUT`    | `/api/admin/agents/<agent_id>`    | 更新（builtin 也可改 prompt，但 agent_mode / is_builtin 不可改） |
| `DELETE` | `/api/admin/agents/<agent_id>`    | 软删；builtin 拒绝（返 400） |

### 5.7 Chat API 给前端 dropdown 用

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET`    | `/api/chat/agents`                | 返回 enabled 的 agent 列表，仅含 agent_id/name/agent_mode/avatar/description |

---

## 六、前端实现

### 6.1 Admin「Agent 管理」标签

仿模型管理风格，加在导航栏「模型管理」之后：

- 路由：`#/agents`
- 列表卡片：每张卡显示 `name` / `agent_mode 标签`（快速/推理）/ `is_builtin 徽章` / `model_id 关联`（无则显示"未绑定"）/ 启用开关 / 「编辑」/「删除」按钮
- 「新增」按钮：弹窗输入 name / description / agent_mode（select）/ system_prompt（textarea，带"可用变量"chip 提示）/ model_id（下拉，从 chat_models 取）

### 6.2 对话页 agent_mode dropdown 改造

当前 `index.html`：

```html
<select class="agent-select">
  <option value="quick">智能体：快速问答</option>
  <option value="agent">智能体：智能推理</option>
</select>
```

改为**动态填充**：

- 启动时调 `GET /api/chat/agents`
- 用 agent_configs 表里的所有 enabled agent 填充 select
- value = agent_id；text = `智能体：{name}`
- localStorage 记忆上次选择
- 发消息时 body 带 `agent_id`

**不引入新组件**——保持 select 形态，避免侵入式 UI 变动。

---

## 七、测试

### 7.1 后端单测

- `tests/test_phase7_2_a_chat_agent_repository.py`：CRUD + builtin 默认行
- `tests/test_phase7_2_a_admin_agents_api.py`：路由 + builtin 不可删
- `tests/test_phase7_2_a_prompt_renderer.py`：placeholder 替换 + 未识别保留
- `tests/test_phase7_2_a_runner_agent_config.py`：runner 优先级 agent_config > yaml

### 7.2 联调（追加到 MANUAL_TESTING.md "I" 段）

```
I. Phase 7.2.A Agent 配置（约 15 分钟）
  I.1 admin 进入 agent 管理；列表显示 2 个内置 agent
  I.2 编辑 builtin-quick：把 system_prompt 改成"用 markdown 嵌套粗体标题排版"；保存
  I.3 对话页发同样问题 → vLLM 输出排版变好
  I.4 创建第 3 个 agent："商业资料助手"，prompt 自定义；对话页 dropdown 出现该 agent
  I.5 删除 builtin-agent 应被拒绝 400
  I.6 切到自定义 agent 发问题 → 日志显示 agent_id=agent_xxx
```

---

## 八、工作量粗估

| 模块 | 粗估 |
|---|---|
| agent_configs 表 schema + 迁移 + 种子数据 | 0.2 天 |
| ChatAgentRepository + 单测 | 0.3 天 |
| PromptRenderer + 单测 | 0.1 天 |
| RagRunner / AgentRunner 接 agent_config + 优先级逻辑 | 0.3 天 |
| Admin API（CRUD） + 单测 | 0.3 天 |
| `/api/chat/agents` 端点 + chat.py body 接 agent_id | 0.1 天 |
| 前端 admin agent 管理 tab | 0.3 天 |
| 前端对话页 dropdown 动态填充 + agent_id 传递 | 0.1 天 |
| 联调 + 手册 I 段 | 0.3 天 |
| **合计** | **~2 人日**（仍属"7.2.A 最小价值版"范畴） |

---

## 九、相关文件清单

**新建**

- `migrations/postgres/004_phase7_2_a_agent_configs.sql`
- `custom_app/scripts/apply_phase7_2_a_migration.py`
- `custom_app/repositories/chat_agent_repository.py`
- `custom_app/api/admin_agents.py`
- `custom_app/services/prompt_renderer.py`
- 4 个 `tests/test_phase7_2_a_*.py`

**修改**

- `custom_app/db.py` / `custom_app/repositories/postgres_provider.py` — agent_configs 表
- `custom_app/repositories/__init__.py` — 导出 ChatAgentRepository
- `custom_app/api/__init__.py` / `custom_app/app.py` — 注册 admin_agents_bp
- `custom_app/api/chat.py` — body 接 agent_id；`_load_agent_config_row()`
- `custom_app/services/rag_runner.py` / `agent_runner.py` — 接 agent_config
- `custom_app/frontend/admin.html` / `admin.js` / `style.css` — agent 管理 tab
- `custom_app/frontend/index.html` / `main.js` — agent_select 动态填充
- `custom_app/frontend/services/kbApi.js` / `chatApi.js` — agent 相关 API
- `docs/MANUAL_TESTING.md` — I 段

---

## 十、验收标准

1. ✅ 编辑 builtin-quick 的 system_prompt → 重启 Flask 后下次对话生效
2. ✅ 创建第 3 个 agent，关联不同 model_id → 对话页 dropdown 出现，切换后用对应模型 + prompt
3. ✅ Builtin agent 可改 prompt 但不可删
4. ✅ Placeholder `{{language}}` `{{current_time}}` 自动填充
5. ✅ 无 agent_configs 行（向后兼容）时回退到 yaml 老路径
6. ✅ `pytest tests/test_phase7_2_a_*.py` 全过

---

## 十一、Phase 衔接

- **Phase 7.2.B**（推后）：完整 agent 配置（推荐问题、检索策略 per-agent 覆盖、VLM、IM 集成、prompt 模板库 + i18n）—— 工作量 3-5 人日
- 与 Phase 6 / 5 解耦：不动 KB / KG 任何代码

---

*Phase 7.2.A 让 system_prompt 从全局 yaml 移到 per-agent 数据库，解决"vLLM 排版差/AGV SOP prompt 一刀切"两个具体问题，为完整 agent 体系打底。*

---

## 十二、会话续接上下文（2026-05-16 压缩点）

> 之前的会话已被压缩；新会话从这里开始读即可。下面是接着做 Phase 7.2.A 需要知道的全部状态。

### 12.1 当前 git 状态

- 分支：`main`，已 commit 到 `09b8c39 feat(phase7): 多 provider 对话模型管理 + 真切换（7.0 + 7.1）`
- 工作树干净，未 push（用户要不要 push 自行决定）
- 上游：`awp/main`

### 12.2 Phase 7.0 + 7.1 已完成

详见 [PHASE_7_1_COMPLETION.md](./PHASE_7_1_COMPLETION.md)。一句话总结：

- `chat_models` 表（Postgres + SQLite）+ admin 模型管理 UI 已落地
- 4 个 provider 真切换：Gemini（走 `/v1beta/openai` 兼容端点）/ OpenAI / vLLM / Anthropic（专用 SDK）
- 对话页 chip 切换正确路由到对应 provider；Runner cache 按 `(kb_id, model_id)` 隔离
- 221 单测全过；用户已实际验证 4 个 provider 都能正常对话

**关键文件位置**：

| 模块 | 文件 |
|---|---|
| Provider 注册表 + base_url 解析 | [custom_app/services/providers/registry.py](../../custom_app/services/providers/registry.py) |
| OpenAI 兼容 adapter（OpenAI / vLLM / Gemini-compat） | [custom_app/services/providers/openai_compat_adapter.py](../../custom_app/services/providers/openai_compat_adapter.py) |
| Anthropic 专用 adapter | [custom_app/services/providers/anthropic_adapter.py](../../custom_app/services/providers/anthropic_adapter.py) |
| Adapter factory | [custom_app/services/chat_adapter_factory.py](../../custom_app/services/chat_adapter_factory.py) |
| ChatModelRepository | [custom_app/repositories/chat_model_repository.py](../../custom_app/repositories/chat_model_repository.py) |
| Admin models API | [custom_app/api/admin_models.py](../../custom_app/api/admin_models.py) |
| RagRunner 接 chat_model | [custom_app/services/rag_runner.py:121](../../custom_app/services/rag_runner.py#L121)（ctor）+ `_apply_chat_model_override` |
| AgentRunner 接 chat_model | [custom_app/services/agent_runner.py:69](../../custom_app/services/agent_runner.py#L69)（ctor）+ `init()` 里 `_adapter_canonical=True` 分支 |
| chat.py 路由 | [custom_app/api/chat.py](../../custom_app/api/chat.py) `_load_chat_model_row()` |

### 12.3 用户已 commit 的痛点（驱动 7.2.A）

测试 Phase 7.1 时用户发现：

> Claude 答案排版漂亮（粗体标题、嵌套 bullet、空行）；vLLM 答案内容一样但排版扁平。

**根因**：
1. **模型能力差异**（Claude 训练强势）
2. **system_prompt 一刀切**：`servers/generation/parameter.yaml` 里的 system_prompt 是 AGV SOP 专用、要求"严肃汇报"风格，对所有模型生效

用户提议参考 WeKnora 的 agent 管理界面（截图里有「基本信息 / 模型配置 / 知识库 / 工具配置 / 技能 / 检索策略 / 网络搜索 / 多模态 / IM 集成」9 个 tab，以及 `{{knowledge_bases}}` `{{web_search_status}}` `{{current_time}}` `{{language}}` 占位符）。我对 WeKnora 做了完整调研（见 §一），最终敲定**最小价值版 7.2.A** 方案。

### 12.4 7.2.A 第一步该做什么

按本文档 §五 顺序：

1. **写 Postgres 迁移 + SQLite schema**：`agent_configs` 表（见 §4.1）
2. **写迁移脚本**：`custom_app/scripts/apply_phase7_2_a_migration.py`（仿 [apply_phase7_migration.py](../../custom_app/scripts/apply_phase7_migration.py)）
3. **种子数据**：init_db 末尾插入 `builtin-quick` / `builtin-agent` 两行（见 §4.2）
4. **新建 `ChatAgentRepository`**：[custom_app/repositories/chat_agent_repository.py](../../custom_app/repositories/)，CRUD + 单测
5. **PromptRenderer**：[custom_app/services/prompt_renderer.py](../../custom_app/services/)，placeholder 替换
6. **RagRunner / AgentRunner 接 `agent_config`**：优先级 `agent_config > chat_model > yaml`
7. **chat.py body 接 `agent_id`**：缺省时按 `agent_mode` 取 builtin
8. **Admin API**：`/api/admin/agents` CRUD + 单测
9. **`/api/chat/agents`**：前端 dropdown 用
10. **前端**：admin 加「Agent 管理」tab + 对话页 `agent_select` 动态填充

工作量 ~2 人日。

### 12.5 注意事项

- **不要**新建 `agent_config_repository.py`——已存在但用途不同（kb_agent_configs 表，管 `kb_id` 关联的工具启用）。**用 `chat_agent_repository.py`**
- **不要**改 `servers/generation/parameter.yaml` 里的 system_prompt——保持作为兜底回退
- **AGV SOP system_prompt 内容**要复制到 builtin-quick 种子数据，否则破坏现有 AGV 知识库的回答风格
- **向后兼容**：无 agent_configs 行（init_db 未跑过）时 Runner 必须能回退到 yaml；老 .env 老 yaml 部署升级零配置仍可用
- **agent_mode 字段值**：用 `'quick'` / `'agent'`（与现有前端 dropdown 字符串一致），**不要**抄 WeKnora 的 `'quick-answer'` / `'smart-reasoning'`
- **chat_models 表 + agent_configs 表的关系**：`agent_configs.model_id` 引用 `chat_models.model_id`，但**不强外键**（chat_models 可能被软删）；查询时若 model_id 找不到对应 model 就退回 chip 选的 model_id

### 12.6 测试环境

- Conda env：`ultrarag`（**不**用 `.venv` / `uv`）
- 跑测试：`& "C:\Users\Peter\miniconda3\envs\ultrarag\python.exe" -m pytest tests/ -q --ignore=tests/test_chat_stream_profile.py`
- 当前 baseline：221 通过；已知遗留 fails（与 7.2.A 无关）：
  - `test_phase2_kb_api.py::TestChatRunnerThreadSafety::test_concurrent_kb_switch_no_race`
  - `test_phase2_kb_api.py::TestChatStreamSse::test_stream_passes_agent_mode_to_runner`
  - `test_rag_runner_agent_mode.py::test_*`（mock 风格不兼容 VectorStore 抽象）
  - `test_hotfix_kg_search_incoming.py::*`（串跑时测试间状态污染，单跑通过）

### 12.7 .env 当前

- `ULTRARAG_CHAT_BACKEND=gemini`（无 chat_models 行时的回退）
- `ULTRARAG_GEMINI_MODEL=gemini-3.1-pro-preview`
- `ULTRARAG_VECTOR_BACKEND=qdrant`
- `ULTRARAG_DB_BACKEND=postgres`
- `ULTRARAG_KG_BACKEND=neo4j`
- `ULTRARAG_POSTGRES_URI=postgresql://postgres:postgres123!%40%23@192.168.8.40:5432/awprag`

### 12.8 用户当前在 admin 配置过的 chat_models（参考）

- 一个 Gemini（`gemini-3.1-pro-preview`）
- 一个 Claude（`claude-opus-4-7`）—— Anthropic 新模型已弃用 temperature；7.1 已处理
- 一个 vLLM Qwen（`Qwen/Qwen3.6-27B-FP8` @ `http://192.168.8.44:8800/v1`）

### 12.9 启动新会话的暗号

新会话说 **"开始 7.2.A"** 即可。我会按 §五 + §12.4 执行，不再重新调研 WeKnora（已经在 [PHASE_7_1_COMPLETION.md §2.1](./PHASE_7_1_COMPLETION.md) 和本文档 §一-§三记录完整）。

---

## 十三、实施回顾 + 踩坑记录（2026-05-16 完工）

> 7.2.A 在 1 个会话内一次性完成（代码 + 单测 + 联调）。手工 I.1–I.6 全过。期间踩了 4 个坑，4 个都已修；后两个不是 7.2.A 引入的旧 bug，但用户验收时第一次暴露，记下来给 7.2.B 参考。

### 13.1 落地清单（最终版）

| 模块 | 状态 | 文件 |
|---|---|---|
| Postgres 迁移 | ✅ | [migrations/postgres/004_phase7_2_a_agent_configs.sql](../../migrations/postgres/004_phase7_2_a_agent_configs.sql) |
| 迁移脚本 + 种子 | ✅ | [custom_app/scripts/apply_phase7_2_a_migration.py](../../custom_app/scripts/apply_phase7_2_a_migration.py) |
| SQLite schema + init_db 种子 | ✅ | [custom_app/db.py](../../custom_app/db.py) `_seed_builtin_agents` |
| ChatAgentRepository | ✅ | [custom_app/repositories/chat_agent_repository.py](../../custom_app/repositories/chat_agent_repository.py) |
| PromptRenderer | ✅ | [custom_app/services/prompt_renderer.py](../../custom_app/services/prompt_renderer.py) |
| RagRunner / AgentRunner 接 agent_config | ✅ | [rag_runner.py `_apply_agent_config_override`](../../custom_app/services/rag_runner.py) / [agent_runner.py `_build_system_prompt`](../../custom_app/services/agent_runner.py) |
| chat.py body 接 agent_id + builtin fallback | ✅ | [custom_app/api/chat.py `_load_agent_config_row`](../../custom_app/api/chat.py) |
| Admin API CRUD | ✅ | [custom_app/api/admin_agents.py](../../custom_app/api/admin_agents.py) |
| `/api/chat/agents` | ✅ | [chat.py `get_chat_agents`](../../custom_app/api/chat.py) |
| Admin 「Agent 管理」tab | ✅ | [custom_app/frontend/admin.js `renderAgents` / `openAgentEditor`](../../custom_app/frontend/admin.js) |
| 对话页 dropdown 动态填充 + agent_id 入 payload | ✅ | [main.js `initAgentSelect`](../../custom_app/frontend/main.js) + [agentSelector.js `populateAgentSelect` / `getSelectedAgent`](../../custom_app/frontend/components/agentSelector.js) |
| MANUAL_TESTING §I | ✅ | [docs/MANUAL_TESTING.md](../MANUAL_TESTING.md) I.0–I.7 |
| 单测覆盖 | ✅ | 50 个新用例（repo 9 + prompt_renderer 14 + runner_agent_config 9 + admin_agents_api 16 + agent_tools_shape 2）|

### 13.2 验收路径（用户走过的）

1. ✅ I.0 Postgres 迁移 + 种子（幂等可重复跑）
2. ✅ I.1 admin Agent 管理 tab 出现 + 2 个 builtin 卡片
3. ✅ I.2 编辑 builtin-quick prompt → vLLM 排版变好
4. ✅ I.3 创建第 3 个 agent「商业资料助手」→ dropdown 出现
5. ✅ I.4 编辑 builtin-agent agent_system_prompt → ReAct 风格切换
6. ✅ I.5 builtin agent 不可删（API 400 + UI 隐藏按钮）
7. ✅ I.6 admin 不可改 builtin 的 agent_mode

### 13.3 踩坑记录

#### 坑 1：Max Tokens HTML5 step=64 拒收 4096（7.2.A 引入）

`<input type="number" min="1" step="64">` → 浏览器只接受 1, 65, 129, … 4033, 4097，把常见的 4096 / 8192 都拒掉。

**修**：[admin.js](../../custom_app/frontend/admin.js) `inputMaxTokens.step = '1'`。

**给 7.2.B 留的 lesson**：number input 写 step 之前先想清楚常见值是不是 1 + step·k；2 的幂常用值（4096 / 8192）跟 step=64 不兼容。

#### 坑 2：qa_rag.jinja 全文 dump（非 7.2.A 引入，但 7.2.A 暴露）

`prompt/agv_qa_rag.jinja` 写死 SOP-style user prompt（"Output exactly N sections, translate English to Chinese, …"）；[servers/retriever/parameter.yaml](../../servers/retriever/parameter.yaml) `final_top_k=0`（不截断召回）→ 召回多少 chunk 全文进 prompt。商业资料助手即使 system_prompt 写了"销售视角"，也被 user prompt 压住，看起来"全文输出"。

**临时缓解**：user 自己改 retriever yaml 把 `final_top_k` 改成 5 / 8；或在 agent system_prompt 末尾加"仅基于第 1-3 段回答"。

**根治放 7.2.B**（见 §11）：per-agent 覆盖 retriever（`recall_top_k` / `final_top_k`）+ user prompt 模板选择（SOP / 通用文档 / FAQ）。

#### 坑 3：自定义短 agent_system_prompt 不能省略工具说明（非 bug）

如果替换 agent_system_prompt 后省略了工具名（`knowledge_search` / `list_knowledge_chunks` / `final_answer`），LLM 无法学到工具用法，会输出畸形 tool_call，引发坑 4。

**修**：MANUAL_TESTING §I.4 给"安全的自定义模板"——必须列工具名 + 调用规则。

#### 坑 4：chat_stream() tools shape 漏判 canonical 分支（Phase 7.1 旧 bug）

[agent_runner.py:749](../../custom_app/services/agent_runner.py#L749) `chat_stream()` **无条件**调 `openai_tools_to_gemini()` 扁平化 tools schema → vLLM / OpenAI-compat / Anthropic 拿到 `{name, description, parameters}` 没有 `{type:function, function:{...}}` 外壳 → vLLM Pydantic 拒：`5 validation errors tools[i].function Field required`。

`init()` 同位置（line 185-192）就有正确分支按 `_adapter_canonical` 选 shape，**`chat_stream()` 漏了**。这是 Phase 7.1 引入 canonical adapter 时的疏忽，7.2.A 让用户首次实际触发：编辑了 builtin-agent prompt → 缓存失效 → 重建 Runner → 第一次走 vLLM agent 模式 → 触发。

**修**：[agent_runner.py:749-760](../../custom_app/services/agent_runner.py#L749) 加 `if self._adapter_canonical` 分支，回归测试 [tests/test_phase7_2_a_agent_tools_shape.py](../../tests/test_phase7_2_a_agent_tools_shape.py)（2 用例）。

**给 7.2.B 留的 lesson**：
- 同一份逻辑（"按 adapter 模式选 tools shape"）出现在 `init()` 和 `chat_stream()` 两处，**应抽函数**，避免分支漂移
- 7.1 加 canonical adapter 时单测只覆盖了 `init()` 路径，没覆盖 `chat_stream()`——回归套件该加一组"按 adapter 模式发请求 body 形状正确"的契约测试

### 13.4 测试矩阵（最终）

| 套件 | 用例数 | 状态 |
|---|---|---|
| `tests/test_phase7_2_a_*.py` | 50 | ✅ 全过（含 2 个 tools shape 回归） |
| Phase 7 全部（7.0 + 7.1 + 7.2.A） | 106 | ✅ 全过 |
| Sprint 1/4/5/7/8/9/10 + function_calling_closed_loop | 154 | ✅ 全过（agent_runner 无回归） |
| 前端 vitest 全套 | 162 | ✅ 全过 |
| 用户手工 I.1–I.6 联调 | 6/6 | ✅ 全过 |

### 13.5 已知遗留（沉到 7.2.B）

1. **user prompt 模板可选**（坑 2 根治）：per-agent 选 SOP / 通用文档 / FAQ 模板，不再全局写死 `agv_qa_rag.jinja`
2. **per-agent 检索策略**：`recall_top_k` / `final_top_k` / `enabled_tools` 落到 agent_configs 行
3. **agent 配置完整 9 tab**（对齐 WeKnora）：推荐问题、网络搜索、VLM、IM 集成、prompt 模板库、i18n
4. **抽 `tools_for_adapter(canonical, schemas)` 辅助函数**（坑 4 lesson）

---

*Phase 7.2.A 让 system_prompt 从全局 yaml 移到 per-agent 数据库，AGV SOP / 商业资料 / 智能推理 三类 agent 都能独立配置；联调中暴露并修了 Phase 7.1 留的一个 tools shape bug。下一站 7.2.B。*
