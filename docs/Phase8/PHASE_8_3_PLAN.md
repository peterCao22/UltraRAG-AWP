# Phase 8.3 —— IRCoT 移植（借用验证 → 剥离移植）

> **状态**：草案待讨论（2026-05-16）
> **前置**：[Phase 8.2](./PHASE_8_2_PLAN.md) 完成且至少「半胜」（chunking 或 BM25 有一项显著有效）
> **借用**：✅ 第 1 周借 UltraRAG 验证；第 2-3 周剥离移植到 custom_app
> **参考**：[IRCoT 论文](https://arxiv.org/abs/2212.10509)、UltraRAG [examples/ircot.yaml](../../examples/ircot.yaml)

---

## 一、目标

1. **第 1 周**（借用验证）：用 UltraRAG 跑通 IRCoT，在小份 SOP 评测集上对比 Phase 8.2 基线
2. **第 2-3 周**（剥离移植）：**只在验证有效的前提下**，把 IRCoT 算法剥离到 custom_app，串入 `rag_runner`
3. **不达标则不上线**：Phase 8 至 8.2 收尾，IRCoT 代码不进生产

---

## 二、非目标（推迟）

| 推迟项 | 推到哪 |
|--------|--------|
| Search-o1 移植 | 视 IRCoT 效果决定；若 IRCoT 已显著提升，Search-o1 边际收益可能小 |
| R1-Searcher 移植 | 同上 |
| 自适应推理深度（动态判断 loop 次数） | Phase 9+ |
| 多 Agent 协作（IRCoT 多角色） | 不在范围 |

---

## 三、IRCoT 是什么

**Interleaving Retrieval with Chain-of-Thought**：把"先检索、后回答"改成"边推理边检索"。

### 3.1 单轮 RAG（当前 custom_app）

```
query → 检索 top-5 → LLM 生成答案
```

适合：一问一答、答案在单个 chunk 内

### 3.2 IRCoT 多轮

```
query → 检索 → LLM 生成第一句推理 → 用这句推理再检索 → 生成第二句 → ... → 综合答案
```

适合：多跳问题、需要分步骤推导

**SOP 场景的真实例子**：
- 用户问："AGV 充电桩故障代码 E03 怎么处理？"
- 单轮 RAG：检索"E03"，可能只命中"错误代码表"那个 chunk，答不全
- IRCoT：
  - 第 1 轮：检索"E03"→ 命中"E03 = 电池温度过高"
  - 第 2 轮：用"电池温度过高"再检索 → 命中"电池过热处理流程"
  - 综合：给出完整诊断 + 处理步骤

### 3.3 UltraRAG IRCoT pipeline（[examples/ircot.yaml](../../examples/ircot.yaml)）

```yaml
pipeline:
- benchmark.get_data
- retriever.retriever_init
- generation.generation_init
- loop:
    times: 2          # 默认循环 2 次
    steps:
    - retriever.retriever_search
    - prompt.ircot_next_prompt        # 拼推理链 prompt
    - generation.generate
    - branch:
        router:
        - router.ircot_check_end       # 判断是否已能给答案
        branches:
          incomplete:
          - custom.ircot_get_first_sent  # 提取下一步推理用的句子
          complete: []
- retriever.retriever_search
- prompt.ircot_next_prompt
- generation.generate
- custom.ircot_extract_ans            # 抽取最终答案
- evaluation.evaluate
```

核心组件：
- `prompt.ircot_next_prompt` —— prompt 模板（拼接历史推理）
- `router.ircot_check_end` —— 判断推理是否完成（~30 行）
- `custom.ircot_get_first_sent` —— 抽取推理首句作为下一轮 query（~50 行）
- `custom.ircot_extract_ans` —— 从推理链抽取最终答案（~80 行）

---

## 四、第 1 周：借用验证

### 4.1 准备工作

| 子任务 | 工时 | 说明 |
|--------|------|------|
| 把 ifs_docs 一小份 chunks（10-20 条）导入 UltraRAG 格式 | 0.5 天 | UltraRAG retriever 需要 `corpus.jsonl` + `embedding.npy` |
| 配置 UltraRAG 的 retriever + generation 指向 Gemini | 0.5 天 | 复用现有 `servers/generation/parameter.yaml` |
| 准备 10-20 条多跳评测样本（从 Phase 8.1 评测集里筛 `tags=["multi_step"]`） | 0.5 天 | 单轮答得了的题目不能验证 IRCoT 价值 |

### 4.2 跑 IRCoT

```bash
# UltraRAG 侧跑 IRCoT
ultrarag run examples/ircot.yaml --query_set data/eval/multi_step_subset.jsonl
```

### 4.3 对比基线

| 模式 | Recall@5 | F1 | 延迟 | Gemini 配额 / query |
|------|---------|------|------|-------------------|
| Phase 8.2 单轮 | (基线) | (基线) | ~1s | 1× |
| UltraRAG IRCoT (loop=2) | ? | ? | ~3s | 3× |
| UltraRAG IRCoT (loop=3) | ? | ? | ~5s | 4× |

### 4.4 决策点

| 结果 | 行动 |
|------|------|
| F1 提升 ≥0.05 且延迟 <2× 单轮 | 进入第 2-3 周剥离移植 |
| F1 提升 <0.05 | **停止**，IRCoT 不上线，Phase 8 至 8.2 收尾 |
| F1 提升够但延迟 >3× | 评估"chat 模式 vs 思考模式"双轨：前端加按钮，默认单轮，复杂问题手动切 IRCoT |

---

## 五、第 2-3 周：剥离移植（前提：4.4 决策为「移植」）

### 5.1 剥离清单

| UltraRAG 源 | 目标位置 | 估计行数 | 改造 |
|------------|---------|---------|------|
| `servers/custom/src/custom.py` 里的 `ircot_*` 函数 | `custom_app/services/strategies/ircot.py` | ~300 | 去 `@app.tool` 装饰器；改 `retriever_search` 调用为 `rag_runner.search` |
| `servers/router/src/router.py` 里的 `ircot_check_end` | `custom_app/services/strategies/ircot_router.py` | ~80 | 去装饰器；保留判定逻辑 |
| `prompt/ircot_*.jinja` | `custom_app/prompts/ircot/` | ~5 个文件 | 复制；用 Jinja2 渲染 |

### 5.2 接入 rag_runner

```python
# custom_app/services/rag_runner.py
def chat_ircot(self, query: str, *, max_loops: int = 2) -> str:
    """IRCoT 模式：多轮检索 + 推理链。"""
    from custom_app.services.strategies.ircot import (
        build_initial_prompt, extract_first_sent,
        check_end, extract_final_answer,
    )

    chunks = self.search(query, top_k=5)
    reasoning_chain = []

    for loop_idx in range(max_loops):
        prompt = build_initial_prompt(query, chunks, reasoning_chain)
        thought = self._llm.generate(prompt)
        reasoning_chain.append(thought)

        if check_end(thought):
            break

        next_query = extract_first_sent(thought)
        chunks.extend(self.search(next_query, top_k=3))

    final_prompt = build_initial_prompt(query, chunks, reasoning_chain, final=True)
    raw = self._llm.generate(final_prompt)
    return extract_final_answer(raw)
```

### 5.3 前端切换（可选）

如果延迟可接受（<3s），直接默认开。否则前端加「深度思考」按钮，参考 Phase 7 模型 chip 设计：

```html
<select name="mode">
  <option value="chat">Chat (1× 速度)</option>
  <option value="ircot">Deep Reasoning (3× 速度, 多跳问题更准)</option>
</select>
```

后端按 mode 路由到 `chat()` 或 `chat_ircot()`。

### 5.4 改造点总览

| 文件 | 改动 |
|------|------|
| `custom_app/services/strategies/ircot.py` | 新建（剥离自 UltraRAG） |
| `custom_app/services/strategies/ircot_router.py` | 新建（剥离自 UltraRAG） |
| `custom_app/prompts/ircot/*.jinja` | 新建（复制自 UltraRAG） |
| [`rag_runner.py`](../../custom_app/services/rag_runner.py) | 加 `chat_ircot` 方法 |
| [`api/chat.py`](../../custom_app/api/chat.py) | `chat_stream` 加 `mode` 参数路由 |
| frontend `index.html` | （可选）加深度推理按钮 |

---

## 六、任务拆分（总 3 周）

### Week 1：借用验证

| 子任务 | 工时 | 验收 |
|--------|------|------|
| 准备 UltraRAG 端 corpus（导入 ifs_docs 小份） | 0.5 天 | `ultrarag run examples/agv_index_only.yaml` 跑通 |
| 跑 IRCoT pipeline | 0.5 天 | 输出 predictions.jsonl |
| 用 Phase 8.1 评测脚本算分 | 0.5 天 | 对比矩阵：单轮 vs IRCoT (loop=2/3) |
| 决策会议（go/no-go） | 0.5 天 | 写决策记录 `phase8_3_decision.md` |
| **小计** | **2 天**（剩余 3 天做准备和调参） | |

### Week 2：剥离

| 子任务 | 工时 | 验收 |
|--------|------|------|
| 剥离 `custom.py` IRCoT 函数 + pytest | 1.5 天 | 5 个核心函数全部覆盖单测 |
| 剥离 `router.py` IRCoT 判定 + pytest | 0.5 天 | 边界 case 覆盖 |
| 复制 prompt 模板到 `custom_app/prompts/ircot/` | 0.5 天 | Jinja2 渲染单测 |
| 接入 `rag_runner.chat_ircot()` | 1 天 | 端到端调用 5 条 query 不报错 |
| 集成 chat.py API + mode 参数 | 0.5 天 | curl 测试 `/api/chat?mode=ircot` |

### Week 3：调优 + 评测 + （可选）前端

| 子任务 | 工时 | 验收 |
|--------|------|------|
| 跑全量评测集（含单跳 + 多跳） | 1 天 | 输出 phase8_3_final_report.md |
| 调 prompt / max_loops 参数 | 1-2 天 | 评测分数稳定可复现 |
| （可选）前端加深度推理按钮 | 1 天 | 用户能切换模式 |
| 文档 + 验收 | 0.5 天 | README 段落 + 部署 checklist |

---

## 七、关键风险

| 等级 | 风险 | 缓解 |
|------|------|------|
| 🔴 HIGH | IRCoT 对 SOP 场景不一定有效（论文场景是开放域多跳 QA） | Week 1 借用验证就是为此而设；不达标立即停 |
| 🔴 HIGH | 延迟 3-5× 影响用户体验 | 双轨：默认 chat 模式，深度推理可选；前端明确标注耗时 |
| 🟡 MED | UltraRAG `ircot_extract_ans` 抽取逻辑依赖特定 prompt 输出格式 | 剥离时同步移植 prompt 模板，不要混搭 |
| 🟡 MED | Gemini thoughtSignature（Phase 7）和 IRCoT 多轮 prompt 拼接冲突 | 测试：IRCoT 模式下 thoughtSignature 是否需要禁用 |
| 🟡 MED | Gemini 配额 3-4× 上涨 | 配额监控告警；可选切到 Flash 模型降本 |
| 🟢 LOW | 剥离后函数命名冲突（UltraRAG 用 snake_case，custom_app 用 PEP8） | 全部按 custom_app 风格统一 |

---

## 八、待讨论问题

1. **是否真的需要 IRCoT**：用户实际问的多跳问题占比多少？如果 <20%，直接做"双轨"意义不大；建议先从 chat 历史抽样统计
2. **max_loops 设几次**：UltraRAG 默认 2，论文常用 2-4。SOP 场景多数 1-2 步足够，建议默认 2，evaluation 时扫描 1/2/3
3. **失败回退**：IRCoT 中途 LLM 报错，是降级到单轮 RAG 还是直接报错？建议**降级 + 错误标记**
4. **是否做 Search-o1 / R1-Searcher**：放在 Phase 8.3.x 增量；优先看 IRCoT 收益再决定
5. **Agent 模式（Phase 7 ReAct loop）和 IRCoT 的边界**：Agent 已经能多轮调 tool，IRCoT 在 Agent 链路里是冗余还是叠加？需要小 PoC 比对

---

## 九、退出条件

Week 1 验证后：

| 结局 | 条件 | 行动 |
|------|------|------|
| 🟢 **移植** | F1 提升 ≥0.05，延迟可接受 | 进入 Week 2-3 剥离移植 |
| 🟡 **观望** | F1 持平但端到端答案质量人工评估更好 | 写入 `phase8_3_decision.md` 暂搁置，转 Phase 9 再议 |
| 🔴 **放弃** | F1 持平或下降 | Phase 8 至 8.2 收尾，IRCoT 不上线 |

Week 3 之后（如果走移植路线）：

- [ ] `custom_app/services/strategies/ircot.py` + `ircot_router.py` 全部测试通过
- [ ] `custom_app/prompts/ircot/` 模板齐全
- [ ] `rag_runner.chat_ircot()` 端到端跑通 + 评测分数稳定
- [ ] `api/chat.py` 支持 mode 路由
- [ ] 0 行 UltraRAG import（grep 确认）
- [ ] 部署文档：如何切换 chat/ircot 模式

---

> 本子阶段**强烈依赖 Week 1 决策点**。讨论时先达成"什么样的指标算成功"的共识。
