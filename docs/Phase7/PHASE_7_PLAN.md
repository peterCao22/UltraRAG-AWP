# Phase 7 —— 对话模型可配置 + Admin 模型管理 + 前端模型切换

> **状态**：计划已确认（2026-05-15），待开工
> **前置**：Phase 6.0 完成；Agent 链路已支持 Gemini 3 `thoughtSignature`、function calling 闭环；项目已固定 Postgres（awprag）作为唯一关系型后端
> **并列**：[Phase 6.1 —— 入库进度](../Phase6/PHASE_6_1_PLAN.md) 独立验收，不纳入本阶段
> **参考实现**：`D:\Peter2025\myCursor\WeKnora`（Tencent WeKnora）— 后端 `internal/handler/model.go`、`internal/models/provider/`，前端 `frontend/src/views/settings/ModelSettings.vue`、`frontend/src/components/ModelEditorDialog.vue`、`frontend/src/components/Input-field.vue`

---

## 一、目标

1. **后端**支持在数据库中配置多个对话模型，按 **Provider 类型** 注册（4 类：Gemini / OpenAI / Anthropic / OpenAI 兼容）。
2. **Admin 页**新增「模型管理」标签：CRUD 模型、测试连接、设置默认模型。
3. **对话页**输入框右下角加模型 chip（参考 WeKnora `model-selector-trigger`），每条消息可独立切换；记忆到 `localStorage`。
4. **服务端**按 `(kb_id, model_id)` 缓存 `RagRunner` / `AgentRunner`，避免缓存串台。

---

## 二、非目标（推迟）

| 推迟项 | 推到哪 |
|--------|--------|
| 多租户 / 用户权限 | Phase 8（本阶段仅在表里**预留** `tenant_id` 列，全填 `1`） |
| Embedding / Rerank 模型也走 DB | 后续 Phase（保持 yaml 硬编码） |
| Ingest / KG 模型走 DB | 长期分离，KG 抽取继续读 `.env`（`ULTRARAG_GEMINI_MODEL` 或专用 env） |
| Provider 扩到 23 个（WeKnora 那样） | 按需加，先 4 个够用 |

---

## 三、设计决策（已确认）

| 议题 | 决策 |
|------|------|
| **数据库** | 仅 **Postgres**（awprag）建 `chat_models` 表；SQLite 后端不再扩展 |
| **API Key 存储** | **明文存 DB**（与 WeKnora 一致）；GET 返回时由 `_hide_sensitive(model)` 屏蔽为 `***` |
| **Provider MVP** | 4 个：`gemini` / `openai` / `anthropic` / `openai_compatible`（vLLM/Qwen 走这条） |
| **测试连接策略** | **真实发短 prompt**（例如 `"ping"`，max_tokens=1）走一次完整链路；准但消耗 1 次配额 |
| **依赖管理** | **conda `ultrarag` 环境 + pip**；`anthropic` 包通过 `pip install anthropic` 安装，写入 `pyproject.toml` 的核心 deps |
| **切换粒度** | **每条消息可独立切换**；服务端按 `(kb_id, model_id)` 缓存 Runner |
| **Ingest/KG 模型** | 与对话模型**解耦**，继续走 `.env`，不读 `chat_models` 表 |
| **前端框架** | 沿用项目现有 `marked + DOMPurify + 原生 JS`（不引入 Vue / TDesign） |
| **多租户预留** | 表中加 `tenant_id INTEGER NOT NULL DEFAULT 1`，加索引；中间件统一注入 `g.tenant_id = 1`（MVP 期 hardcode） |

---

## 四、数据模型

### 4.1 `chat_models` 表（Postgres / awprag）

```sql
CREATE TABLE chat_models (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     INTEGER NOT NULL DEFAULT 1,           -- Phase 8 多租户预留
    name          VARCHAR(255) NOT NULL,                -- 用户可读名称："Gemini 2.5 Pro"
    provider      VARCHAR(50)  NOT NULL,                -- gemini | openai | anthropic | openai_compatible
    model_name    VARCHAR(255) NOT NULL,                -- 实际 API 模型 ID："gemini-2.5-pro" / "gpt-4o" / "claude-haiku-4-5-20251001" / "Qwen2.5-7B-Instruct"
    base_url      VARCHAR(500),                         -- 可空，provider 有默认值（Gemini/OpenAI 官方端点）
    api_key       TEXT,                                 -- 明文存储；GET 返回屏蔽
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,       -- 同一 tenant 内唯一（约束在 service 层校验）
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    description   TEXT,
    extra_config  JSONB NOT NULL DEFAULT '{}',          -- temperature/max_tokens/timeout/supports_vision 等
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at    TIMESTAMPTZ
);

CREATE INDEX idx_chat_models_tenant_enabled ON chat_models(tenant_id, enabled) WHERE deleted_at IS NULL;
CREATE INDEX idx_chat_models_provider       ON chat_models(provider)            WHERE deleted_at IS NULL;
```

迁移脚本路径：`custom_app/migrations/postgres/00X_create_chat_models.sql`。

### 4.2 Provider 元信息（代码常量，不入库）

`custom_app/services/providers/registry.py`：

```python
PROVIDERS = {
    "gemini":             { "label": "Google Gemini",      "default_base_url": "https://generativelanguage.googleapis.com",  "requires_auth": True },
    "openai":             { "label": "OpenAI",              "default_base_url": "https://api.openai.com/v1",                  "requires_auth": True },
    "anthropic":          { "label": "Anthropic Claude",    "default_base_url": "https://api.anthropic.com",                  "requires_auth": True },
    "openai_compatible":  { "label": "OpenAI 兼容（vLLM/自部署）", "default_base_url": "",                                    "requires_auth": False },
}
```

---

## 五、后端实现

### 5.1 目录结构

```text
custom_app/
  api/
    chat.py                       # 改：body 接 model_id；新增 GET /api/chat/models
    admin_models.py               # 新：admin CRUD + 测试连接
  services/
    providers/
      __init__.py
      registry.py                 # 4 个 provider 元信息 + validate_config()
    chat_adapter_factory.py       # resolve_chat_adapter(model_id) -> LLMAdapter
    llm_adapter.py                # 已有 Gemini；本期补 OpenAIAdapter / AnthropicAdapter
  repositories/
    chat_model_repository.py      # 新：list/get/create/update/delete/set_default
  utils/
    ssrf_guard.py                 # 新：validate_url_for_ssrf()（参考 WeKnora secutils）
migrations/
  postgres/
    00X_create_chat_models.sql
```

### 5.2 API 路由草案

| 方法 | 路径 | 用途 | 备注 |
|------|------|------|------|
| `GET`    | `/api/chat/models`              | 对话页 chip 下拉用：仅返回 `enabled=true` 的模型，**不**返回 `api_key` / `base_url` | 前端每次打开输入框调一次 |
| `POST`   | `/api/chat/stream`              | body 新增可选 `model_id`；缺省用 `is_default=true` 那条 | 旧客户端兼容 |
| `GET`    | `/api/admin/models`             | 全量列表（含 disabled、含 deleted_at IS NULL 的）；api_key 字段返回 `***` | admin 页用 |
| `GET`    | `/api/admin/models/providers`   | 返回 4 个 provider 元信息（label / default_base_url / requires_auth） | 新增/编辑弹窗下拉用 |
| `POST`   | `/api/admin/models`             | 创建；body 包含 name/provider/model_name/base_url/api_key/extra_config | **SSRF 校验** base_url |
| `PUT`    | `/api/admin/models/<id>`        | 更新；api_key 为空表示不变 | **SSRF 校验** |
| `DELETE` | `/api/admin/models/<id>`        | 软删除（`deleted_at = NOW()`） | |
| `POST`   | `/api/admin/models/<id>/set-default` | 设为默认；同 tenant 其它清零 | |
| `POST`   | `/api/admin/models/<id>/test`   | 发一条 `"ping"` 短 prompt，返回 `{ok, latency_ms, error?}` | **真实消耗 1 次 token** |

### 5.3 适配器层

`custom_app/services/llm_adapter.py` 补充：

- **`OpenAIAdapter`**：基于 `openai` SDK（OpenAI 1.x），支持 streaming + tool calling
- **`AnthropicAdapter`**：基于 `anthropic` SDK，messages API + streaming + tool use；映射 `tools` schema 到 Anthropic 格式
- 现有 `GeminiLLMAdapter` 保持不变，构造时从 model record 取 `api_key` / `base_url`（不再只读 env）
- `openai_compatible` 复用 `OpenAIAdapter`，只是 `base_url` 由用户填

工厂：

```python
# chat_adapter_factory.py
def resolve_chat_adapter(model_id: str | None) -> LLMAdapter:
    model = ChatModelRepository.get_default() if model_id is None else ChatModelRepository.get(model_id)
    if model is None or not model.enabled:
        raise ChatModelNotFound(model_id)
    provider = model.provider
    if provider == "gemini":
        return GeminiLLMAdapter(api_key=model.api_key, base_url=model.base_url or DEFAULT_GEMINI, model=model.model_name, extra=model.extra_config)
    if provider in ("openai", "openai_compatible"):
        return OpenAIAdapter(api_key=model.api_key, base_url=model.base_url or DEFAULT_OPENAI, model=model.model_name, extra=model.extra_config)
    if provider == "anthropic":
        return AnthropicAdapter(api_key=model.api_key, base_url=model.base_url or DEFAULT_ANTHROPIC, model=model.model_name, extra=model.extra_config)
    raise UnknownProvider(provider)
```

### 5.4 Runner 缓存

`rag_runner.py` / `agent_runner.py`：

```python
# 原：_runners: dict[str, RagRunner] = {}              # key = kb_id
# 改：_runners: dict[tuple[str, str], RagRunner] = {}  # key = (kb_id, model_id)
```

`chat_stream` 处理逻辑：

```python
model_id = body.get("model_id") or ChatModelRepository.get_default().id
adapter  = resolve_chat_adapter(model_id)
runner   = _get_or_create_runner(kb_id, model_id, adapter)
```

### 5.5 SSRF 校验

`utils/ssrf_guard.py`：

- 拒绝 `127.0.0.0/8`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16` 等私网（除非 env `ULTRARAG_ALLOW_PRIVATE_BASE_URL=1`，本地局域网 vLLM 场景）
- 拒绝非 `http`/`https` scheme
- DNS 解析后再校验解析出的 IP（防 DNS rebinding）

> 当前 vLLM 部署在内网，用户可能填 `http://192.168.x.x:8000`，所以**默认放开**私网（与本项目实际部署一致），仅校验 scheme + 拒绝明显恶意 host（如 `metadata.google.internal`、`169.254.169.254`）。

---

## 六、前端实现

> 全部用 `marked + DOMPurify + 原生 JS`，不引入 Vue/TDesign。视觉风格参考 WeKnora（卡片、`#0052D9` 主色、圆角 8px、灰色描边）。

### 6.1 Admin 页「模型管理」标签

`custom_app/frontend/admin.html` + `admin.js` + `style.css`：

- 顶部 tab 新增「模型管理」（在「知识库」「Agent 工具」之外）
- 模型列表（卡片式）：每张卡显示 `name` / `provider 标签` / `model_name` / 默认标记 / 启用开关 / 「编辑」「删除」「设为默认」「测试」按钮
- 「新增模型」按钮 → 打开 `<dialog>` 弹窗：
  1. **Provider 下拉**（4 选 1）→ 切换时自动填充 `base_url` 默认值
  2. **模型显示名** 输入框（用户起的名字，如「Gemini 2.5 Pro - 主力」）
  3. **Model Name** 输入框（实际 API 模型 ID，placeholder 按 provider 给示例）
  4. **Base URL** 输入框（可空，有默认）
  5. **API Key** 输入框（type=password）
  6. **Temperature / Max Tokens / 描述** 三个可选字段
  7. **「测试连接」按钮** → 调 `POST /api/admin/models/<id>/test`（或创建前调一个无 id 版本）；显示 ✓/✗ + latency
  8. 保存 / 取消

### 6.2 对话页输入框模型 chip

`custom_app/frontend/index.html` + `chat.js` + `style.css`：

- 输入框右下角加 `<button class="model-chip">`：显示当前选中模型 `name` + 下拉箭头
- 点击 → 弹出 dropdown overlay（参考 WeKnora `model-selector-dropdown`）：
  - 列出 `/api/chat/models` 返回的所有启用模型
  - 每条显示 `name` + provider 标签（remote / local）
  - 底部「+ 新建模型」链接 → 跳 admin 页
- 选中后写入 `localStorage.ULTRARAG_SELECTED_MODEL_ID`，发送消息时 body 带 `model_id`
- 默认值：localStorage 有 → 用 localStorage；否则用后端返回的 `is_default=true` 那条

---

## 七、测试

### 7.1 后端单测

- `tests/test_chat_model_repository.py`：CRUD + set_default 互斥
- `tests/test_chat_adapter_factory.py`：4 个 provider 各返回对应 Adapter；未知 provider 报错；disabled 模型报错
- `tests/test_ssrf_guard.py`：恶意 host / 元数据 IP 拒绝
- `tests/test_admin_models_api.py`：GET 返回的 api_key 被屏蔽为 `***`；POST/PUT 通过 SSRF 校验
- `tests/test_phase7_runner_cache.py`：`_runners` 键变成 `(kb_id, model_id)`，两个 model_id 不共享实例

### 7.2 联调（手工 + 写进 MANUAL_TESTING.md F 段）

- 在 admin 创建 3 个模型：`Gemini 2.5 Pro` / `Claude Haiku 4.5` / `vLLM Qwen`
- 对话页 chip 切换 → 服务端日志 `chat_stream model_id=xxx` 与所选一致
- 测试连接：每个模型点「测试」都能拿到 `ok: true`
- 删除当前选中的模型 → 前端 chip 回退到 default

---

## 八、依赖变更

`pyproject.toml` 核心 deps 新增：

```toml
"openai>=1.50",        # 7A 内置（已有部分代码依赖，本期统一版本）
"anthropic>=0.40",     # 7A 新增
```

conda 环境安装：

```powershell
conda activate ultrarag
pip install anthropic "openai>=1.50"
```

> 不再使用 `uv sync` / `.venv`。

---

## 九、风险与备忘

| 风险 | 缓解 |
|------|------|
| **Anthropic API 在国内访问受限** | 在「测试连接」失败时给出明确提示「Anthropic 需要代理或境外网络」 |
| **Gemini 3 thoughtSignature 与 model_name 绑定** | 切换 `gemini-2.5-pro` ↔ `gemini-3-pro-preview` 时 cache 不可共用，已通过 `(kb_id, model_id)` 缓存键自然隔离 |
| **OpenAI 与 Anthropic tool schema 不兼容** | Adapter 层各自映射；Agent 工具定义保持中性 JSON Schema，由 Adapter 转格式 |
| **API Key 明文存 DB** | (1) Postgres 文件加密 / (2) admin 接口加 Bearer 鉴权（Phase 8 一起做） / (3) GET 永远屏蔽 |
| **测试连接消耗配额** | UI 上「测试」按钮加防抖 + 5s cooldown |

---

## 十、相关文件清单

**新建**
- `custom_app/api/admin_models.py`
- `custom_app/services/providers/__init__.py`
- `custom_app/services/providers/registry.py`
- `custom_app/services/chat_adapter_factory.py`
- `custom_app/repositories/chat_model_repository.py`
- `custom_app/utils/ssrf_guard.py`
- `migrations/postgres/00X_create_chat_models.sql`
- `tests/test_chat_model_repository.py`
- `tests/test_chat_adapter_factory.py`
- `tests/test_ssrf_guard.py`
- `tests/test_admin_models_api.py`
- `tests/test_phase7_runner_cache.py`

**修改**
- `custom_app/api/chat.py`（接 `model_id`、新增 `/api/chat/models`）
- `custom_app/services/llm_adapter.py`（新增 OpenAI/Anthropic Adapter）
- `custom_app/services/rag_runner.py`、`custom_app/services/agent_runner.py`（缓存键 + 接 model_id）
- `custom_app/frontend/admin.html` / `admin.js` / `style.css`（模型管理 tab）
- `custom_app/frontend/index.html` / `chat.js` / `style.css`（输入框 chip）
- `pyproject.toml`（加 anthropic / 锁 openai 版本）
- `docs/MANUAL_TESTING.md`（增加 F 段「Phase 7 模型管理」验收）
- `CLAUDE.md`（更新模型配置说明）

---

## 十一、工作量粗估

| 模块 | 粗估 |
|------|------|
| DB schema + Repository + 迁移 | 0.5 人日 |
| Provider 注册表 + 工厂 + OpenAIAdapter | 0.5 人日 |
| AnthropicAdapter（含 tool use 适配） | 1 人日 |
| Admin API（CRUD + test connection + SSRF） | 1 人日 |
| Admin 前端（卡片列表 + 编辑弹窗） | 1.5 人日 |
| 对话页 chip + 切换逻辑 | 0.5 人日 |
| Runner 缓存改造 + chat_stream 接入 | 0.5 人日 |
| 测试 + 联调 + 手册更新 | 1 人日 |
| **合计** | **约 6.5 人日** |

---

*Phase 7 完成后：用户可在 Admin 添加任意 Gemini/OpenAI/Anthropic/兼容模型，对话页每条消息自由切换；KG 抽取等离线流程不受影响。*
