# Phase 8 —— 检索质量量化 + 高级策略试点（借用-剥离模式）

> **状态**：计划草案（2026-05-16），三个子阶段待逐一讨论确认
> **前置**：[Phase 5](../Phase5/PHASE5_PLAN.md)（Qdrant + Postgres + Neo4j 三栈双后端就绪）、[Phase 6.0](../Phase6/PHASE_6_COMPLETION.md)（Ingest KG）、[Phase 7](../Phase7/PHASE_7_PLAN.md)（对话模型可配置）
> **不在本阶段**：MCP server 化、VisRAG（已确认推后）
> **核心方法论**：**开发期借用 UltraRAG 加速验证 → 生产期剥离纯函数到 custom_app**

---

## 一、阶段目标

把 custom_app 的检索质量从「凭手感」升级到「可量化、可对比、可回归」，并在确认有效的前提下，引入业界已验证的高级检索策略（contextual chunking、BM25 双路、IRCoT）。

**生产部署原则**：UltraRAG runtime 不部署到服务器，仅在开发期作为「PoC 脚手架」使用。

---

## 二、子阶段拆分

| 子阶段 | 目标 | 借用 UltraRAG | 工作量（开发 + 剥离） | 验收门槛 | 状态 |
|--------|------|---------------|---------------------|---------|------|
| [8.0](./PHASE_8_0_PLAN.md) | 兜底滑窗切分（结构松散文档保底） | ❌ 纯 custom_app 小改 | 0.5 天 | 长文档可切多块；现有 KB chunks.jsonl 不变 | ✅ 2026-05-17 |
| [8.1](./PHASE_8_1_PLAN.md) | 离线评测体系 | ✅ 借 evaluation + benchmark server | 2-3 天 + 0.5 天 | 50 条评测集 + 基线 baseline.json | 🟡 工程脚手架就绪（2026-05-17）；待业务侧标注 + 跑 baseline |
| [8.2](./PHASE_8_2_PLAN.md) | Contextual chunking + BM25 双路 | ❌ 完全 custom_app 内部 | 1 周 | 评测分数较 8.1 基线显著提升 | 🟡 8.2.1 + 8.2.2 工程落地（2026-05-17）；8.2.3 评测对比待 8.1 标注 |
| [8.3](./PHASE_8_3_PLAN.md) | IRCoT 移植 | ✅ 借 UltraRAG 验证 → 剥离移植 | 1 周 + 1-2 周 | 评测分数较 8.2 进一步提升；否则不上线 | ⏳ 待启动 |

**累计工作量约 4 周**，按顺序串行执行。每个子阶段都有「评测分数不达标 → 停止」的退出条件。

> **8.0 的定位**：不是优化项，是 8.1 评测的**前置数据修复**——避免结构松散文档因「整篇 1 chunk」拖累评测信号。

---

## 三、借用-剥离模式

### 3.1 为什么不直接在 UltraRAG 上跑生产

| 维度 | 借用 UltraRAG 跑生产 | 剥离到 custom_app |
|------|---------------------|------------------|
| 部署 | 多带 5000+ 行无关代码、9 个 MCP server、fastmcp 进程 | 纯 custom_app + Qdrant/PG/Neo4j |
| 数据 | 要把 Qdrant 数据二次导入 UltraRAG retriever 格式 | 直接复用现有栈 |
| 调试 | MCP 跨进程，日志/堆栈跨多个服务 | 单进程 Flask，断点直达 |
| 演进 | 跟 UltraRAG 上游版本耦合 | 自己掌控 |

### 3.2 可剥离性验证（已确认）

UltraRAG 的算法模块对框架依赖**极薄**：

| 模块 | 总行数 | 框架依赖 | 剥离后净行数 |
|------|--------|----------|-------------|
| `servers/evaluation/src/evaluation.py` | 685 | 仅顶部 `from ultrarag.server import UltraRAG_MCP_Server` + `@app.tool` 装饰器 | ~400（纯指标算法） |
| `servers/benchmark/src/benchmark.py` | 179 | 同上 | ~120 |
| `servers/router/src/router.py` | 280 | 同上 | ~80（IRCoT 部分） |
| `servers/custom/src/custom.py` | 1802 | 同上 | ~300（IRCoT 部分） |

剥离动作：去掉 `@app.tool` 装饰器 + 删掉 `app = UltraRAG_MCP_Server(...)` 那行 → 剩下的是普通 Python 函数。

### 3.3 剥离触发点

| 子阶段 | 触发条件 | 剥离工作量 |
|--------|---------|-----------|
| 8.1 | 评测脚本跑通、基线导出后 | 半天（搬 metrics 到 `custom_app/services/eval/`） |
| 8.2 | 不需要剥离（本来就在 custom_app 写） | — |
| 8.3 | 评测验证 IRCoT 在 SOP 场景有提升后 | 1-2 周（IRCoT 函数 + prompt 模板搬过来） |

### 3.4 剥离后的目标结构

```
custom_app/
├── services/
│   ├── eval/                    # ← Phase 8.1 剥离产物
│   │   ├── metrics.py           # 从 UltraRAG evaluation.py 剥离（~400 行）
│   │   ├── dataset.py           # 评测集 IO
│   │   └── runner.py            # 评测驱动
│   ├── chunking/                # ← Phase 8.2 新增
│   │   └── contextual.py        # 自写
│   ├── retrieval/               # ← Phase 8.2 新增
│   │   ├── bm25.py              # 自写（rank_bm25 / PG FTS5）
│   │   └── rrf.py               # 自写（参考 WeKnora 30 行）
│   ├── strategies/              # ← Phase 8.3 剥离产物
│   │   ├── ircot.py             # 从 UltraRAG custom.py 剥离（~300 行）
│   │   └── ircot_router.py      # 从 UltraRAG router.py 剥离（~80 行）
│   └── rag_runner.py            # 加 chat_bm25 / chat_ircot 方法
├── prompts/                     # ← Phase 8.3 复制产物
│   └── ircot_*.jinja
└── scripts/
    └── eval_custom_app.py       # ← Phase 8.1 评测驱动入口
```

---

## 四、退出条件（贯穿三个子阶段）

每个子阶段都设「分数门槛」，未达标则不进入下一阶段：

| 子阶段 | 关键指标 | 期望提升 | 不达标的处理 |
|--------|---------|---------|-------------|
| 8.1 | 基线 Recall@5 / MRR / F1 | — （建立基线） | 评测集质量不够 → 补充样本到 ≥50 条 |
| 8.2 | Recall@5 提升 ≥10pp 或 MRR 提升 ≥0.05 | 显著 | 收益不显著 → 8.2 不上线，跳到 8.3 用 8.1 基线对比 |
| 8.3 | F1 提升 ≥0.05 且端到端延迟 <2× | 中等 | 收益不显著 → IRCoT 不上线，Phase 8 至 8.2 收尾 |

---

## 五、与既有 Phase 的边界

| Phase | 关系 |
|-------|------|
| Phase 5 | 提供 Qdrant 检索、Neo4j KG 查询基础设施；Phase 8 不修改后端栈 |
| Phase 6 | Ingest 流程；Phase 8.2 contextual chunking 会改 `_run_ingest_job` 的 parse 阶段 |
| Phase 7 | 对话模型可配置；Phase 8.1 评测脚本可在不同模型间对比 |
| Phase 8 ↔ 自身 | 三个子阶段严格串行，8.1 先跑、8.2 用 8.1 量化、8.3 用 8.2 量化 |

---

## 六、文档清单

- [PHASE_8_0_PLAN.md](./PHASE_8_0_PLAN.md) —— 兜底滑窗切分（结构松散文档保底）
- [PHASE_8_1_PLAN.md](./PHASE_8_1_PLAN.md) —— 离线评测体系
- [PHASE_8_2_PLAN.md](./PHASE_8_2_PLAN.md) —— Contextual chunking + BM25 双路
- [PHASE_8_3_PLAN.md](./PHASE_8_3_PLAN.md) —— IRCoT 移植

---

> 本 README 是 Phase 8 的方向锚点。三个子阶段计划文档在讨论确认后**逐一**进入实施。
