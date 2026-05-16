# Phase 7.2.A —— Agent 配置与 system_prompt 管理

> **状态**：计划已确认（2026-05-15），待开工
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
