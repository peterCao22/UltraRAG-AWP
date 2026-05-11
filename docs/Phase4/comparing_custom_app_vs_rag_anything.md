# UltraRAG custom_app 与 RAG-Anything 对比分析

> 对比时间：2026-05-08
> RAG-Anything 项目：https://github.com/HKUDS/RAG-Anything

---

## 一、整体架构对比

### UltraRAG custom_app

基于 UltraRAG MCP 框架的**垂直应用**，面向 SOP/FAQ 场景的深度定制 RAG 系统。

```
.docx 上传
    |
    v
+---------------------------------------------------+
| Stage 1: 解析   raw/*.docx -> corpora/chunks.jsonl  |
| Stage 2: 嵌入   chunks.jsonl -> embedding/*.npy    |
| Stage 3: 索引   embedding/*.npy -> index/*.index    |
+---------------------------------------------------+
    |
    v
[可选] KG 抽取  chunks.jsonl -> SQLite (kg_entities / kg_relations)
```

### RAG-Anything

基于 LightRAG 的**通用多模态文档处理管道**，面向多种文档格式的开箱即用系统。

```
多种文件（PDF/Office/图片/文本）
    |
    v
格式归一化（LibreOffice/ReportLab/Pillow）
    |
    v
三引擎解析（MinerU/Docling/PaddleOCR）
    |
    v
content_list.json -> 按 type 分类路由
    |
    v
VLM 生成文字描述（GPT-4o / Claude）
    |
    v
存入 LightRAG 知识图谱 + 向量索引
```

---

## 二、文档解析层对比

| 维度 | UltraRAG custom_app | RAG-Anything |
|------|---------------------|--------------|
| **解析引擎** | python-docx | MinerU / Docling / PaddleOCR（三选一） |
| **支持格式** | 仅 .docx | PDF, DOC/DOCX, PPT/PPTX, XLS/XLSX, TXT, MD, JPG, PNG, BMP, TIFF, GIF, WebP |
| **Office 处理** | 直接解析 DOCX XML | LibreOffice 转 PDF 后交给 MinerU/Docling |
| **PDF 支持** | 未实现（扩展名在白名单中但无解析器） | 原生支持（MinerU/Docling） |
| **图片支持** | 仅从 DOCX 中提取内嵌图片 | PDF 内图片提取 + 独立图片文件直接处理 |
| **OCR** | 不支持 | 支持（PaddleOCR，扫描件/图片文字识别） |
| **公式处理** | 不支持 | 支持（MinerU 提取 + EquationModalProcessor） |
| **表格处理** | 转为管道符分隔文本 | MinerU/Docling 结构化提取 + TableModalProcessor |
| **分块策略** | 业务定制（STEP-based + Heading-based） | 通用（按 MinerU 内容块自然分段） |
| **批量处理** | 无（逐文件处理） | 有（ThreadPoolExecutor 并行 + 进度条） |
| **自定义解析器** | 无插件机制 | 有（register_parser 注册机制） |

### 解析辅助工具

| 工具 | UltraRAG custom_app | RAG-Anything |
|------|---------------------|--------------|
| LibreOffice | 不使用 | Office 转 PDF |
| ReportLab | 不使用 | TXT/MD 转 PDF |
| Pillow | 不使用 | 图片格式标准化 |
| pypdfium2 | 不使用 | PDF 渲染为图片（供 OCR） |

---

## 三、分块策略对比

### UltraRAG custom_app — 业务定制分块

```
DOCX 文件
    |
    v
STEP-based 分块（按 "STEP <n>:" 行分割）
    |
    v
Heading-based 分块（按 H1/H2/H3 样式分割）
    |
    v
每个 chunk = { id, title, contents, doc, images[] }
```

- 核心文件：`services/docx_parser.py:209`
- STEP 标记：`STEP_RE = re.compile(r"^\s*STEP\s+(\d+)\s*:", re.IGNORECASE)`
- chunk id 命名：`<doc_stem>_step_<N>` / `<doc_stem>_intro` / `<doc_stem>_section_<N>`

### RAG-Anything — 通用内容分块

```
content_list.json（MinerU 输出）
    |
    v
separate_content() 按 type 分类
    |
    v
text / image / table / equation / custom -> 各自处理器
    |
    v
每个 chunk = 格式化模板（image_chunk / table_chunk / ...）
```

- 核心文件：`raganything/processor.py` -> `separate_content()`
- 每个多模态元素独立成 chunk，附带 VLM 描述

---

## 四、多模态处理层对比

| 维度 | UltraRAG custom_app | RAG-Anything |
|------|---------------------|--------------|
| **图片嵌入** | 不支持，嵌入时剥离图片占位符 | 支持，VLM 生成描述后存入向量 |
| **图片理解** | 无，仅保留路径引用供前端展示 | GPT-4o 等 VLM 生成 detailed_description + entity_info |
| **VLM 增强查询** | 不支持 | 支持（vlm_enhanced=True，检索到图片后发给 VLM 分析） |
| **图文关联** | 图片以内联 `[IMG: ...]` 占位符嵌入 chunk 文本 | 图片作为独立 entity 存入知识图谱 |
| **表格理解** | 管道符文本，无语义分析 | TableModalProcessor 语义分析 |
| **公式理解** | 不支持 | EquationModalProcessor 数学分析 |

### RAG-Anything 图片处理链路

```
图片路径 + 标题 + 脚注
    |
    v
构建 vision_prompt（含上下文）
    |
    v
图片 base64 编码
    |
    v
发送给 vision_model_func（如 GPT-4o）
    |
    v
返回 detailed_description + entity_info
    |
    v
格式化为 image_chunk 存入 LightRAG
```

---

## 五、嵌入与索引层对比

| 维度 | UltraRAG custom_app | RAG-Anything |
|------|---------------------|--------------|
| **嵌入模型** | Google Gemini `gemini-embedding-001`（云端 API） | 本地 sentence_transformers / infinity_emb / OpenAI API |
| **嵌入维度** | 768（从 3072 截断） | 依模型而定（通常 768-1024） |
| **嵌入内容** | `title + "\n" + contents`（纯文本，剥离图片） | 文本 chunk + VLM 图片描述 + 表格描述 + 公式描述 |
| **归一化** | L2 归一化 | 依模型配置 |
| **向量库** | FAISS `IndexIDMap2` + `IndexFlatIP`（内存加载） | FAISS / Milvus（LightRAG 层） |
| **相似度计算** | 余弦相似度（L2 归一化 + 内积） | 依后端配置 |
| **重排序** | **有**（CrossEncoder `bge-reranker-v2-m3`） | 无内置（LightRAG 支持外部 reranker） |

---

## 六、知识图谱层对比

| 维度 | UltraRAG custom_app | RAG-Anything |
|------|---------------------|--------------|
| **图谱类型** | SQLite 表（entities + relations 两表） | Neo4j / PostgreSQL / OpenSearch（真正的图谱数据库） |
| **抽取方式** | Gemini API + Jinja2 模板提示词 | LLM 抽取实体关系 + 自动构建图谱 |
| **实体类型** | Person, Organization, Location, Product, Event, Date, Work, Concept, Resource, Category, Operation | 从 VLM 描述中动态提取 |
| **查询方式** | 自定义工具 `QueryKnowledgeGraphTool` | 图谱遍历 + 向量混合检索 |
| **层次关系** | 无 | 有（belongs_to 层次、权重评分） |

---

## 七、搜索流程对比

### UltraRAG custom_app — 两模式检索

**Quick Mode（RagRunner）：**

```
用户问题
    |
    v
_rewrite_query(question) -> 改写查询（LLM）
    |
    v
embed_query(rewritten_q) -> 768-dim 向量（Gemini）
    |
    v
FAISS search(q_vec, recall_k) -> Top-K 命中
    |
    v
_rerank_hit_ids() -> CrossEncoder 重排序
    |
    v
_expand_hit_ids() -> SOP 扩展（命中 STEP 后拉取全文档）
    |
    v
_build_prompt() -> Jinja2 模板渲染
    |
    v
_generate() -> Gemini/OpenAI LLM 生成答案
```

**Agent Mode（AgentRunner）：**

```
用户问题 -> ReAct loop（最多 12 轮）
    |
    v
工具注册：
  - KnowledgeSearchTool   (语义向量搜索)
  - KeywordSearchTool     (精确关键词匹配)
  - ListChunksTool        (深度阅读：全文档 chunk)
  - QueryKnowledgeGraphTool (知识图谱查询)
  - FinalAnswerTool       (终止循环)
    |
    v
SSE 实时流：thought -> tool_call -> tool_result -> chunk -> done
```

### RAG-Anything — 三查询模式

```
用户问题
    |
    v
mode="hybrid" -> 向量搜索 + 图谱遍历混合排序
    |
    v
vlm_enhanced=True -> 检索到的图片 base64 编码 -> VLM 分析
    |
    v
aquery_with_multimodal() -> 支持附带图片/表格查询
```

| 维度 | UltraRAG custom_app | RAG-Anything |
|------|---------------------|--------------|
| **查询改写** | **有**（LLM 改写后再检索） | 无 |
| **重排序** | **有**（CrossEncoder） | 无内置 |
| **SOP 扩展** | **有**（命中 STEP 后拉取同文档所有 chunk 按序排列） | 无（有 context_window 上下文扩展） |
| **Agent 模式** | **有**（ReAct loop + 4 工具） | 无 |
| **混合检索** | 语义 + 关键词（Agent 工具内选择） | 向量 + 图谱遍历混合 |
| **多模态查询** | 不支持 | 支持（aquery_with_multimodal） |
| **VLM 增强** | 不支持 | 支持（vlm_enhanced） |

---

## 八、工程架构对比

| 维度 | UltraRAG custom_app | RAG-Anything |
|------|---------------------|--------------|
| **Web 框架** | Flask + Blueprint | 无内置（LightRAG dashboard） |
| **前端** | 原生 JS + Vue 3 + marked + DOMPurify | 无内置 |
| **数据库** | SQLite（元数据管理） | 依 LightRAG 配置 |
| **任务队列** | 自定义 JobExecutor（FIFO + 单线程） | asyncio 异步管道 |
| **API** | RESTful + SSE 流式 | Python SDK |
| **认证/角色** | 有（roles API + login） | 无 |
| **知识库管理** | 有（多 KB + 文档级 CRUD + 任务状态追踪） | 工作目录级 |
| **批量处理** | 无 | 有（文件夹递归 + 并行） |
| **部署** | Python 进程 + 前端静态文件 | pip install raganything |

---

## 九、总结：各自优势与劣势

### UltraRAG custom_app 优势

1. **检索精度高** — 查询改写 + 向量召回 + CrossEncoder 重排序 + SOP 扩展
2. **Agent 模式** — ReAct loop 让模型自主选择检索策略
3. **业务适配强** — STEP-based 分块完美匹配 SOP/FAQ 文档
4. **SOP 扩展** — 命中 STEP 后自动拉取全文档上下文
5. **工程完整** — Flask API + 异步任务 + 前端界面 + 认证 + 知识库管理
6. **流式输出** — SSE 实时流式返回（含 Agent 思考过程）

### UltraRAG custom_app 劣势

1. **解析能力单薄** — 仅支持 DOCX，无 PDF/图片/扫描件处理
2. **无多模态检索** — 图片不参与语义检索
3. **嵌入依赖云端** — Gemini API 有网络和成本约束
4. **格式扩展困难** — 加 PDF 支持需重写解析层
5. **无批量处理** — 不支持文件夹递归和并行解析

### RAG-Anything 优势

1. **解析能力强** — 三引擎覆盖 PDF/Office/图片/扫描件/公式/表格
2. **多模态理解** — VLM 为图片/表格/公式生成语义描述
3. **格式支持广** — 16+ 种文件格式
4. **批量处理** — 文件夹递归 + 线程池并行
5. **本地化部署** — 嵌入和解析均可本地运行
6. **自定义解析器** — register_parser 插件机制

### RAG-Anything 劣势

1. **无业务定制** — 通用分段，无 STEP-aware 分块
2. **检索精度有限** — 无查询改写/重排序/SOP 扩展
3. **无 Agent 模式** — 不支持多步推理和工具调用
4. **无工程框架** — 无 Web API/前端/任务管理
5. **图片搜索间接** — 依赖 VLM 描述质量，无视觉向量

---

## 十、融合建议

如果将 RAG-Anything 的**解析层**接入 UltraRAG custom_app 的**三阶段入库流程**：

```
多种格式文件（PDF/DOCX/图片）
    |
    v
[RAG-Anything 解析层] MinerU/Docling/PaddleOCR
    |
    v
content_list.json
    |
    v
[UltraRAG 适配层] 转换为 chunks.jsonl 格式
    （保留 STEP/Heading 分块逻辑）
    |
    v
[UltraRAG 嵌入层] Gemini 嵌入
    |
    v
[UltraRAG 索引层] FAISS 索引 + 检索
```

这样可以同时获得：
- RAG-Anything 的**宽格式解析能力**
- UltraRAG 的**高精度检索和业务适配**

---

## 十一、RAG-Anything 源码深读（2026-05-11）

> 阅读 `D:\Peter2025\myCursor\RAG-Anything\raganything\` 后整理的关键事实，用于指导 Phase 4 设计决策。

### 11.1 content_list.json 的真实结构

MinerU 的输出是一个**保序的扁平段落流**，每个元素是一个带 `type` 字段的 dict：

```json
[
  {"type": "text", "text": "...", "text_level": 1, "page_idx": 0},
  {"type": "text", "text": "...", "text_level": 0, "page_idx": 0},
  {"type": "image", "img_path": "...", "image_caption": [...], "page_idx": 1},
  {"type": "table", "table_body": "| ... |", "table_caption": [...], "page_idx": 2},
  {"type": "equation", "latex": "...", "text": "...", "page_idx": 3}
]
```

**关键字段含义**：

| 字段 | 含义 | 对融合的价值 |
| --- | --- | --- |
| `type` | text / image / table / equation | 用于路由到对应处理器 |
| `text_level` | 0=正文，>=1 是标题层级（H1/H2/H3...） | **可重建 Heading-based 分块树** |
| `page_idx` | 页码，0-based | 提供位置上下文，可做"邻近段落"加权 |
| `text` | 文本内容（text 类型） | 主体内容 |
| `img_path` | 图片路径（image 类型） | 给前端展示；未来 CLIP 视觉索引锚点 |
| `image_caption` / `image_footnote` | 图片标题/脚注 | 作为图片的"伪文本"参与检索 |
| `table_body` | 表格 markdown | 已有结构，无需再分析 |
| `latex` | 公式 LaTeX | 公式语义保留 |

**核心来源**：

- `raganything/utils.py:14` — `separate_content()` 实现
- `raganything/modalprocessors.py:215-235` — `_extract_text_from_item()` 字段使用示例
- `raganything/parser.py:958` — `_read_output_files()` 读取 `<stem>_content_list.json`

### 11.2 `separate_content()` 的副作用（必须规避）

```python
# raganything/utils.py:14
def separate_content(content_list):
    text_parts = []
    multimodal_items = []
    for item in content_list:
        if item.get("type") == "text":
            text_parts.append(item.get("text", ""))
        else:
            multimodal_items.append(item)
    text_content = "\n\n".join(text_parts)
    return text_content, multimodal_items
```

**问题**：把所有 text 用 `\n\n` 拼成一坨，多模态扔到独立列表。这丢失了：

1. **图文邻近关系** — "图片插在 STEP 3 文本之后"这种位置信息消失
2. **标题层级链** — H1/H2/H3 的父子关系在拼接后变成普通段落分隔
3. **STEP 边界** — 多个 STEP 段落被拼接到同一个长文本里

**结论**：UltraRAG 接入 MinerU 时**不要调用 `separate_content()`**。应该直接在原始 `content_list` 上跑自己的分块逻辑（保留位置和层级信息）。

### 11.3 DOCX → PDF 中转的隐患（关键风险）

`raganything/parser.py:1298` 的 `parse_office_doc()` 显示，MinerU 处理 DOCX 的链路是：

```
DOCX → LibreOffice 转 PDF → MinerU 解析 PDF → content_list.json
```

**这是一条有损链路**，对 SOP 文档尤其致命：

| 信息维度 | DOCX 直读（现有 docx_parser.py） | 经 PDF 中转后 |
| --- | --- | --- |
| 段落样式名（Heading 1/Heading 2） | 精确读取 | 丢失，只能靠字号粗细推断 `text_level` |
| 内嵌图片在原文中的段落位置 | 通过 `paragraph_idx` 精确定位 | 变成 PDF 视觉坐标 |
| `"STEP 5:"` 行边界 | DOCX XML 中是独立段落 | PDF 化后可能被合并到正文 |
| 表格单元格语义 | DOCX 表格 XML 直接拿 | 转 PDF 后靠视觉识别 |

**直接结论**：**SOP DOCX 不应该走 MinerU 路径**。现有 `docx_parser.py` 的精度是业务核心资产，必须保留。

### 11.4 MinerU 工程层面的现实

| 维度 | 实际情况 | 部署影响 |
| --- | --- | --- |
| 调用方式 | 外部 CLI 子进程：`mineru -p input -o output -m auto` | 进程隔离，错误处理要解析 stdout |
| 部署依赖 | `pip install mineru` + 首次自动下载模型（GB 级） | 内网环境需手动准备模型缓存 |
| Office 转换依赖 | 系统级 `libreoffice` CLI | Windows 需独立安装 LibreOffice |
| OCR 后端 | MinerU 内置，不需要单独装 PaddleOCR | 简化部署 |
| GPU 需求 | 强烈推荐（CPU 模式慢 5-10x） | 影响并发上限和首次入库耗时 |
| Windows 兼容 | 代码层面有 `subprocess.CREATE_NO_WINDOW` 处理 | 原生支持 |

**核心来源**：`raganything/parser.py:712-810` — `_run_mineru_command()` 实现

### 11.5 关于"以图搜图 / 文字搜图"的辨析

RAG-Anything 的所谓"多模态检索"本质上是**伪图像检索**：

```
图片 → VLM（GPT-4o）生成文字描述 → 文本嵌入 → 文本向量检索
```

这条链路解决的是"图片内容能被语义查询命中"，但**不是真正的视觉检索**。

**真正的以图搜图 / 文字搜图**需要：

```
图片 → CLIP/SigLIP/BGE-VL 视觉编码器 → 视觉向量 → 视觉向量检索
                ↑
                同一向量空间
                ↓
查询文本 → CLIP 文本编码器 → 文本向量
```

CLIP 类模型把图像和文本嵌入到**同一个向量空间**，文本和图像可直接比较。这是 OpenAI CLIP 2021 论文的核心贡献，RAG-Anything 的 VLM 路线完全是另一条技术路径。

**对 UltraRAG 的启示**：

- RAG-Anything 的 VLM 多模态层**对未来"以图搜图"帮助不大**
- 真正的准备工作是在解析阶段**保留稳定的图片 ID 和位置信息**，未来 CLIP 可以重新嵌入
- chunk schema 中 `images[]` 字段要规范化（path / caption / page_idx / img_id）

---

## 十二、Phase 4 设计决策（已确认）

### 12.1 三个关键决策（用户拍板）

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 通用文档解析器 | **MinerU（PDF/图片）+ Docling（DOCX）** | MinerU 处理 PDF/扫描件最强；通用 DOCX 走 Docling 避免 PDF 中转有损 |
| Phase 4 范围 | **解析层扩格式 + heading_path 嵌入增强** | 实打实提升解析覆盖和检索准确率；约 2-3 周 |
| KB 类型暴露方式 | **KB 创建时下拉选择，事后不可改** | 明确稳妥；"SOP 知识库"和"通用知识库"作为两种产品形态 |

### 12.2 双轨解析架构

```
                  ┌─────────────────────────────┐
                  │   KB 创建时选 type          │
                  │   sop_docx / general        │
                  └──────────┬──────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │                              │
        sop_docx 路由                   general 路由
              │                              │
   ┌──────────▼─────────┐         ┌─────────▼──────────────┐
   │ docx_parser.py     │         │  Parser 工厂分发        │
   │ (现有，保留)         │         │  .docx → docling       │
   │ STEP+Heading 分块  │         │  .pdf  → mineru        │
   │                    │         │  .png/.jpg → mineru OCR │
   │                    │         │  .md → 简易 parser      │
   └──────────┬─────────┘         └─────────┬──────────────┘
              │                              │
              └──────────────┬──────────────┘
                             │
                  统一 chunks.jsonl schema
                             │
                  ┌──────────▼──────────────┐
                  │  通用 chunker            │
                  │  在 content_list 上      │
                  │  重建 heading tree       │
                  │  (SOP 路径下额外跑 STEP) │
                  └──────────┬──────────────┘
                             │
                  ┌──────────▼──────────────┐
                  │  增强嵌入                │
                  │  embed("heading_path +   │
                  │         title + content")│
                  └──────────┬──────────────┘
                             │
                  ┌──────────▼──────────────┐
                  │  FAISS（不变）           │
                  │  + 本地 reranker         │
                  │  bge-reranker-v2-m3      │
                  └─────────────────────────┘
```

**架构核心特征**：

- **解析层"双轨"** — SOP 业务精度不让步，通用能力同时具备
- **schema 统一** — 嵌入/索引/检索下游零改动
- **为未来 CLIP 留口子** — `images[].img_id` 字段稳定
- **增量价值** — 即使只做"两路解析 + heading_path 嵌入"，已能显著提升

### 12.3 统一 chunks.jsonl Schema 升级

```json
{
  "id": "doc_stem_chunk_001",
  "title": "...",
  "contents": "...",
  "doc": "原始文件名",
  "kb_id": "agv_demo",

  "source_type": "sop_docx | general_pdf | image | markdown",
  "parser": "docx_parser | mineru | docling",

  "structure": {
    "heading_path": ["第3章 故障处理", "3.2 电池告警"],
    "heading_level": 2,
    "step_number": 5,
    "page_idx": 12
  },

  "images": [
    {"path": "...", "caption": "...", "page_idx": 12, "img_id": "..."}
  ],

  "tables": [
    {"markdown": "| ... |", "caption": "...", "page_idx": 12}
  ]
}
```

**字段设计要点**：

- `structure.heading_path` — 数组形式标题层级链，检索时可做"父级标题加权"
- `structure.step_number` — 仅 SOP 文档有，保留现有 STEP 扩展逻辑
- `images[].img_id` — 稳定 ID，为未来 CLIP 视觉索引埋钩子
- `parser` — 记录解析来源，便于调试和回归追踪

### 12.4 检索准确率提升的杠杆排序

| 杠杆 | 改动量 | 预期收益 | Phase 4 是否纳入 |
| --- | --- | --- | --- |
| ① 解析层扩格式 | 大 | 让原本搜不到的文档能搜到 | **纳入** |
| ② heading_path 参与嵌入 | 小 | 标题上下文增强语义，命中率明显提升 | **纳入** |
| ③ 混合检索（BM25 + 向量） | 中 | 专有名词类查询大幅改善 | 视精力（Phase 4.5 候选） |
| ④ Query rewrite 增强（multi-query） | 中 | 一个问题改写成 3 个并行检索 | 视精力 |
| ⑤ 重排序模型 | — | **已落地**，本地 `bge-reranker-v2-m3` | 已完成 |

### 12.5 重排序层现状

本地已部署 `bge-reranker-v2-m3`，封装在 `custom_app/utils/local_reranker.py`：

- **模型路径**：`C:\reranker\bge-reranker-v2-m3`（本地离线，不依赖云端）
- **运行模式**：GPU + FP16（CPU 自动降级 FP32）
- **典型流程**：向量检索 Top 30/50 → `LocalReranker.rerank()` → Top 3/5 → LLM 生成
- **单例模式**：`get_default_reranker()` 模块级缓存，避免重复加载
- **API 入口**：
  - `rerank(query, documents, top_k, min_score)` — 纯文本列表打分
  - `rerank_items(query, items, content_key, ...)` — 带 metadata 的对象打分，保留原字段并追加 score/rank

**Phase 4 影响**：检索链路末端的 rerank 步骤已就绪，可直接对接新解析层产出的 chunks。

---

## 十三、待对齐事项（执行 /plan 前）

正式拆解 Phase 4 任务前，以下 4 点需要用户拍板：

### 13.1 现有库的迁移策略

`agv_demo` 和 `ifs_docs` 两个库的 schema 升级方案：

- **A. 软迁移** — 标记为 `type=sop`，schema 升级时给老 chunk 补默认字段（`heading_path=[]`, `page_idx=null`），**不重新解析**
- **B. 硬迁移** — 标记为 `type=sop`，**全部重新解析入库**，保证 schema 一致
- **C. 双 schema** — 老库保持原样，仅新建库走新 schema

### 13.2 MinerU / Docling 的依赖方式

- 默认依赖 vs 可选依赖（`uv sync --extras parsing`）
- 部署环境是否有 GPU
- 内网/离线部署时的模型分发方案

### 13.3 heading_path 拼接策略

- **A. 自然语言路径** — `"故障处理 > 电池告警\n<title>\n<contents>"`
- **B. 结构化标签** — `"[H1: 故障处理] [H2: 电池告警] <title>\n<contents>"`

需要在嵌入维度做小规模 A/B 验证。

### 13.4 KB type 字段落地

- `custom_app/db.py` 的 `kbs` 表需要 `ALTER TABLE ADD COLUMN type TEXT`
- 前端 KB 创建对话框增加下拉选择
- 后端 `api/kb.py` 创建逻辑校验 type 合法值
- 是否需要写专门的 migration 脚本

---

## 十四、Phase 5 预告（存储栈迁移）

> **状态**：概要锚点 — 详细方案见 [`../Phase5/PHASE5_OUTLINE.md`](../Phase5/PHASE5_OUTLINE.md)

### 14.1 Phase 5 范围

| 当前 | 迁移目标 | 优先级 | 时机 |
| --- | --- | --- | --- |
| FAISS（内存加载） | **Qdrant**（已在局域网部署） | **P0 必做** | Phase 5.1 |
| SQLite（`db/app.sqlite`） | **PostgreSQL**（已在局域网部署） | **P0 必做** | Phase 5.1 |
| SQLite KG 两表 | Neo4j | **P1 视需求** | Phase 5.2（待 Agent 多跳推理需求验证后启动） |

### 14.2 Phase 4 已为 Phase 5 预留的接口

- **VectorStore Protocol** —— Phase 5 加 `QdrantVectorStore` 实现即可，RagRunner 不动
- **`chunks.jsonl` 加 `vector_id` 字段** —— Phase 5 迁移时填 Qdrant point id
- **`kb_documents.file_type` 字段** —— 迁移 Postgres 时 1:1 复制

### 14.3 为什么不在 Phase 4 里做

- 解析层是上游，存储层是下游 → 上游不稳，下游再好也搜不出
- Phase 4 完成后立刻看到解析覆盖和准确率提升 → ROI 立竿见影
- Phase 5 是性能/扩展投资 → 当前 FAISS 还没到瓶颈时先做收益不明显
- 存储迁移单独立项 → 可做并行运行 + 灰度切换，降低风险

### 14.4 路线图

```text
Phase 4 (本期, 2-3 周)
    ↓ 预留 VectorStore Protocol + vector_id 字段
Phase 5.1 (后续, 2-3 周): Qdrant + PostgreSQL
    ↓
Phase 5.2 (按需, 1-1.5 周): Neo4j
    ↓
Phase 6 (规划中, 1-2 周): BM25 混合检索 + Query 增强
```

---

> **本文档作为 Phase 4 设计基线**。后续若 RAG-Anything / MinerU 行为发生变化（如版本升级），相关章节需同步更新。
