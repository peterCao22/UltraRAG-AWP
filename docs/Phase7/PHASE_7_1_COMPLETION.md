# Phase 7.1 —— 多 provider 真切换（完成报告）

> **状态**：✅ 已完成（2026-05-15）
> **前置**：[Phase 7.0](./PHASE_7_PLAN.md)（chat_models 表 + admin 模型管理 + chip 切换）
> **参考实现**：Tencent WeKnora（`D:\Peter2025\myCursor\WeKnora`）
> **下一步**：[Phase 7.2.A](./PHASE_7_2_A_PLAN.md) Agent 配置与 system_prompt 管理

---

## 一、问题背景

Phase 7.0 完成后，用户可以在 admin 注册 Gemini / OpenAI / Anthropic / OpenAI 兼容（vLLM）四类模型，并在对话页 chip 切换。但实际验证发现：

> 切换到 vLLM 模型时，Flask 日志仍然显示 `backend=gemini model=gemini-3.1-pro-preview` — chip 只是更换 Runner 缓存键，**底层 LLM 仍是 .env 配置的 Gemini**。

`model_id` 仅作为 cache key，没真正路由到对应 provider。Phase 7.1 的目标：**让 chip 选哪个模型，对话链路就真用哪个 LLM**。

---

## 二、设计选型（关键决策）

### 2.1 调研 WeKnora 多 provider 实现

通过阅读 [WeKnora chat.go / remote_api.go / chat_provider_spec.go / gemini.go]，确认它的策略：

**所有 provider 共用同一份 `RemoteAPIChat` 实现，全部走 OpenAI 兼容协议**。

- `internal/models/chat/chat.go`：内部 canonical schema 全部采用 OpenAI 标准（`tool_calls` / `tool_call_id` / `role: tool` / `function.arguments`）
- `internal/models/chat/remote_api.go`：唯一 HTTP 实现，调 `POST {base_url}/chat/completions`，用 `go-openai` SDK
- `internal/models/provider/gemini.go:13`：Gemini 走 Google 官方 OpenAI 兼容端点 `https://generativelanguage.googleapis.com/v1beta/openai`，**不**写原生 `:generateContent`
- WeKnora **没有 Anthropic provider**（Claude 通过 OpenRouter 代理）

### 2.2 取舍讨论

用户在评审时提出：

> 1. 需要接入 anthropic，而且不用 openrouter 转，使用专用的 AnthropicLLMAdapter
> 2. 对于 gemini 的支持，是否用专用 SDK 在维护和速度上会更好？

**对 timeout 根因的纠正**：用户之前的 Gemini 3 timeout（`('Connection aborted.', TimeoutError)`）是**服务端推理慢**导致的——大 prompt（history + tools schema）让 Gemini 服务端长时间生成。**这跟客户端选什么协议无关**，专用 SDK 不会更快。

最终方案（折中版）：

| Provider | 接入方式 | 理由 |
|---|---|---|
| **Gemini** | OpenAI 兼容端点 `/v1beta/openai`（同 WeKnora） | 物理路径一样、延迟一样；省一套 SDK 维护 |
| **OpenAI / vLLM / 兼容** | OpenAI 协议（原生支持） | 通用标准 |
| **Anthropic** | 专用 `anthropic` SDK（独立协议） | Anthropic 无 OpenAI 兼容端点；messages 格式完全不同 |

**代价**：Gemini 经兼容端点会丢失 `thoughtSignature`（multi-turn 思维链回传）。对用户实际场景影响小（用户从未使用此能力）。

---

## 三、实现摘要

### 3.1 统一 Protocol + canonical schema

**文件**：[custom_app/services/providers/llm_protocol.py](../../custom_app/services/providers/llm_protocol.py)

```python
@runtime_checkable
class LLMAdapter(Protocol):
    def call(messages, tools, system_prompt, temperature, max_tokens) -> CanonicalChatResponse: ...
    def stream(...) -> Iterator[CanonicalStreamEvent]: ...
    def model_name() -> str: ...
```

Canonical schema 采用 OpenAI 风格：`role=system/user/assistant/tool`、`tool_calls=[{id, type:"function", function:{name, arguments}}]`、`tool` 消息用 `tool_call_id` 关联。

### 3.2 OpenAICompatAdapter（同时服务 OpenAI / vLLM / Gemini-compat）

**文件**：[custom_app/services/providers/openai_compat_adapter.py](../../custom_app/services/providers/openai_compat_adapter.py)

- 用 `openai` SDK
- `_build_messages()` 智能规整 AgentRunner 简化格式（`{name, args}`）→ OpenAI 标准（`{id, type, function: {name, arguments(JSON str)}}`），自动补 `tool_call_id` 关联
- streaming：按 OpenAI SSE chunk 拼接，正确处理 tool_calls delta（index + arguments 分段拼接）

### 3.3 AnthropicAdapter（完整版）

**文件**：[custom_app/services/providers/anthropic_adapter.py](../../custom_app/services/providers/anthropic_adapter.py)

完整 messages / tools 双向转换：

- **入口**：
  - `role=system` 抽出来传 `system` 参数（Anthropic 不接受 messages 里的 system）
  - `role=tool` → `user` + content_blocks `tool_result`（tool_use_id 关联）
  - `assistant.tool_calls` → content_blocks `tool_use`
  - OpenAI 风格 tools schema (`{type, function: {name, parameters}}`) → Anthropic 风格 (`{name, description, input_schema}`)
- **出口**：response.content blocks → `text` + `tool_calls`
- **streaming**：监听 `content_block_start` / `content_block_delta` / `content_block_stop` / `message_delta` 事件
- **temperature 处理**：新模型（Opus 4.x / Sonnet 4.x）已弃用，调用方未明确传值时**不发送**该字段

### 3.4 Provider → base_url 路由

**文件**：[custom_app/services/providers/registry.py](../../custom_app/services/providers/registry.py)

- Gemini 默认 base_url 改为 `https://generativelanguage.googleapis.com/v1beta/openai`
- `effective_base_url(provider, user_base_url)` 函数：用户填了用用户的，否则用 provider 默认

### 3.5 Adapter Factory

**文件**：[custom_app/services/chat_adapter_factory.py](../../custom_app/services/chat_adapter_factory.py)

```python
def resolve_chat_adapter(model_row) -> LLMAdapter:
    provider = model_row["provider"]
    if provider in ("gemini", "openai", "openai_compatible"):
        return OpenAICompatAdapter(...)
    if provider == "anthropic":
        return AnthropicAdapter(...)
```

### 3.6 RagRunner / AgentRunner 接入

**RagRunner** ([custom_app/services/rag_runner.py](../../custom_app/services/rag_runner.py))：

- `__init__(chat_model: dict | None)` 新增参数
- `_apply_chat_model_override()`：从 row 取 provider/base_url/api_key/model_name 覆盖 yaml + env
- `_generate()` / `_generate_stream()`：有 `_llm_adapter` 时走 adapter，否则回退老 .env 路径（向后兼容）
- `_generate_via_adapter()` / `_generate_stream_via_adapter()`：新增

**AgentRunner** ([custom_app/services/agent_runner.py](../../custom_app/services/agent_runner.py))：

- `__init__(chat_model: dict | None)` 新增参数
- `init()` 里若 `_chat_model` 非空 → 用 adapter factory 构造 adapter，标记 `_adapter_canonical=True`
- `_llm_call()`：canonical 路径直接返回 canonical 数据，无需 `gemini_response_to_tool_calls` 等老路径函数
- 老路径（GeminiLLMAdapter）保留，无 chat_models 行时回退

### 3.7 chat.py 路由

**文件**：[custom_app/api/chat.py](../../custom_app/api/chat.py)

```python
def _load_chat_model_row(model_id):
    return ChatModelRepository().get(model_id)

def _get_runner(kb_id, model_id):
    r = RagRunner(kb_id=kb_id, chat_model=_load_chat_model_row(model_id))
    ...
```

---

## 四、测试

### 4.1 新增单测

**文件**：[tests/test_phase7_1_adapters.py](../../tests/test_phase7_1_adapters.py)

- `TestOpenAICompatCall` — 基本 call、system_prompt 前置、tool_calls 解析
- `TestOpenAICompatBuildMessages` — AgentRunner 简化 dict → OpenAI 标准转换
- `TestAnthropicConvertMessages` — system 抽取、tool role → user.tool_result、assistant.tool_calls → tool_use blocks、tool_call_id 自动推回
- `TestAnthropicConvertTools` — OpenAI schema → Anthropic input_schema
- `TestAnthropicCall` — 不传 temperature、tool_use 解析
- `TestAdapterFactory` — 4 provider 路由正确性

**结果**：18/18 通过 ✅

### 4.2 回归

| 测试套 | 结果 |
|---|---|
| Phase 7.1 新单测 | 18/18 |
| Phase 7.0 全部 | 38/38 |
| Phase 6.1 / 6.2 全部 | 41/41 |
| `test_sprint1_agent_sse_events.py` | 8/8（**额外修复了之前 4 个已知 fails**） |
| `test_sprint5_agent_runner.py` / `test_function_calling_closed_loop.py` 等 | 全过 |
| **总计** | **221/221** ✅ |

前端 `npm test`：155/155 通过。

### 4.3 副作用

修复了 Phase 7.0 §十二.6 列出的 4 个 `TestQuickModeNoReasoningEvents::*` 老 fails——因为我在 `_generate` / `_generate_stream` 用了 `getattr(self, "_llm_adapter", None)` 防御性访问，绕过 `__init__` 的测试也能跑通。

---

## 五、用户验证结果

按 [docs/MANUAL_TESTING.md §H'](../../docs/MANUAL_TESTING.md) 验证：

| 用例 | 结果 |
|---|---|
| H'.1 vLLM quick 模式真切换 | ✅ 日志确认 `base_url=http://192.168.8.40:8000/v1` |
| H'.2 Gemini 经 OpenAI 兼容端点 | ✅ 日志确认 `base_url=https://generativelanguage.googleapis.com/v1beta/openai` |
| H'.3 Anthropic 直连 | ✅ 日志确认 `base_url=https://api.anthropic.com`，不传 temperature |
| H'.4 跨 provider agent tool calling | ✅ Claude 最快、排版最好；vLLM 输出正确但排版扁平 |

---

## 六、用户反馈引出的下一步

用户截图对比 Claude 与 vLLM 同问题答案：内容一致，但 Claude 的 markdown 排版（嵌套粗体标题、分级 bullet）明显好于 vLLM。

**根因**：
1. Claude 训练数据特别强调结构化输出，模型本身的排版能力强
2. yaml 里的 system_prompt 是 AGV SOP 专用、要求"严肃汇报"风格，**对所有模型一刀切**，限制了排版自由度

**解决方向**：仿 WeKnora，做 **Agent 配置系统**——把 system_prompt 从全局 yaml 移到 per-agent 数据库行，每个 agent 可以有独立的人格和输出风格指令。

详见 [Phase 7.2.A 计划](./PHASE_7_2_A_PLAN.md)。

---

## 七、文件清单

**新建（4 文件）**

- `custom_app/services/providers/llm_protocol.py` — Protocol + canonical types
- `custom_app/services/providers/openai_compat_adapter.py` — 完整 OpenAI 兼容 adapter
- `custom_app/services/chat_adapter_factory.py` — 重写（Phase 7.0 的版本被替换）
- `tests/test_phase7_1_adapters.py` — 18 个单测

**修改（6 文件）**

- `custom_app/services/providers/anthropic_adapter.py` — 升级为完整版
- `custom_app/services/providers/registry.py` — Gemini base_url 改为 `/v1beta/openai`；加 `effective_base_url()`
- `custom_app/services/rag_runner.py` — `chat_model` 参数 + adapter 路径
- `custom_app/services/agent_runner.py` — `chat_model` 参数 + canonical 路径
- `custom_app/api/chat.py` — `_load_chat_model_row()` 注入 Runner
- `docs/MANUAL_TESTING.md` — H' 段 5 个验收用例

---

## 八、关键设计决策（备查）

1. **三套适配器，统一 canonical schema** —— 内部全用 OpenAI 风格 `tool_calls` / `tool_call_id`
2. **Gemini 走兼容端点** —— 与 WeKnora 一致；丢失 thoughtSignature 但可接受
3. **Anthropic 独立 SDK + 双向 messages 转换** —— 因 Anthropic 没有官方 OpenAI 兼容端点
4. **向后兼容** —— 无 chat_models 行时 Runner 回退到 .env 老路径（GeminiLLMAdapter），任何已部署实例升级后零配置仍能工作
5. **AgentRunner 简化格式兼容** —— `_build_messages` / `_convert_messages` 智能从 `{name, args}` 推回 OpenAI 标准 `tool_call_id`

---

*Phase 7.1 完成后：用户在 admin 注册的任何 Gemini/OpenAI/Anthropic/vLLM 模型，在对话页 chip 切换后真切换。下一步 Phase 7.2.A 解决 system_prompt 一刀切的问题。*
