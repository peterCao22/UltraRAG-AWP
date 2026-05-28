# Phase 12.1 —— Context Resolution（指代消解）

> **状态**：实施计划（2026-05-28）
> **前置**：[Phase 12 README §二](./README.md#二子阶段拆分) 方向锚点
> **工时估算**：1.5-2 周
> **退出条件**：50 条多轮场景人工标注，**改写正确率 ≥ 80%**，且单轮场景 Hit@5 不退化

---

## 一、阶段目标

让用户能用"它/这个/上一步/第 N 个/继续"这种**指代型**问题继续对话，系统能基于历史回答把指代消解成具体内容，再走 RAG。

**典型场景**（来自 Phase 12 README §二）：

```
T1 user:  AGV 启动前要做什么？
T1 ans:   1. 检查电池 2. 检查急停按钮 3. 检查导航传感器

T2 user:  第 2 个怎么操作？        ← 指代："第 2 个" = "检查急停按钮"
T2 解析:  改写 query → "急停按钮如何检查"
T2 ans:   走 RAG，返回急停按钮 SOP

T3 user:  那它需要多久检查一次？   ← 双重指代："它" = 急停按钮
T3 解析:  改写 query → "急停按钮多久检查一次"
```

---

## 二、当前现状（实施前）

### 2.1 已有能力

| 能力 | 位置 | 状态 |
|---|---|---|
| Session 历史落库 | `kb_session_messages` 表 | ✅ |
| AgentRunner 注入 history（最近 6 条） | `agent_runner.py:293` | ✅ 已生效 |
| `_rewrite_query`（单轮） | `rag_runner.py:1214` | ⚠️ **不看 history** |
| RagRunner.chat 路径接收 history | `rag_runner.py:chat_stream` | ❌ **完全没接** |
| 前端展示推理步骤 | `frontend/main.js` thought 事件 | ✅ |

### 2.2 问题定位

**RagRunner.chat / chat_stream 走 quick 模式时根本不知道 session 历史存在**：

- `api/chat.py:472` IRCoT 路径调 `runner.chat_ircot(question=question, ...)` 不传 history
- `api/chat.py:505` quick 路径调 `runner.chat_stream(question=question, ...)` 不传 history
- 只有 `agent_mode=agent` 的 AgentRunner 拿到 history

所以**快速问答 + 深度思考**两条路径对指代型 query 都答非所问；只有智能推理因为 LLM 看到 history 才偶尔答对。

---

## 三、方案设计

### 3.1 整体流水线

```
新 query → has_reference? → no  → 走原 RAG（不动）
                         → yes → rewrite_with_history → 改写后 query → 走原 RAG
                                      ↓
                              [confidence 检查]
                                      ↓
                              低 → fallback 到原 query
                              高 → 把改写过程通过 SSE 发给前端可见
```

### 3.2 三段实现

#### 3.2.1 指代检测（Reference Detection）

**输入**：当前 query + 最近 N 轮历史（user/assistant 交替）
**输出**：`{has_reference: bool, reference_phrases: [...], confidence: float}`

**实现路径 — 二选一**：

| 选项 | 优点 | 缺点 |
|---|---|---|
| **A. 规则 + 关键词**（推荐先用）| 0 token 成本；中文指代词集合很小 | 召回率有限 |
| B. LLM 判定 | 召回更全 | 每问 +1 LLM 调用 |

**A 方案关键词集**：
```python
REFERENCE_MARKERS = {
    # 代词
    "它", "他", "她", "它们", "这", "那", "这个", "那个", "这些", "那些", "其",
    # 序数 / 列表项
    "第一", "第二", "第三", "第 1", "第 2", "第 3", "第一个", "第二个", "第三个",
    # 步骤
    "上一步", "下一步", "上面", "下面", "这步", "那步", "STEP",
    # 续问
    "继续", "然后呢", "接着", "下文", "之后呢", "下一个",
}
```

判定：query 含任一关键词 + 历史非空 → 触发改写。

**B 留作 fallback**（A 判定为 no_reference 但 LLM 仍认为该改写时上）—— 本期**先不上 B**。

#### 3.2.2 改写（Rewrite with History）

**Prompt 模板**（中英文双语兜底）：

```jinja
你是技术问答助手。用户在与系统对话，最新一轮可能含指代（"它/第 2 个/继续"等）。
你的任务：把指代消解成具体内容，输出**改写后的检索 query**。

【对话历史】
{% for turn in history %}
{{turn.role}}: {{turn.content}}
{% endfor %}

【最新用户问题】
{{question}}

输出格式（严格 JSON，无其他文字）：
{"rewritten_query": "...", "confidence": 0.0-1.0, "resolved": [{"reference": "第 2 个", "meaning": "急停按钮"}]}

规则：
1. 改写后的 query 应是**独立、完整、可检索**的（不再含代词）
2. 若历史不足以消解指代，rewritten_query 直接复用原 query，confidence < 0.5
3. confidence 反映你对消解结果的把握，低于 0.7 视为不确定
4. 保留原 query 的领域术语
```

**模型选择**：**Claude Haiku 4.5**（用户 2026-05-28 确认）。
- 模型 ID: `claude-haiku-4-5-20251001`
- 每问预算 < $0.001（input ~$1/MTok × ~1k token + output ~$5/MTok × ~100 token ≈ $0.0015）
- 走项目已有的 AnthropicAdapter（无需新增 provider）
- Gemini-flash 留作 fallback：Anthropic 配额撞顶时降级

#### 3.2.3 接入点

**插入位置**：`rag_runner._prepare_chat_context` 开头，紧接 `_rewrite_query` 之前。

```python
# 新增方法
def _resolve_references(
    self,
    question: str,
    history: list[dict] | None,
) -> tuple[str, dict]:
    """返回 (改写后 query, meta dict)
    meta = {
        "applied": bool,
        "original_query": str,
        "rewritten_query": str,
        "confidence": float,
        "resolved": [{"reference": ..., "meaning": ...}],
        "ms": int,
        "skip_reason": str | None,  # "no_history" / "no_marker" / "low_confidence" / "error"
    }
    """
```

**调用方改动**：
- `api/chat.py:472, 505` 调 `runner.chat_ircot / chat_stream` 时**也传 history**（与 agent 路径对齐）
- `rag_runner.chat_stream / chat_ircot` 签名加 `history: Optional[List[Dict]] = None`
- 内部把 history 传给 `_prepare_chat_context`

---

## 四、SSE 事件 + 前端展示

### 4.1 新增 SSE 事件

```json
{"type": "reference_resolution",
 "original_query": "第 2 个怎么操作？",
 "rewritten_query": "急停按钮如何检查",
 "confidence": 0.92,
 "resolved": [{"reference": "第 2 个", "meaning": "急停按钮"}]}
```

在 `status` 事件之后、`thought / chunk` 之前发送。

### 4.2 前端展示

| 模式 | 展示 |
|---|---|
| 快速问答 | 在回答上方加灰色一行：**"系统理解为：急停按钮如何检查"** + 反馈按钮 👍 👎 |
| 智能推理 / 深度思考 | 同上，且在推理步骤里作为"第 0 轮：指代消解"展示 |

**关键决策**（Phase 12 README §六 备忘 2 已问）：**显示而非静默**，给用户修正机会。

### 4.3 用户修正路径

灰色提示行后跟一个"不是这意思？点这里修正"按钮：
- 点击后展开 input，用户输入正确含义
- 把修正后的 query 重新发起一次检索（前端复用现有 chat API）
- 同时把修正样本落 `audit_logs`（待 11.1.2 上线后）做数据集积累

---

## 五、评测体系

### 5.1 评测数据集

**目标**：50 条多轮场景，覆盖：

| 类型 | 示例 | 数量 |
|---|---|---|
| 单代词 | "它怎么处理" | 15 |
| 序数代词 | "第 2 个怎么操作" | 15 |
| 续问 | "继续 / 然后呢" | 10 |
| 双重指代 | "那它需要多久" | 5 |
| **诱饵**（不该改写） | "AGV 怎么启动"（无 history） | 5 |

**数据来源**：
1. 业务方手工写 30 条（IT 部门 + 车间）
2. 从 Phase 11.1.2 审计日志（**未上线 → 先用现有 session 数据**）筛 20 条真实多轮
3. 标注格式：`{turns: [...], expected_rewrite: "...", should_rewrite: bool}`

### 5.2 评测脚本

新增 `custom_app/scripts/eval_reference_resolution.py`：

| 指标 | 计算 |
|---|---|
| **精确率** | 改写正确 / 触发改写总数（避免过度改写）|
| **召回率** | 触发改写 / 应改写场景数 |
| **F1** | 调和均值 |
| **诱饵未触发率** | 不该改写的场景里确实没改写的比例 |

**判定标准**：人工 + 自动结合。
- 自动：改写 query 与 expected_rewrite 用 LLM 判等价（避免字面不同但语义相同被误判错）
- 抽样人工复核 20%

### 5.3 端到端评测

跑改写后的 ifs_docs / agv_demo 评测，对比 Phase 11.2 基线：
- **必须满足**：单轮场景 Hit@5 不退化 > 1pp（说明改写没误伤单轮）
- **应该满足**：多轮场景 Hit@5 ≥ 80%（说明改写真起作用）

---

## 六、配置与开关

### 6.1 env

```bash
ULTRARAG_REF_RESOLUTION_ENABLED=1                              # 0 完全关闭（紧急回滚）
ULTRARAG_REF_RESOLUTION_BACKEND=anthropic                      # anthropic | gemini（fallback）
ULTRARAG_REF_RESOLUTION_MODEL=claude-haiku-4-5-20251001
ULTRARAG_REF_RESOLUTION_FALLBACK_MODEL=gemini-2.0-flash        # Anthropic 配额撞顶时降级
ULTRARAG_REF_RESOLUTION_MIN_CONFIDENCE=0.7
ULTRARAG_REF_RESOLUTION_MAX_HISTORY=6                          # 最多看几轮历史
```

### 6.2 servers/retriever/parameter.yaml

```yaml
reference_resolution:
  enabled: true
  backend: anthropic         # anthropic | gemini
  model: claude-haiku-4-5-20251001
  fallback_model: gemini-2.0-flash
  min_confidence: 0.7
  max_history_turns: 6
  show_to_user: true         # false = 静默改写（不推荐）
```

### 6.3 Admin 配置

后期可以做 per-KB 开关 + per-tenant 改写模型选择，本期**全局开关足够**。

---

## 七、改动文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `custom_app/services/reference_resolver.py` | 🆕 新增 | 核心模块；规则检测 + LLM 改写 + confidence |
| `custom_app/services/rag_runner.py` | 改 | `_prepare_chat_context` 接 history；`chat_stream/chat_ircot` 签名加 history |
| `custom_app/api/chat.py` | 改 | 给 RagRunner 路径也传 history（已有 list_messages_for_agent 复用）|
| `custom_app/frontend/main.js` | 改 | 渲染 `reference_resolution` 事件；加修正按钮 |
| `custom_app/frontend/style.css` | 改 | 灰色提示行样式 |
| `prompt/reference_resolution.jinja` | 🆕 新增 | 改写 prompt 模板 |
| `tests/test_reference_resolver.py` | 🆕 新增 | 单测：规则检测 / LLM 解析 / confidence 阈值 / fallback |
| `data/eval/reference_resolution_dataset.jsonl` | 🆕 新增 | 50 条多轮评测集 |
| `custom_app/scripts/eval_reference_resolution.py` | 🆕 新增 | 评测脚本 |
| `docs/Phase12/PHASE_12_1_SUMMARY.md` | 🆕 新增（实施后）| 收官总结 |

---

## 八、实施步骤（Week-by-Week）

### Week 1：基础设施 + 评测集

| Day | 任务 |
|---|---|
| 1 | 写 `reference_resolver.py` 骨架 + 规则检测 + 单测 |
| 2 | 写 `prompt/reference_resolution.jinja` + LLM 改写实现 |
| 3 | 改 `rag_runner` + `api/chat.py` 串联 history |
| 4 | 业务方收集 30 条手工评测；自己写 20 条诱饵/边界用例 |
| 5 | 写 `eval_reference_resolution.py` + 跑初版指标 |

### Week 2：前端 + 调优 + 上线

| Day | 任务 |
|---|---|
| 1 | 前端展示 + 修正按钮 |
| 2 | 跑端到端评测对比 Phase 11.2 基线 |
| 3 | 根据评测结果调 confidence 阈值 + prompt 措辞 |
| 4 | 人工验证三种前端模式 |
| 5 | commit + push + 写 SUMMARY |

---

## 九、待讨论 / 风险点

### 9.1 待决策

| 项 | 选项 | 建议 |
|---|---|---|
| 改写后是否给用户看 | 显示 / 静默 | **显示**（README §六备忘 2，已定）|
| 触发条件 | 规则 / LLM / 混合 | 本期**规则 only**；LLM 兜底推 12.1.x |
| 评测集来源 | 手工 + 审计日志 | 手工 30 + 现有 session 抓 20 |
| confidence 阈值 | 0.5 / 0.7 / 0.85 | 默认 **0.7**，env 可调 |
| 历史窗口 | 4 / 6 / 8 轮 | 默认 **6 轮**（与 AgentRunner `_HISTORY_LIMIT` 对齐）|

### 9.2 关键风险

| 等级 | 风险 | 缓解 |
|---|---|---|
| 🔴 HIGH | 改写错了 = 答非所问，比不改写更糟 | confidence 阈值 + 用户可见展示 + 修正按钮 |
| 🟡 MED | LLM 改写每问 +500-800ms 延迟 | 用 flash 类小模型；规则检测先过滤 70%+ 单轮 query 跳过改写 |
| 🟡 MED | 历史污染：用户上一问问错了，沿着错的方向改写 | 改写时只看最近 N 轮；max_history=6 防上下文溢出 |
| 🟢 LOW | 中英文混合场景失效 | prompt 双语示例；评测集含 30% 英文 query |

### 9.3 已知不做的事

- **不做** B 方案（LLM-based 指代检测），规则覆盖 80% 场景已够，留做 12.1.x
- **不做** 跨 session 记忆（推 12.2 Session Memory）
- **不做** 主动反问（推 12.3 Clarification）
- **不做** Phase 11.1.2 审计日志依赖；评测数据先手工 + 现有 session

---

## 十、退出条件（验收）

实施完成后需满足：

- [ ] `eval_reference_resolution.py` 跑 50 条评测：**改写精确率 ≥ 80%**、**召回率 ≥ 75%**
- [ ] 诱饵场景未触发改写率 ≥ 95%（避免过度改写）
- [ ] ifs_docs / agv_demo 单轮 Hit@5 退化 ≤ 1pp
- [ ] 前端三种模式（快速 / 智能推理 / 深度思考）人工验证：指代场景全部能正确回答
- [ ] `ULTRARAG_REF_RESOLUTION_ENABLED=0` 紧急回滚路径验证
- [ ] commit + push + SUMMARY 落档

---

## 十一、与 12.2 / 12.3 的边界

| 当前阶段（12.1）做 | 推到 12.2 / 12.3 |
|---|---|
| 单轮指代消解 | 长会话摘要（>10 轮）|
| 改写 query | 主动反问 |
| 规则触发 | 跨 session 记忆 |
| 静态历史窗口 6 轮 | 动态摘要 + 关键实体提取 |

12.1 完成后，Phase 12 README §六备忘 2 已基本回答；12.2 启动时再回答备忘 3（"摘要何时触发"）。

---

> 计划制定完成。等用户确认后进入 Week 1 实施。
