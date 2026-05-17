# Phase 9 —— 图文联动（Multimodal Image-Text Linking）

> **状态**：方向锚点（2026-05-16），详细计划待 Phase 8 完成后再展开
> **前置**：[Phase 8](../Phase8/README.md) 全部完成（**必须**有评测基线，否则 Phase 9 的"是否有效"无法判定）
> **参考实现**：`D:\Peter2025\myCursor\RAG-Anything\raganything\modalprocessors.py` 的 `ImageModalProcessor`（行 826-960）
> **参考论文**：VisRAG-2（arxiv 2410.10594）、ColPali（arxiv 2407.01449）、M3DocRAG（arxiv 2411.04952）

---

## 一、阶段目标

让 SOP 文档中的图片**真正参与检索和生成**，而不只是被动地附着在 chunk 上：

1. **图片有语义**：每张图被 VLM 生成结构化描述（caption + entity）
2. **图片可检索**：图片描述参与向量检索；命中文本 chunk 时若 KG 关联图片，自动带出
3. **跨章节联动**：用户问 A 章节的问题，**B 章节甚至 B 文档**的相关图片能作为补充答案出现

---

## 二、用户场景（来自实际需求）

> "我在检索一个问题，这个问题可能涉及到内容会在另外一个章节或者段落，甚至另外一个文件，但有个图片的内容能给好的补充说明它，作为回复，把这个图片显示出来放在段落文字下面，并适当的对上下文进行描述。"

**SOP 典型例子**：

- 用户问："AGV 急停按钮在哪？"
- 命中文本 chunk："…按下急停按钮可立即停止 AGV…"（章节 3）
- **现状**：只回文字，用户还要翻文档找图
- **Phase 9 目标**：自动带出章节 5 的"AGV 控制面板示意图"——因为该图被识别为含实体 `急停按钮`，KG 把两者关联

---

## 三、为什么不放进 Phase 8

| 维度 | 说明 |
|------|------|
| **范围不同** | Phase 8 是「检索质量量化 + 文本侧优化」；Phase 9 是「引入新模态（图像语义）」 |
| **依赖前置** | Phase 9 必须有 Phase 8.1 评测基线，否则改完不知道是有效还是无效 |
| **改动大** | 涉及 ingest 新 stage、KG schema 扩展、检索路径融合、生成 prompt 调整——4 个层面 |
| **风险高** | VLM 调用成本上涨 5-10×（每张图 1 次）、生成质量依赖 prompt 工程 |
| **优先级** | Phase 8 的 contextual chunking + BM25 是「稳赚」；Phase 9 是「高风险高回报」，必须先稳后险 |

---

## 四、技术方案（高层）

参考 RAG-Anything `ImageModalProcessor`（[modalprocessors.py:826](../../../RAG-Anything/raganything/modalprocessors.py)）的成熟实现，结合你已有的 Neo4j KG 栈。

### 4.1 三层能力拆解

| 层 | 名称 | 工作量 | 风险 | 收益 |
|---|------|--------|------|------|
| 9.1 | 图片语义抽取（VLM caption + entity） | 1-2 周 | 中 | 中（基础） |
| 9.2 | 图片参与检索（caption 融入向量空间） | 1 周 | 中 | **高** |
| 9.3 | 跨章节图文联动（KG 实体桥接） | 2-3 周 | 高 | **极高** |

### 4.2 数据流

```
docx 已存在的图片
  │
  ▼
ImageDescriber (Gemini Vision)
  │  prompt: "结合上下文 {prev_chunk}, 用 50-150 字描述这张图，并抽取关键实体"
  ▼
图片元数据扩充
  {
    "path": "images/agv/img_0003.png",
    "caption": "AGV 控制面板：左上方红色按钮为急停按钮，中央显示屏…",
    "entities": ["急停按钮", "控制面板", "显示屏"]
  }
  │
  ├─► 写回 chunks.jsonl（chunk.images 升级为对象数组）
  │
  ├─► kg_extractor 把图片作为节点写入 Neo4j
  │   :Image {path, caption, kb_id}
  │   :Image -[:MENTIONS]-> :Entity {name: "急停按钮"}
  │
  └─► caption 文本拼到对应 chunk 的 embedding 输入（Phase 8.2 contextual 的扩展）

检索时：
  query → 向量召回文本 chunks（top-5）
        → 对每个 chunk，从 Neo4j 查"该 chunk 提到的实体 → 关联的图片"
        → 合并：文本 + 图片（去重）→ 送入 LLM 生成
```

### 4.3 改造点（粗估，详细见 9.1/9.2/9.3 PLAN）

| 文件 | 改动 |
|------|------|
| `custom_app/services/parsers/image_describer.py` | **新增**：调 Gemini Vision 批量描述 |
| `custom_app/services/parsers/schema.py` | `Chunk.images` 升级为 `list[ImageMeta]`，向后兼容 str 数组 |
| `custom_app/services/docx_parser.py` | 不动（图片抽取逻辑保持） |
| `custom_app/services/kg_extractor.py` | 加 `extract_from_image` 分支 |
| `custom_app/services/kgstore/neo4j_store.py` | 加 `:Image` 节点类型 + `MENTIONS` 关系 |
| `custom_app/services/rag_runner.py` | 检索后调 `_expand_images_via_kg` |
| `custom_app/api/kb.py:_run_ingest_job` | 加 `image_describe` stage（parse 之后、embed 之前） |
| `custom_app/prompts/` | 生成 prompt 加图片块支持 |

---

## 五、子阶段拆分

### Phase 9.1 — 图片语义抽取（基础设施）

**目标**：每张图都有 `(caption, entities)` 元数据，落盘但**还不参与检索**

**工时**：1-2 周

**关键工作**：
- 新建 `image_describer.py`：批量并行调 Gemini Vision
- chunks.jsonl schema 升级 `images: list[ImageMeta]`
- ingest stage 接入
- 回填两个评测 KB（ifs_docs / agv_demo）所有图片

**验收**：所有现有图片都有 caption + entities，可在 admin 视图查看（**这时才需要 admin 显示图片**——你之前确认本期不做，Phase 9.1 时再看）

---

### Phase 9.2 — 图片参与检索（caption 融入向量）

**目标**：图片 caption 作为文本拼接到 chunk embedding，**或**单独建图片向量子库

**工时**：1 周

**关键工作**：
- 决策点：方案 A（拼到 chunk embedding）vs 方案 B（独立 multimodal collection）
  - A 更简单、改动小，先做
  - B 更准、需 CLIP/Jina-CLIP，Phase 9.2.x 增量
- 检索时图片可作为独立 hit 返回（不只是 chunk 附属）
- 生成 prompt 模板支持「图片块」

**验收**：Phase 8.1 评测集上跑分对比，至少一个指标显著提升（Recall@5 ≥+5pp 或 F1 ≥+0.03）

---

### Phase 9.3 — 跨章节图文联动（KG 桥接）

**目标**：实现你描述的「问题在 A 章节，图片在 B 章节也能被关联出来」

**工时**：2-3 周

**关键工作**：
- Neo4j 加 `:Image` 节点类型 + `(:Chunk)-[:CONTAINS]->(:Image)` + `(:Image)-[:MENTIONS]->(:Entity)`
- 检索时 `_expand_images_via_kg(text_hits)`：从命中 chunk 的实体出发，邻居扩散找图片
- 控制：避免实体爆炸（如"系统"这种通用词关联太多图，要按"实体相关度"过滤）
- 生成 prompt 加引用控制（必须用 `![](url)` markdown）

**验收**：人工抽 10 条 query 评估「跨章节图片是否合理」+ Phase 8.1 评测整体不下降

---

## 六、关键风险（高层）

| 等级 | 风险 | 缓解方向 |
|------|------|---------|
| 🔴 HIGH | VLM 生成的 caption 质量不稳定（Gemini 偶尔出"这是一张图片"这种废话） | Phase 9.1 PoC 阶段抽 20 张样本人工评，prompt 迭代 |
| 🔴 HIGH | 图片实体抽取出大量噪声（如"按钮"这种通用词） | 实体过滤：长度 ≥2、不在停用词表、TF-IDF 加权 |
| 🟡 MED | 跨章节联动可能引入"看似相关实际无关"的图片 | Phase 9.3 加相关度阈值；评测时人工 review 失败案例 |
| 🟡 MED | Gemini Vision 配额 + 成本 | 39 chunks × 平均 3 张图 ≈ 120 次调用 / KB ingest，可接受；缓存到 chunks.jsonl 不重算 |
| 🟡 MED | LightRAG 是 RAG-Anything 的核心，剥离时不能直接搬代码 | 借鉴算法思路，自己写 Neo4j 实现 |
| 🟢 LOW | 旧 KB 的图片需回填 caption | 一次性回填脚本；非阻塞 |

---

## 七、退出条件

每个子阶段都有独立的 go/no-go：

| 子阶段 | 退出条件 | 不达标处理 |
|--------|----------|-----------|
| 9.1 | Caption 人工评估准确率 ≥80% | 调 prompt；不达标推迟 9.2/9.3 |
| 9.2 | Phase 8.1 评测分数显著提升 | 不上线，Phase 9 至 9.1 收尾 |
| 9.3 | 跨章节图片人工评估"合理"率 ≥70% | 9.3 推迟；只保留 9.2 |

**最坏情况**：Phase 9 至 9.1 收尾——所有图片至少有了 caption 元数据，admin 视图可看，未来其他工作可以复用。

---

## 八、与既有 Phase 的关系

| Phase | 关系 |
|-------|------|
| Phase 5.2 | Neo4j KG 是 Phase 9.3 的基础设施 |
| Phase 6.0 | KG ingest stage 是 Phase 9.3 加 `:Image` 节点的扩展点 |
| Phase 7 | 对话模型可配置；Phase 9 的 VLM 调用走 Gemini，与 chat 模型解耦 |
| **Phase 8.1** | **强依赖**：Phase 9 所有评测都基于 8.1 基线 |
| Phase 8.2 | Phase 9.2 的"caption 拼到 chunk embedding"是 8.2 contextual chunking 的扩展 |
| Phase 8.3 | IRCoT 多轮可与 Phase 9.3 图文联动叠加（IRCoT 第二轮检索可命中图片） |

---

## 九、文档清单（待写）

- [ ] PHASE_9_1_PLAN.md —— 图片语义抽取（Phase 8 完成后展开）
- [ ] PHASE_9_2_PLAN.md —— 图片参与检索
- [ ] PHASE_9_3_PLAN.md —— 跨章节图文联动

**当前文档仅为方向锚点**。详细任务拆分等 Phase 8 全部完成、拿到评测基线后再写。

---

## 十、给未来自己的备忘

写详细计划时要确认：

1. **VLM 选型**：Gemini Vision（默认）vs 本地 MiniCPM-V vs Qwen-VL 之间的成本/质量对比
2. **图片 caption 的语言**：中英混合 SOP 该用中文 prompt 还是英文？影响检索匹配
3. **实体规范化**：图片实体和文本实体如何合并？"急停按钮" vs "急停" vs "紧急停止按钮"是否同一实体？
4. **检索时图片配额**：每条 query 最多返回几张图？避免回复爆炸
5. **生成 prompt 的图文混排格式**：LLM 应该输出什么 markdown 才能让前端正确渲染
6. **回填策略**：旧 KB 全量回填还是只对新 ingest 生效

---

> **下一步**：等 Phase 8 跑完拿到评测基线，再回头展开 Phase 9.1 详细计划。
