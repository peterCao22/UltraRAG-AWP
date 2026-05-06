# Phase 1 — 核心 RAG 流程开发计划

> 目标：能够上传 AGV 操作 DOCX 文档，完成入库索引，然后通过 API 进行问答，回答包含文字和关联图片（base64）。

---

## 技术配置（已确认）

| 模块 | 选型 | 连接地址 / 说明 |
|------|------|----------------|
| **生成模型** | gpt-oss-120b（vLLM） | `http://192.168.8.44:8100/v1`，无需 API Key |
| **Embedding** | Google `gemini-embedding-001` | 原生 API（非 OpenAI 兼容），需自定义适配层，API Key 存 `.env` |
| **向量索引** | FAISS（本地文件） | 无需额外服务进程 |
| **Reranker** | **Phase 1 暂不引入** | 用较大 `top_k=8` 补偿；Phase 2 按需加本地模型 |
| **文档格式** | `.docx` | `python-docx` 解析，自定义图片导出 |

> **Reranker 说明**：Google 目前没有 reranking API；本地 `sentence_transformers` reranker（如 `BAAI/bge-reranker-v2-m3`）需要 GPU 且引入额外延迟。Phase 1 优先跑通主链路，用 top_k=8 + 更精准的 embedding 模型覆盖召回质量；如后续评估确实需要，再在 Phase 2 接入本地 Reranker Server。
**语言策略（Phase 1）**：当前 AGV 原始文档以英文为主，因此问答 Prompt 与默认输出语言统一使用英文。若后续引入中文原始语料，再扩展中英文双语策略。
---

## 任务清单

```
Phase1/
├── T1  环境搭建与配置文件          → docs/Phase1/01_环境与配置.md
├── T2  DOCX 解析器开发              → docs/Phase1/02_DOCX解析入库.md
├── T3  向量索引构建流程             → docs/Phase1/03_向量索引构建.md
├── T4  问答 Pipeline + 响应格式    → docs/Phase1/04_问答流程.md
└── T5  质量优化实施清单             → docs/Phase1/05_优化版实施清单.md
```

### T1 — 环境与配置 【前置】

- [ ] 1.1 创建 `.env` 文件，写入 Google API Key  
- [ ] 1.2 修改 `servers/generation/parameter.yaml`（指向 vLLM）  
- [ ] 1.3 修改 `servers/retriever/parameter.yaml`（指向 Google Embedding）  
- [ ] 1.4 验证 vLLM 接口连通性（`curl` 测试）  
- [ ] 1.5 验证 Google Embedding API 连通性（Python 测试脚本）  
- [ ] 1.6 用 Anaconda 创建 Python 3.11 虚拟环境并安装所需依赖

### T2 — DOCX 解析器开发 【核心定制开发】

- [ ] 2.1 创建 `custom_app/services/docx_parser.py`  
- [ ] 2.2 实现段落文本提取（保留段落序号 `para_idx`）  
- [ ] 2.3 实现表格提取（每行转为 `col1 | col2 | ...` 文本，附表格所在段落位置）  
- [ ] 2.4 实现嵌入图片导出（保存到 `data/kb/<kb_id>/images/`，记录 `para_idx`）  
- [ ] 2.5 生成 `raw_paragraphs.jsonl`（含 `id / title / contents / para_range / images` 字段）  
- [ ] 2.6 批量处理目录下所有 `.docx` 文件  
- [ ] 2.7 用 `BatteryChangeSequenceSOP.docx` 跑通并人工校验输出

### T3 — 向量索引构建 【UltraRAG 配置 + 测试】

- [ ] 3.1 新增 `examples/agv_chunk.yaml`（文本分块 pipeline）  
- [ ] 3.2 新增 `examples/agv_index.yaml`（embed + index pipeline）  
- [ ] 3.3 跑通分块：`ultrarag run examples/agv_chunk.yaml` → `chunks.jsonl`  
- [ ] 3.4 跑通索引：`ultrarag run examples/agv_index.yaml` → `index.index + embedding.npy`  
- [ ] 3.5 验证索引：Python 脚本直接调 `retriever_search` 确认召回结果

### T4 — 问答 Pipeline + 响应格式 【开发 + 集成验证】

- [ ] 4.1 新增 `examples/agv_rag.yaml`（问答 pipeline）  
- [ ] 4.2 新增 AGV 专用 Prompt 模板 `prompt/agv_qa_rag.jinja`  
- [ ] 4.3 创建 `custom_app/services/rag_runner.py`（封装完整问答逻辑 + base64 图片拼装）  
- [ ] 4.4 创建最小 Flask API `custom_app/app.py`（一个 `/api/chat` 端点用于验证）  
- [ ] 4.5 端到端测试：提问 → 检索 → 生成 → 返回文字 + 关联图片  
- [ ] 4.6 验收：表格问题能输出表格文本；含图片段落问题能返回 base64 图

---

## 交付标准（Phase 1 完成判定）

1. `ultrarag run examples/agv_index.yaml` 成功建索引，无报错。  
2. 调用 `POST /api/chat`，入参 `{"kb_id": "agv_demo", "question": "换电步骤是什么？"}` 能返回：  
   - `answer`：非空文字，内容与文档相关  
   - `sources`：至少 1 条，含 `title`、`snippet`、`images`（可为空数组）  
3. 文档中有图片的段落被检索命中时，`sources[].images` 不为空，图片可在浏览器 `<img src>` 正常显示。

---

## Phase 1 优化版建议（在基础版之上）

### 目标问题

- 回答啰嗦、信息重复：召回片段过多且缺少有效压缩。
- 图文不匹配：回答步骤与图片绑定不稳定。

### UltraRAG 已有可复用能力（优先采用）

- 已有 `reranker` 服务能力，可直接接入本地重排模型。
- 已有 `hybrid_search` 示例（向量 + 关键词），可用于补召回。
- 已有 `loop/branch` 多步检索示例，可用于复杂问题扩展检索。
- Prompt 模板服务已支持可控注入上下文与格式约束。

### WeKnora 可借鉴点（面向 Phase 1 的最小迁移）

- QueryUnderstand：先改写 query，再进入检索，降低噪声召回。
- FilterTopK：将截断阶段显式化，便于 A/B 和参数调优。
- Rerank 增强：重排后再做去冗余（含相似片段压缩）。
- Merge 去重：去掉重复/近重复 chunk，减少答案冗长。

### 推荐最小落地顺序（Phase 1.5）

1. 召回放大 + 截断收敛：`recall_top_k=12`，重排后取 `top_k=3`。  
2. 增加 `rewrite_query`：先做一轮轻量 LLM 改写，再检索。  
3. 新增本地 rerank（GPU）：优先轻量 cross-encoder，小 batch 运行。  
4. 图文绑定从“关键词猜测”升级为“source_id 显式绑定”。

### 验收口径（优化版）

- 相同问题下，`sources` 数量下降且答案长度更短。
- 关键步骤命中率不降低（保证准确性不回退）。
- 图片与步骤对齐率提高（抽样人工评估）。
- 单次响应时延可控（本地 GPU rerank 不显著拖慢）。

---

## 文件生成路径一览

| 类型 | 路径 |
|------|------|
| 环境变量 | `.env` |
| 生成配置 | `servers/generation/parameter.yaml` |
| 检索配置 | `servers/retriever/parameter.yaml` |
| 分块 Pipeline | `examples/agv_chunk.yaml` |
| 建索引 Pipeline | `examples/agv_index.yaml` |
| 问答 Pipeline | `examples/agv_rag.yaml` |
| AGV Prompt 模板 | `prompt/agv_qa_rag.jinja` |
| DOCX 解析器 | `custom_app/services/docx_parser.py` |
| RAG 运行封装 | `custom_app/services/rag_runner.py` |
| 最小 Flask 入口 | `custom_app/app.py` |
| 知识库数据根目录 | `data/kb/agv_demo/` |
