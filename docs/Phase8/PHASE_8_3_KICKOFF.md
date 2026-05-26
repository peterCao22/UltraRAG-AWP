# Phase 8.3 Week 1 启动评估文档

> 整理时间：2026-05-26
> 状态：可行性评估完成 → 推荐路径已确定
> 上游 PLAN：[`PHASE_8_3_PLAN.md`](./PHASE_8_3_PLAN.md)

---

## 一、PLAN 门槛全部达成

[`PHASE_8_3_PLAN.md`](./PHASE_8_3_PLAN.md) §五.4 / §九 的重启条件：

| 条件 | 状态 |
|---|---|
| multi_step 标签样本 ≥15 条 | ✅ **21 条**（agv 20 + ifs 1） |
| Phase 8.2 评测显示召回是瓶颈 | ✅ multi_step Recall@5=0.48 vs MRR=0.96（召回少+排序准） |
| 业务出现跨文档真实问题 | ✅ failing 类样本里多条跨 section_1+section_2 |

→ **Phase 8.3 重启条件满足**。

---

## 二、UltraRAG IRCoT 直接借用的可行性评估

PLAN §四 假设"借用 UltraRAG = 0 改造，1 周验证收益"。**实测后这个假设不成立**。

### 2.1 UltraRAG IRCoT 实际代码（已读源码确认）

| 文件 | 内容 | 与 SOP 业务适配度 |
|---|---|---|
| [`examples/ircot.yaml`](../../examples/ircot.yaml) | pipeline 定义（loop times=2 + check_end + extract_ans） | ✅ 通用，无需动 |
| [`prompt/IRCoT.jinja`](../../prompt/IRCoT.jinja) | **全英文 + Wikipedia few-shot 例子** | 🔴 必须重写：业务是中文 SOP，Wikipedia 例子诱导 LLM 答错风格 |
| [`servers/router/src/router.py:ircot_check_end`](../../servers/router/src/router.py#L60) | `"so the answer is" in ans.lower()` 判终结 | 🔴 中文场景永不触发 → loop 跑满 times=2 → 浪费一轮 |
| [`servers/custom/src/custom.py:ircot_extract_ans`](../../servers/custom/src/custom.py#L149) | 正则 `r"so the answer is[\s:]*([^\n]*)"` 抽答案 | 🔴 中文不触发 → 抽不到答案，返回完整原文 → F1 失真 |
| [`servers/custom/src/custom.py:ircot_get_first_sent`](../../servers/custom/src/custom.py#L127) | `re.search(r"(.+?[。！？.!?])")` 取首句 | ✅ 中英都支持 |
| UltraRAG retriever | 默认 FAISS 索引文件 | 🟡 我们生产用 Qdrant；借用要重建一份 FAISS |
| UltraRAG generation | 默认 vLLM/OpenAI 接口 | 🟡 要改成 Gemini |

### 2.2 改造工时估算

| 改造点 | 工时 | 说明 |
|---|---|---|
| 改写 IRCoT.jinja 为中文 SOP 风格 + 替换 few-shot 例子 | 1-2 小时 | 用 SOP 文档的真实 chunk 写 2-3 个多跳例子 |
| 改 `ircot_check_end` 支持中文判终结 | 30 分钟 | 加 "答案是" / "所以" / "因此" 等中文 trigger |
| 改 `ircot_extract_ans` 抽中文答案 | 30 分钟 | 同上 |
| 把 agv_demo 的 chunks 转 UltraRAG 格式 + 建 FAISS | 1 小时 | UltraRAG 期望 corpus.jsonl + embedding.npy（custom_app 已有，只差 FAISS 文件） |
| 配 UltraRAG retriever / generation 指向 Gemini | 30 分钟 | 改 yaml + env |
| 评测集 → UltraRAG benchmark 格式 | 30 分钟 | 写转换脚本 |
| 跑 IRCoT pipeline 验证 + 调试 | 2-3 小时 | 必踩坑 |
| **合计** | **6-8 小时（1 天）** | |

**这违背了 PLAN §三 "借用 = 0 改造，开发期加速验证" 的初心**。改造 UltraRAG 4 处中文化点后，IRCoT 已经不算"借用"了。

### 2.3 替代方案：直接 custom_app 内自写 IRCoT loop

PLAN §五.2 本来就要做的事——**剥离 UltraRAG IRCoT 到 custom_app**。既然要剥离，**不如跳过借用阶段直接自写**。

**核心逻辑只需 ~100 行 Python**：

```python
# custom_app/services/strategies/ircot.py（新建）

def chat_ircot(rag_runner, query: str, *, max_loops: int = 2) -> dict:
    """IRCoT 多轮检索 + 推理链。"""
    # 第 1 轮：基础检索
    prep = rag_runner._prepare_chat_context(query, top_k=5)
    chunks_seen = set(prep["hit_ids"])
    thoughts: list[str] = []

    for loop_idx in range(max_loops):
        # 构造 prompt：原 query + 已检索 chunks + 已有思考链
        prompt = build_ircot_prompt(
            query=query,
            chunks=[rag_runner._rows[i] for i in chunks_seen],
            thoughts=thoughts,
        )
        thought = rag_runner._generate(prompt)
        thoughts.append(thought)

        # 中文 + 英文判终结
        if any(p in thought for p in ["答案是", "因此答案", "so the answer is"]):
            break

        # 抽思考首句作下一轮 query
        next_q = re.search(r"(.+?[。！？.!?])", thought)
        if not next_q:
            break

        # 第 2+ 轮：用思考首句再检索，新 chunks 加入
        prep2 = rag_runner._prepare_chat_context(next_q.group(1), top_k=3)
        chunks_seen.update(prep2["hit_ids"])

    # 最终回答：拼接所有 chunks + 全部思考链 → 让 LLM 综合
    final_prompt = build_ircot_final_prompt(query, chunks_seen, thoughts)
    final_answer = rag_runner._generate(final_prompt)
    return {
        "answer": extract_final_answer(final_answer),
        "thoughts": thoughts,
        "n_loops": len(thoughts),
        "chunks_seen": list(chunks_seen),
    }
```

**优点**：

| 维度 | 自写 IRCoT | 借用 UltraRAG |
|---|---|---|
| 工时 | **1 天**（含 prompt 设计 + 测试） | 1 天（改造 + 跑通） |
| 长期维护 | 在 custom_app 内，与 RagRunner 同栈调试 | 跨进程 / 跨仓库追踪 |
| 中文适配 | 从一开始就为中文 SOP 设计 | 改 4 处 + 维持英文兼容 |
| 直接复用现有 | 调 `self.search()` / `_generate()` 复用所有 BM25 / RRF / Reranker / context | 重建 FAISS + 替换 generation 配置 |
| 测试 | 用 [`tests/test_eval_runner.py`](../../tests/test_eval_runner.py) 的 stub 模式直接测 | 跨进程不易写单测 |
| 评测 | 直接接到 `eval_custom_app.py --with-generation` | 多一道 predictions.jsonl 转换 |

→ **推荐：跳过借用，直接自写**。

---

## 三、推荐执行路径

### Week 1（修订版）：直接自写 + 评测

| Day | 任务 | 工时 | 交付 |
|---|---|---|---|
| Day 1 上午 | 设计中文 SOP 风格 IRCoT prompt（2-3 个 few-shot 例子） | 2-3 小时 | `prompt/ircot_sop.jinja` |
| Day 1 下午 | `custom_app/services/strategies/ircot.py` 实现 + 单测 | 3-4 小时 | 代码 + `tests/test_ircot.py` |
| Day 2 上午 | 接入 `eval_custom_app.py --strategy ircot`，跑 multi_step 子集 | 2 小时 | 评测分数 |
| Day 2 下午 | 对比 baseline（hybrid+ctx）vs IRCoT 在 multi_step 子集上 | 2 小时 | `data/eval/phase8_3_ircot_comparison.md` |
| **小计** | | **1 天** | go/no-go 决策 |

### Week 1 go/no-go 决策

| 结果 | 行动 |
|---|---|
| multi_step Recall@5 提升 ≥10pp **且** 延迟 <2× | 进入 Week 2 全量接入 + 前端切换 |
| multi_step Recall@5 提升 5-10pp | 写 KICKOFF v2，评估是否上"深度推理"按钮 |
| 提升 <5pp | 停止，把 chat_ircot 留作可选 |
| 延迟 >3× 单跳 | 即使分数高也不默认开，做用户手动切换按钮 |

### Week 2-3（仅 Week 1 通过后启动）

按 [PHASE_8_3_PLAN.md §五](./PHASE_8_3_PLAN.md#五week-2-3剥离移植前提44决策为移植) 执行：

- 完整移植 + 前端 mode 切换（chat vs deep reasoning）
- 评测全量样本（不只 multi_step 子集）
- 文档更新 + 部署 checklist

---

## 四、预计风险

| 等级 | 风险 | 缓解 |
|---|---|---|
| 🔴 HIGH | IRCoT 对 SOP 场景不一定有效（论文是开放域多跳 QA） | Week 1 评测就是为此而设；不达 5pp 立即停 |
| 🔴 HIGH | 延迟 3-5× 影响用户体验 | 双轨：默认 chat，深度推理可选；前端明确标注耗时 |
| 🟡 MED | Gemini thoughtSignature 与 IRCoT 多轮 prompt 拼接冲突 | 测试时禁用 thoughtSignature；如需保留则在 prompt 末尾标记轮数 |
| 🟡 MED | Gemini 配额 2-3× 上涨 | 评测时跑小子集（20 条 multi_step）即可 |
| 🟢 LOW | RagRunner._prepare_chat_context 不支持"补 chunks_seen"语义 | 自写 ircot.py 内部维护 chunks_seen set，绕过 |

---

## 五、立即可启动的子任务（如果你点头）

按 Day 1 上午 → 下午顺序：

```
1. 设计 prompt/ircot_sop.jinja（含 2-3 个中文 SOP 多跳 few-shot 例子）
2. 实现 custom_app/services/strategies/__init__.py + ircot.py
3. 写 tests/test_ircot.py（stub 模式跑 3-5 个 case）
4. eval_custom_app.py 加 --strategy ircot 参数
5. 跑 multi_step 子集对比 baseline
6. 产出 phase8_3_ircot_comparison.md
```

---

## 六、决策选项

| 选 | 含义 |
|---|---|
| A | 启动自写 IRCoT（推荐路径）；按 Day 1 上午→下午顺序 |
| B | 按原 PLAN 借用 UltraRAG 跑（接受 1 天改造工时） |
| C | Phase 8.3 暂不启动，Phase 8 完全收尾，转 Phase 11.1 性能优化 |
| D | 先小试：手工写 1 条 multi_step prompt 在 Gemini 网页版跑，看效果再决定 |
