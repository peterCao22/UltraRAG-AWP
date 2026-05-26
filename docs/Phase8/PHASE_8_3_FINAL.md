# Phase 8.3 IRCoT 收尾总结

> 总结时间：2026-05-26
> 状态：✅ 完成（Week 1 验证 + Week 2 接入生产链路；Week 3 调优放未来增量）
> 跨 commit：[`7ae9f27`](kickoff) → [`8fc34d5`](week 1) → [`d896cd3`](week 2)
> 上游：[`PHASE_8_3_PLAN.md`](./PHASE_8_3_PLAN.md) / [`PHASE_8_3_KICKOFF.md`](./PHASE_8_3_KICKOFF.md)

---

## 一、最终交付物

### 1.1 代码 / 配置（生产可用）

| 路径 | 内容 |
|---|---|
| [`prompt/ircot_sop.jinja`](../../prompt/ircot_sop.jinja) | 中文 SOP 风格 IRCoT prompt，含 3 个 few-shot 例子（单跳 / 跨文档 / 跨章节） |
| [`custom_app/services/strategies/__init__.py`](../../custom_app/services/strategies/__init__.py) | strategies 包入口 |
| [`custom_app/services/strategies/ircot.py`](../../custom_app/services/strategies/ircot.py) | `chat_ircot()` 多轮检索 + 推理链（~180 行） |
| [`custom_app/services/rag_runner.py`](../../custom_app/services/rag_runner.py) | 加 `chat_ircot()` 方法（与 chat / chat_stream 同层） |
| [`custom_app/api/chat.py`](../../custom_app/api/chat.py) | `/api/chat` 和 `/api/chat/stream` 支持 `mode=deep_reasoning` + `ircot_max_loops` |
| [`custom_app/frontend/index.html`](../../custom_app/frontend/index.html) | 工具栏加 `[data-role="deep-reasoning-toggle"]` checkbox |
| [`custom_app/frontend/style.css`](../../custom_app/frontend/style.css) | `.deep-toggle` 样式 |
| [`custom_app/frontend/main.js`](../../custom_app/frontend/main.js) | 读 toggle → 传 `mode` 给 sendChatMessage |
| [`custom_app/frontend/services/chatApi.js`](../../custom_app/frontend/services/chatApi.js) | sendChatMessage 接收 `mode` / `ircotMaxLoops` |
| [`custom_app/services/eval/runner.py`](../../custom_app/services/eval/runner.py) | EvalRunner 加 `strategy` / `ircot_max_loops` / `tag_filter` |
| [`custom_app/scripts/eval_custom_app.py`](../../custom_app/scripts/eval_custom_app.py) | CLI 加 `--strategy ircot` / `--ircot-max-loops` / `--tag-filter` |

### 1.2 评测结果

| 路径 | 内容 |
|---|---|
| [`data/eval/phase8_3/agv_demo__quick_multistep.json`](../../data/eval/phase8_3/agv_demo__quick_multistep.json) | Quick 在 multi_step 子集 (n=20) 的基线 |
| [`data/eval/phase8_3/agv_demo__ircot_loops2_multistep.json`](../../data/eval/phase8_3/agv_demo__ircot_loops2_multistep.json) | IRCoT loops=2 在 multi_step 子集 |
| [`data/eval/phase8_3/agv_demo__ircot_loops2_full.json`](../../data/eval/phase8_3/agv_demo__ircot_loops2_full.json) | IRCoT 全量 (n=130) |
| [`data/eval/phase8_3_ircot_comparison.md`](../../data/eval/phase8_3_ircot_comparison.md) | 完整对比报告（Week 1 子集 + Week 2 全量 + 双轨决策） |

### 1.3 测试

| 路径 | case 数 | 覆盖 |
|---|---|---|
| [`tests/test_ircot.py`](../../tests/test_ircot.py) | 26 | 纯函数（_has_end_marker / _extract_final_answer / _first_sentence）+ stub 集成（单跳 / 多跳 / max_loops 上限 / 边界） |

---

## 二、核心数据

### 2.1 multi_step 子集 (n=20)

| 指标 | Quick | IRCoT loops=2 | Δ |
|---|---|---|---|
| Recall@5 | 0.4833 | 0.5000 | +1.67pp |
| **Recall@10** | 0.4833 | **0.6917** | **+20.83pp** 🟢 |
| MRR | 0.9625 | 0.9625 | 0 |
| Hit@1 | 0.9500 | 0.9500 | 0 |
| **F1** | 0.0210 | **0.4246** | **+0.4036** 🟢🟢🟢 |
| **failures** | 19/20 | **7/20** | -60pp |
| 单次延迟 | ~12s | ~24s | ~2× |

### 2.2 全量 (n=130)

| 指标 | Quick baseline | IRCoT loops=2 | Δ |
|---|---|---|---|
| Recall@1 | 0.6436 | 0.6346 | -0.90pp |
| Recall@5 | 0.7654 | 0.7641 | -0.13pp |
| **Recall@10** | 0.7885 | **0.8269** | **+3.84pp** |
| MRR | 0.7925 | 0.8023 | +0.98pp |
| Hit@5 | 0.8615 | 0.8692 | +0.77pp |

**per-tag 关键**：

- `passing` (n=49): 完全持平 → IRCoT 不破坏简单问答
- `multi_step` / `failing` (n=20 各): R@10 +10pp → 多跳问题真受益
- 其他 tag: R@5 略降 / R@10 略升 → 对单跳样本 IRCoT 是浪费

---

## 三、PLAN §四.4 退出条件最终判定

| 门槛 | 判定 |
|---|---|
| F1 提升 ≥0.05 | **+0.4036 远超** 🟢🟢 |
| 延迟 <2× 单跳 | **1.6-2.4× 边缘** 🟡 |

**结论**：满足"进入 Week 2-3 全量移植"条件 → 已落地，**走双轨**。

---

## 四、生产配置（已部署）

### 4.1 默认行为不变

- `parameter.yaml retrieval.mode: hybrid` + chunks.jsonl 含 context（Phase 8.2 决策）
- 前端"深度思考"开关默认未勾
- 用户按原方式问答 → quick mode（hybrid + context + reranker）→ ~12s

### 4.2 用户主动启用 IRCoT

打开 http://localhost:8080 后：

1. 工具栏勾选"**深度思考**"
2. 提问跨文档 / 多步骤问题
3. 服务端走 `chat_ircot(max_loops=2)` 多轮检索
4. 前端按 SSE 事件顺序显示：
   - `status: 深度思考模式...`
   - `thought: 【第 1 轮思考】...` （每轮一条）
   - `chunk: 最终答案`
   - `done`

预计 ~24s 完成。

### 4.3 通过 API 直接调用

```bash
curl -X POST http://localhost:8080/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "agv_demo",
    "question": "换完电池后报 Master Link Down 怎么办？",
    "mode": "deep_reasoning",
    "ircot_max_loops": 2
  }'
```

---

## 五、未覆盖 / 留作未来增量的事

### 5.1 Week 3 原计划（已合并到未来增量池）

| 任务 | 状态 | 备注 |
|---|---|---|
| max_loops 扫描（1/2/3/4） | 未做 | 默认 2 已经把 F1 +0.40，调优收益有限 |
| Prompt 微调（基于 7 个剩余 failure） | 未做 | 这 7 个 failure 多是问题本身就模糊（如"前一步呢"上下文丢失），prompt 调优收益不确定 |
| 流式输出（每轮思考逐 token 推送） | 未做 | 现在每轮思考拿到完整字符串后才 yield；要做真流式需重构 `_generate` 接口 |
| MANUAL_TESTING.md 加 IRCoT 段 | 未做 | 等积累更多生产使用经验后再写 |

### 5.2 待业务侧收集运营数据

| 信号 | 收集方式 |
|---|---|
| 用户实际触发"深度思考"的比例 | 加日志：`logger.info("chat mode=%s kb=%s", mode, kb_id)` |
| 深度思考的真实延迟分布 | 已有 `meta.total_ms` 落库；可后台统计 |
| 用户对深度思考答案的满意度 | 前端加点赞 / 反馈按钮（Phase 11.1） |

---

## 六、Phase 8 全程回顾

### 6.1 子阶段一览

| Phase | 工程交付 | 评测信号 | 上线状态 |
|---|---|---|---|
| 8.0 | 兜底滑窗切分 | n/a | ✅ 上线 |
| 8.1 | 评测体系 + 评测集 | baseline 0.6753 r@5 | ✅ 上线 |
| 8.2 | Contextual + BM25 + RRF | v1 + 2.59pp r@5 / v2 + 4.75pp r@5 | ✅ 上线（v2 复盘后回 hybrid） |
| **8.3** | IRCoT + 双轨开关 | multi_step F1 +0.40 / R@10 +20.83pp | ✅ 上线（默认关，用户主动切） |

### 6.2 评测集生命周期

| 节点 | n | 主要来源 | 反映场景 |
|---|---|---|---|
| 05-15 初版 | 58 | 80% chunk 反推 | 字面对齐 → 信号失真 |
| 05-21 v2 扩充 | 130 | +72 真实 session | 用户口语化 / 中英混合 → 真实信号 |
| **05-21 multi_step** | 20 | failing 类样本派生 | IRCoT 攻击目标 |

### 6.3 关键技术发现

1. **chunks.jsonl 不是性能瓶颈**（<1ms） — 慢的是 Gemini API 网络 + LLM 生成（Phase 11.1 优化方向）
2. **评测集质量决定算法判定**：v1（字面对齐）判定 BM25 无效；v2（真实 session）BM25 微正向、context 显著
3. **Contextual chunking** 召回扩大但 Hit@1 微降（trade-off 接受）
4. **IRCoT** 在多跳场景 F1 +0.40，但**默认开会伤简单问答**（必须双轨）

### 6.4 文档体系

```
docs/Phase8/
├── README.md                          总入口
├── PHASE_8_0_PLAN.md                  滑窗切分
├── PHASE_8_1_PLAN.md                  评测体系
├── PHASE_8_2_PLAN.md                  Contextual + BM25
├── PHASE_8_3_PLAN.md                  IRCoT（原计划）
├── PHASE_8_3_KICKOFF.md               8.3 启动评估 + 决策不借 UltraRAG
├── PHASE_8_3_FINAL.md                 本文（8.3 收尾）
├── PHASE_8_SUMMARY.md                 Phase 8 整体总结
├── MANUAL_TESTING.md                  手工测试清单 + 复盘
└── EVAL_DATASET_EXPANSION_GUIDE.md    评测集扩充作业指南
```

---

## 七、下一步候选

Phase 8 完全收尾。下一阶段方向（按 ROI 排序）：

| 优先级 | 方向 | 工时 | 价值 |
|---|---|---|---|
| 🟢 高 | **Phase 11.1 性能优化 3 项**（reranker 预热 + 流式输出 + Qwen3 本地 embedding） | 2-3 天 | 消除 1.3s/问 + 10s 首次开销，体感大幅提升 |
| 🟢 高 | **每月评测集 sprint** | 业务侧 1-2 小时/月 | 评测集 6 个月后到 300+ 真实样本，所有算法决策都能重新校准 |
| 🟡 中 | **Phase 9 图文联动** | 2-6 周 | 独立大方向，与 Phase 8 解耦；现有 SOP 多图，能提升答案丰富度 |
| 🟡 中 | **Phase 11.1 其他 6 项**（日志/审计/FAQ/标签/意图/Rate limit） | 4-5 周 | 上生产前必做 |
| 🔴 低 | Phase 10 多租户 | 6-8 周 | 业务规模到了再做 |
| 🔴 低 | Phase 12 对话智能化 | 4-6 周 | 等 9-11 沉淀 |

---

## 八、致谢业务侧

本期 Phase 8.3 关键转折点：

1. **业务侧手工标注 evaluation set + 在 AI Notes.txt 标 good/bad 答案**
   → 让我们自动派生 20 条 multi_step + 20 条 failing 标签
   → IRCoT 验证有真实诊断目标，而不是凭空堆叠多跳问题
2. **业务侧验收 Phase 8.2 时反馈"系统答案漏步骤"**
   → 印证 multi_step 类问题确实是召回不足
   → Phase 8.3 起跳条件成立

没有这些真实业务信号，Phase 8.3 IRCoT 启动条件就不成立，会按 PLAN §八直接跳过。
