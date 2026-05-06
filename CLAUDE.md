# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Environment Setup
- Install core dependencies: `uv sync`
- Install full dependencies: `uv sync --all-extras`
- Activate environment: `source .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\activate` (Windows)

### Running UltraRAG
- Run a pipeline: `ultrarag run <<pipelinepipeline_yaml_path>`
- Example (Say Hello): `ultrarag run examples/sayhello.yaml`
- Example (AGV RAG): `ultrarag run examples/agv_rag.yaml`

### Testing
- Run all tests: `pytest`
- Run a specific test file: `pytest tests/<<testtest_file>.py`

### Custom AGV App (Phase 1)
- Start the Flask backend: `python -m custom_app.app --port 8080`
- Rerank（CrossEncoder）：`servers/retriever/parameter.yaml` 中 `rag_rerank.enabled` 默认开启；首次加载模型可能较慢。纯测速或排障：`ULTRARAG_DISABLE_RERANK=1`；强制开启：`ULTRARAG_ENABLE_RERANK=1`。
- 生成模型：默认 `servers/generation/parameter.yaml` + OpenAI 兼容（vLLM）。`.env` 可用 `ULTRARAG_OPENAI_*` 覆盖地址/模型/超时。若使用 **Google Gemini API** 做文本生成：设 `ULTRARAG_CHAT_BACKEND=gemini` 与 `GOOGLE_API_KEY`（或 `ULTRARAG_GEMINI_API_KEY`），可选 `ULTRARAG_GEMINI_MODEL`（默认 `gemini-2.0-flash`）；yaml 根键 `chat_backend: gemini` 亦可。改后重启 Flask。
- 对话页若 URL 带 `#session=...`，加载时会按**该会话所属知识库**自动切换下拉框（与仅改本地记忆不同）；切换库请用下拉或「新建对话」。服务端日志会打 `chat_stream kb_id=...` 便于核对请求是否落在预期库。
- Run DOCX parser: `python custom_app/services/docx_parser.py --input data/agv_documents/ --kb_id agv_demo --output data/kb/agv_demo/`
- Phase 3 chat/admin pages load **marked** + **DOMPurify** from `frontend/vendor/` only (no Vue on `index.html` / `admin.html`); `npm run test:cov` covers `custom_app/frontend/`.

## Architecture Overview

UltraRAG is a lightweight RAG development framework based on the **Model Context Protocol (MCP)**.

### Core Components
- **MCP Servers**: Independent modular servers providing atomic RAG functions.
  - `servers/retriever`: Handles embedding and vector search (FAISS, Milvus, etc.).
  - `servers/generation`: Handles LLM generation (vLLM, OpenAI, etc.).
  - `servers/corpus`: Handles document parsing and chunking.
  - `servers/prompt`: Manages Jinja2 prompt templates.
- **MCP Client**: Orchestrates servers based on YAML pipeline configurations.
- **UltraRAG UI**: A visual IDE for pipeline construction and debugging.

### AGV Private Knowledge Base (Custom Implementation)
The `custom_app/` directory implements a specialized RAG system for industrial SOP documents:
- **Data Flow**: `DOCX` $\rightarrow$ `docx_parser.py` (extracts text/images) $\rightarrow$ `chunks.jsonl` $\rightarrow$ `retriever` (FAISS index) $\rightarrow$ `rag_runner.py` (orchestration) $\rightarrow$ `api/chat.py` (Flask API).
- **Image Handling**: Images are extracted from DOCX and linked to text chunks via paragraph indices, allowing the system to return relevant images alongside text answers without needing a VLM for retrieval.
- **Storage**: 
  - Knowledge base files: `data/kb/<<kbkb_id>/`
  - Metadata: `db/app.sqlite` (Phase 2+)

### Key Configuration Files
- `servers/<<modulemodule>/parameter.yaml`: Backend and model configurations for each MCP server.
- `examples/*.yaml`: Pipeline definitions for different RAG workflows.
- `prompt/*.jinja`: Prompt templates for the generation phase.
