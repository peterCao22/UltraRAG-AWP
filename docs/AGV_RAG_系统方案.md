# 基于 UltraRAG 的 AGV 私有知识库 RAG 系统方案

> 版本：v1.1（对齐当前实现） · 日期：2026-03-31  
> 目标：以 UltraRAG 为 RAG 内核，定制开发一套适合**工业操作文档**（DOCX/PDF）的私有知识问答系统。

---

## 1. 整体架构

```text
┌─────────────────────────────────────────────────────────┐
│                    用户浏览器（H5）                      │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  问答对话页  │  │  知识库管理  │  │  用户/权限    │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │
└─────────┼─────────────────┼──────────────────┼──────────┘
          │  HTTP/SSE        │ REST              │ REST
┌─────────▼───────────────────────────────────────────────┐
│               定制后端 (Flask)                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  对话 API    │  │  知识库 API  │  │  用户/鉴权API │  │
│  │  /api/chat   │  │  /api/kb/*   │  │  /api/user/*  │  │
│  │  Phase1:已实现│ │  Phase2:规划中│ │  Phase4:规划中 │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │
│         │                  │                  │          │
│  ┌──────▼──────────────────▼──────────────────▼───────┐  │
│  │ custom_app/app.py + custom_app/api/chat.py         │  │
│  │ custom_app/services/rag_runner.py                  │  │
│  │ Google query embedding + FAISS 检索 + vLLM 生成      │  │
│  └─────────────────────────────────────────────────────┘  │
└─────────┬───────────────────────────────────────────────┘
          │  当前最小闭环以文件与本地索引为主；KB/User API 按阶段推进
┌─────────▼───────────────────────────────────────────────┐
│                UltraRAG MCP Servers                      │
│  ┌─────────┐ ┌────────────┐ ┌──────────┐ ┌──────────┐  │
│  │ corpus  │ │ retriever  │ │  prompt  │ │generation│  │
│  │(解析/分 │ │(Embedding/ │ │(Jinja2  │ │(vLLM/    │  │
│  │  块/导图)│ │ FAISS索引) │ │ 模板)   │ │ OpenAI兼│  │
│  └─────────┘ └────────────┘ └──────────┘ └──────────┘  │
└─────────────────────────────────────────────────────────┘
          │
┌─────────▼───────────────────────────────────────────────┐
│                     数据存储                             │
│  data/                                                  │
│    kb/<kb_id>/raw/       ← 原始上传文件(DOCX/PDF等)     │
│    kb/<kb_id>/corpora/   ← 切块后文本 chunks.jsonl      │
│    kb/<kb_id>/images/    ← 从 DOCX 导出的嵌入图片       │
│    kb/<kb_id>/index/     ← FAISS 向量索引               │
│  db/                                                    │
│    app.sqlite            ← 用户/权限/知识库元数据（Phase2启用） │
└─────────────────────────────────────────────────────────┘
```

> 对齐说明（v1.1）：当前已完成最小问答闭环，`db/app.sqlite` 尚未接入运行链路，按阶段规划在 Phase 2 进入。

---

## 2. 技术选型

| 模块 | 选型 | 理由 |
|------|------|------|
| RAG 核心 | **UltraRAG 0.3.x**（MCP 架构） | 即项目本体，无需重造 |
| 文本 Embedding | Google `gemini-embedding-001`（原生 API）| 已与当前索引/检索链路一致（768 维） |
| 向量索引 | **FAISS**（本地文件，`index.index`） | 无需额外服务，单机够用 |
| 生成模型 | 内部 **gpt-oss-120b vLLM** → OpenAI 兼容接口 | UltraRAG `openai` backend 直接对接 |
| 后端框架 | **Flask**（UltraRAG UI 已用，复用依赖）| 一致性好 |
| 数据库 | **SQLite**（用户/知识库元数据）| 当前为预留，Phase 2 启用 |
| 前端 | 纯 **H5 + Vanilla JS**（或 Vue3 CDN 版） | 无构建依赖，易嵌入内网 |
| 文档解析 | `python-docx` + 自定义 DOCX 图片导出 | 段落/表格/图片三合一 |

---

## 3. 阶段规划

### Phase 1 — 核心 RAG 流程（已最小跑通）

> 状态：已完成最小闭环（DOCX 入库 -> Google Embedding -> FAISS -> `/api/chat`）。
> 补充：当前默认英文问答（语料主语言为英文）；保留 `sources` 用于审计引用。

### Phase 2 — 知识库管理 + 多库支持（下一阶段）

> 目标：引入 `db/app.sqlite`，管理 KB 元数据、文档记录、索引状态；支持多知识库隔离。

### Phase 3 — H5 前端（并轨）

> 目标：对接 `/api/chat` 与知识库管理接口，形成可用对话 UI + 管理页。

### Phase 4 — 用户与权限

> 目标：账号、角色（管理员/普通用户）与 KB 可见性控制，完成平台化收口。

---

## 4. Phase 1：核心 RAG 流程详解

### 4.1 文档入库管线（自定义 → 再调 UltraRAG）

```
上传 DOCX
    │
    ▼
step1_parse_docx()          ← 自定义：python-docx 解析
    ├─ 抽取 段落/章节文字     → raw_paragraphs.jsonl
    ├─ 表格 → "cell1 | cell2" 文本行，随段落存入 JSONL
    └─ 嵌入图片 → 导出为 PNG  → kb/<id>/images/<doc>/<n>.png
           └─ 每张图记录所在段落索引（para_idx）

    │
    ▼
step2_chunk()               ← UltraRAG corpus.chunk_documents
    │  输入: raw_paragraphs.jsonl  chunk_size=512
    │  输出: chunks.jsonl
    │  每条 chunk 结构：
    │    { "id": "AGV_SOP_023",
    │      "title": "3.2 换电步骤",
    │      "contents": "...",
    │      "images": ["images/BatterySOP/3.png"]  }  ← 自定义追加字段
    │
    ▼
step3_embed_and_index()     ← UltraRAG retriever.retriever_init +
                               retriever.retriever_embed +
                               retriever.retriever_index
    输出: index/index.index + embedding/embedding.npy
```

**DOCX 图片关联规则（关键设计）：**  
- 导出图片时，记录图片在文档 XML 中所属的段落序号 `para_idx`。  
- 分块时，每个 chunk 记录它覆盖的段落范围 `[start_para, end_para]`。  
- 若某张图的 `para_idx` 落在该范围内，该图路径写入 chunk 的 `images` 字段。  
- 结果：每条检索 chunk 自带 0-N 张关联图的相对路径，无需 CLIP。

### 4.2 问答流程

```
用户输入: "AGV 换电操作的注意事项"
    │
    ▼
retriever_search(q, top_k=5)
    │ 返回: [ {id, title, contents, images, score}, ... ]
    │
    ▼
prompt.qa_rag_boxed(question, passages)     ← UltraRAG Jinja2 模板
    │ 组装: [检索到的段落文本] + 用户问题
    │
    ▼
generation.generate()       ← 调 vLLM gpt-oss-120b (openai backend)
    │ 返回: answer 文本
    │
    ▼
后端响应组装:
    {
      "answer": "换电时须先断开主电源...",
      "sources": [
        { "title": "3.2 换电步骤",
          "snippet": "...",
          "images": [ "data:image/png;base64,..." ]  ← 在此转 base64
        }
      ]
    }
    │
    ▼
H5 前端渲染：文字 + <img src="data:image/png;base64,...">
```

### 4.3 生成模型配置（`servers/generation/parameter.yaml` 修改要点）

```yaml
backend: openai
backend_configs:
  openai:
    model_name: gpt-oss-120b       # vLLM 注册的模型名称
    base_url: http://<内网IP>:<端口>/v1
    api_key: "internal"            # vLLM 不做鉴权时随意填写
    concurrency: 4
    retries: 3
    base_delay: 1.0

sampling_params:
  temperature: 0.3                 # SOP 类文档推荐低温度
  top_p: 0.9
  max_tokens: 2048

system_prompt: |
  你是一名 AGV 维护专家助手，请根据提供的操作文档片段，用中文简洁准确地回答问题。
  如果文档中没有相关信息，请明确说明"文档中未找到相关内容"，不要编造。
```

### 4.4 Embedding 模型配置（`servers/retriever/parameter.yaml` 修改要点）

```yaml
model_name_or_path: BAAI/bge-m3     # 支持中英文，本地私有化
backend: sentence_transformers
backend_configs:
  sentence_transformers:
    trust_remote_code: true
    sentence_transformers_encode:
      normalize_embeddings: true
      encode_chunk_size: 256

index_backend: faiss
index_backend_configs:
  faiss:
    index_use_gpu: false            # CPU 模式，无需 GPU
    index_chunk_size: 10000
    index_path: data/kb/<kb_id>/index/index.index

corpus_path: data/kb/<kb_id>/corpora/chunks.jsonl
embedding_path: data/kb/<kb_id>/embedding/embedding.npy
top_k: 5
```

### 4.5 AGV RAG Pipeline YAML（`examples/agv_rag.yaml`）

```yaml
# AGV 知识库 RAG 问答 Pipeline

servers:
  retriever: servers/retriever
  prompt:    servers/prompt
  generation: servers/generation

pipeline:
- retriever.retriever_search
- prompt.qa_rag_boxed
- generation.generate
```

> 入库阶段单独跑：`ultrarag run examples/agv_index.yaml`  
> 问答阶段：通过后端 API 调用或 `ultrarag run examples/agv_rag.yaml`

---

## 5. Phase 2：多知识库管理

### 5.1 知识库数据模型（SQLite）

```sql
-- 知识库
CREATE TABLE knowledge_base (
    id          TEXT PRIMARY KEY,   -- uuid
    name        TEXT NOT NULL,
    description TEXT,
    kb_type     TEXT,               -- 如 "操作手册"/"维护规程"/"备件目录"
    owner_id    INTEGER,
    status      TEXT DEFAULT 'building', -- building / ready / error
    doc_count   INTEGER DEFAULT 0,
    created_at  DATETIME,
    updated_at  DATETIME
);

-- 文档
CREATE TABLE document (
    id          TEXT PRIMARY KEY,
    kb_id       TEXT REFERENCES knowledge_base(id),
    filename    TEXT,
    file_type   TEXT,               -- docx / pdf / txt
    file_size   INTEGER,
    chunk_count INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'pending', -- pending / indexing / done / error
    error_msg   TEXT,
    uploaded_at DATETIME
);
```

### 5.2 知识库 API 设计

| Method | Path | 说明 |
|--------|------|------|
| `GET`  | `/api/kb/` | 列出当前用户可见知识库 |
| `POST` | `/api/kb/` | 创建知识库（name, type, description）|
| `GET`  | `/api/kb/<id>` | 知识库详情 + 文档列表 |
| `DELETE` | `/api/kb/<id>` | 删除（同时清理索引文件）|
| `POST` | `/api/kb/<id>/documents` | 上传文档（支持多文件）|
| `DELETE` | `/api/kb/<id>/documents/<doc_id>` | 删除文档并重建索引 |
| `POST` | `/api/kb/<id>/rebuild` | 手动触发全量重建索引 |
| `GET`  | `/api/kb/<id>/status` | 索引构建进度（SSE 推流）|

---

## 6. Phase 3：H5 前端

### 6.1 页面结构

```
/                     → 重定向到 /chat
/login                → 登录页
/chat                 → 对话主页（选知识库 + 问答）
/admin/               → 管理后台入口（管理员专用）
/admin/kb             → 知识库列表与管理
/admin/kb/<id>        → 知识库文档管理 + 索引状态
/admin/users          → 用户管理（仅超级管理员）
```

### 6.2 对话页核心交互

```
┌────────────────────────────────────────────────────────┐
│ [选择知识库 ▼]  AGV 操作手册         [新建对话]        │
├────────────────────────────────────────────────────────┤
│                                                        │
│  AI: 您好，请问有什么可以帮助您？                       │
│                                                        │
│  用户: AGV 换电步骤是什么？                             │
│                                                        │
│  AI: 换电操作步骤如下：                                │
│      1. 进入换电区域前须先...                           │
│      ┌──────────────────────────────────────────────┐ │
│      │ 📄 来源：3.2 换电步骤                         │ │
│      │ ···将 AGV 停至指定换电位···                   │ │
│      │ [📷 相关图片]                                  │ │
│      │   <图片 1>   <图片 2>                         │ │
│      └──────────────────────────────────────────────┘ │
│                                                        │
├────────────────────────────────────────────────────────┤
│ [输入您的问题...]                            [发送 ▶]  │
└────────────────────────────────────────────────────────┘
```

### 6.3 前端关键技术点

- **流式输出**：用 `EventSource` 接收 SSE，逐字展示模型回答。  
- **图片展示**：响应 JSON 里 `sources[].images[]` 为 `data:image/png;base64,...`，直接 `<img src>` 即可。  
- **知识库切换**：切换知识库时，下一次 `/api/chat` 请求带入新的 `kb_id`，后端加载对应索引。  
- **Markdown 渲染**：引入 `marked.js`（CDN），将模型输出里的 Markdown 表格 `| col1 | col2 |` 渲染为 HTML table。

---

## 7. Phase 4：用户与权限

### 7.1 用户模型（SQLite）

```sql
CREATE TABLE user (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,        -- bcrypt
    role        TEXT DEFAULT 'user',   -- user / admin / superadmin
    is_active   INTEGER DEFAULT 1,
    created_at  DATETIME
);

-- 知识库与用户的可见性关联
CREATE TABLE kb_permission (
    kb_id   TEXT REFERENCES knowledge_base(id),
    user_id INTEGER REFERENCES user(id),
    PRIMARY KEY (kb_id, user_id)
);
```

### 7.2 角色权限矩阵

| 操作 | 普通用户 | 管理员 | 超级管理员 |
|------|----------|--------|----------|
| 对话（已分配知识库） | ✅ | ✅ | ✅ |
| 查看知识库列表 | 仅可见库 | 所有库 | 所有库 |
| 创建/删除知识库 | ❌ | ✅（自己的）| ✅ |
| 上传/删除文档 | ❌ | ✅ | ✅ |
| 管理用户 | ❌ | ❌ | ✅ |
| 分配知识库权限 | ❌ | ✅ | ✅ |

### 7.3 认证方式

- Session Cookie（Flask-Login 风格）或 **JWT**（适合前后端分离）。  
- 密码用 **bcrypt** 存储，初始超级管理员账号由环境变量注入。  
- 无需 OAuth/LDAP，内网简单部署。

---

## 8. 目录结构规划

```
UltraRAG/
├── servers/                     # UltraRAG 原有，不动
├── src/ultrarag/                # UltraRAG 原有，不动
├── examples/
│   ├── agv_rag.yaml             # 新增：问答 pipeline
│   └── agv_index.yaml           # 新增：建索引 pipeline
├── prompt/
│   └── agv_qa_rag.jinja         # 新增：AGV 专用提示模板（可选）
├── custom_app/                  # ★ 新增：定制后端
│   ├── __init__.py
│   ├── app.py                   # Flask 主入口
│   ├── api/
│   │   ├── chat.py              # 问答接口
│   │   ├── kb.py                # 知识库管理
│   │   └── user.py              # 用户/权限
│   ├── services/
│   │   ├── docx_parser.py       # DOCX 解析 + 图片导出
│   │   ├── kb_manager.py        # 知识库生命周期
│   │   └── rag_runner.py        # 调用 UltraRAG pipeline
│   ├── models/
│   │   └── db.py                # SQLite 模型（SQLAlchemy）
│   └── frontend/                # ★ 新增：H5 前端
│       ├── index.html           # 对话页
│       ├── admin.html           # 管理后台
│       ├── login.html
│       ├── main.js
│       └── style.css
├── data/
│   └── kb/                      # 知识库数据根目录
│       └── <kb_id>/
│           ├── raw/             # 原始文件
│           ├── corpora/         # chunks.jsonl
│           ├── images/          # 导出图片
│           ├── embedding/       # embedding.npy
│           └── index/           # index.index
├── db/
│   └── app.sqlite               # 元数据
└── docs/
    └── AGV_RAG_系统方案.md       # 本文档
```

---

## 9. 核心流程启动步骤（Phase 1 落地顺序）

### 步骤 1：验证 vLLM 接口可用

```bash
curl http://<内网IP>:<端口>/v1/models
# 确认模型名称与 parameter.yaml 中 model_name 一致
```

### 步骤 2：安装依赖（已有 uv 环境下）

```bash
uv sync --extra retriever   # 安装 embedding 相关
# python-docx 已在核心依赖中，无需额外安装
```

### 步骤 3：放入样本文档

```
data/agv_documents/BatteryChangeSequenceSOP.docx  ← 已有
```

### 步骤 4：运行 DOCX 入库脚本（自定义，Phase 1 开发）

```bash
python custom_app/services/docx_parser.py \
  --input  data/agv_documents/ \
  --kb_id  agv_demo \
  --output data/kb/agv_demo/
# 产出:
#   data/kb/agv_demo/corpora/chunks.jsonl
#   data/kb/agv_demo/images/...
```

### 步骤 5：用 UltraRAG 建向量索引

```bash
# 修改 servers/retriever/parameter.yaml 中路径后：
ultrarag run examples/agv_index.yaml
# 产出:
#   data/kb/agv_demo/index/index.index
#   data/kb/agv_demo/embedding/embedding.npy
```

### 步骤 6：跑一次命令行问答验证

```bash
# 临时测试，确认检索和生成都工作
python -c "
import asyncio
from ultrarag.api import run_pipeline
# ... 调用 agv_rag.yaml pipeline，传入问题
"
```

### 步骤 7：启动定制后端

```bash
python -m custom_app.app --port 8080
# 打开 http://localhost:8080
```

---

## 10. 风险与注意事项

| 风险 | 应对 |
|------|------|
| DOCX 图片与段落对应关系不精确 | 对照 XML `<w:drawing>` 节点，按 `w:p` 段落索引精确绑定；先人工抽查 5 份文件验证 |
| 大文档分块后图片关联丢失 | chunk 元数据保留 `source_para_range`，图片按范围匹配，不丢 |
| vLLM 接口网络不稳 | UltraRAG `openai` backend 已内置 `retries`/`base_delay`，按需调大 |
| FAISS 索引体积（文档量大时）| 先跑 100 份，评估索引大小；超过 10GB 再评估换 Milvus |
| 多用户并发问答 | Flask 多线程模式；UltraRAG retriever_search 为 async，注意事件循环隔离 |
| 图片 base64 响应体过大 | Phase 1 先用，Phase 2 改为返图片 URL，前端按需加载 |

---

## 11. 下一步（Phase 1 开发任务列表）

1. **编写 `custom_app/services/docx_parser.py`**  
   - 用 `python-docx` 解析段落、表格、嵌入图（`doc.inline_shapes`）  
   - 导出图片到 `data/kb/<id>/images/`，生成带 `images` 字段的 `chunks.jsonl`

2. **改写 `servers/generation/parameter.yaml`**（填入内部 vLLM 地址）

3. **改写 `servers/retriever/parameter.yaml`**（填入 `BAAI/bge-m3` 路径、kb 路径）

4. **新增 `examples/agv_index.yaml`**（三步建索引）

5. **新增 `examples/agv_rag.yaml`**（三步问答）

6. **编写 `custom_app/services/rag_runner.py`**  
   - 封装"检索 → 组 prompt → 调 vLLM → 拼 base64 图"的完整流程

7. **编写最小问答 API `custom_app/api/chat.py`**（验证全链路通）

---

> 待 Phase 1 核心链路验证通后，再依次推进 Phase 2（多库管理）→ Phase 3（H5 前端）→ Phase 4（权限）。
