# Phase 3 API 对接设计

> **终端**：接口与 PC/手机无关；前端需在窄屏下保持相同调用方式，仅调整布局与交互（见 `03` §0）。

## 后端 API 汇总（前端视角）

### 1. 对话接口

#### `POST /api/chat`

**请求：**
```json
{
  "question": "AGV 换电步骤是什么？",
  "kb_id": "agv_demo",
  "stream": true,
  "agent_mode": "quick"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 用户问题 |
| `kb_id` | string | 否 | 知识库 ID，默认由前端或后端约定 |
| `stream` | boolean | 否 | 是否 SSE；默认 `true` |
| `agent_mode` | string | 否 | **`quick`**：当前单轮 RAG（等价 WeKnora「快速问答」）。**`agent`**：多轮智能推理（WeKnora「智能推理」语义）；**后端未实现前见下文「降级约定」**。 |

**产品约定（前端实现用）：**

- 切换 `agent_mode` **不自动清空**当前对话历史；**新建对话**清空消息列表（与切换 `kb_id` 清空历史区分：`kb_id` 变更仍建议清空历史）。
- 内置选项第一版：**快速问答**（`quick`）、**智能推理**（`agent`）。**数据分析师**等扩展预留：可增加 `agent_id`（可选，与 `agent_mode` 二选一或并存，以后续定稿为准；当前可不传）。

---

##### 智能推理与 SSE 扩展（契约优先，后端可滞后）

以下事件在 **`agent_mode === "quick"`** 时与历史行为一致，可不发送扩展类型。  
当 **`agent_mode === "agent"`** 且服务端**尚未**实现真 Agent 时，采用 **「降级约定」**，避免前端白等或报错。

**降级约定（推荐，后端实现 Agent 之前）：**

1. 服务端仍返回 **HTTP 200** + `text/event-stream`（与现网一致）。
2. 流首条（或首条有效业务事件前）发送一条 **`meta`**，声明实际使用的模式，例如：  
   `data: {"type":"meta","agent_mode_requested":"agent","effective_agent_mode":"quick","degraded":true,"message":"智能推理尚未启用，已使用快速问答。"}`
3. 随后按 **`effective_agent_mode`** 走现有 `chunk` / `sources` / `done` 流。

前端：若 `degraded === true`，对 `message` **Toast 一次**（避免每条 chunk 重复提示）。

**未来「真 · 智能推理」时建议增加的 SSE 类型（供 UI 步骤条 / 思考过程）：**

| `type` | 用途 | 载荷示例字段 |
|--------|------|----------------|
| `meta` | 会话元信息、降级说明、版本号 | `effective_agent_mode`, `degraded`, `message` |
| `thought` | 模型思考过程（可流式拼接） | `content`, `done?` |
| `tool_start` | 某步工具开始 | `tool_id`, `name`, `hint`（对用户友好文案，非内部函数名） |
| `tool_result` | 工具结束 | `tool_id`, `success`, `summary`（短摘要，避免把原始大 JSON 灌进 UI） |
| `chunk` | 最终回答正文增量 | `content`（与现有一致） |
| `sources` | 引用来源 | `sources`（与现有一致） |
| `done` | 结束 | 可带 `step_count`、`duration_ms` 等可选统计 |

事件顺序不要求固定到每一条，但 **`done` 必须为最后一条业务结束信号**（与现网一致）。

---

**响应（SSE 流）— 快速问答最小示例：**
```
data: {"type": "chunk", "content": "换电"}

data: {"type": "chunk", "content": "操作步骤"}

data: {"type": "sources", "sources": [
  {
    "title": "3.2 换电步骤",
    "snippet": "...将 AGV 停至指定换电位...",
    "images": ["data:image/png;base64,iVBORw0..."]
  }
]}

data: {"type": "done"}
```

**前端处理逻辑：**
```js
// services/chatApi.js
export async function sendChatMessage({
  kbId,
  question,
  agentMode = 'quick', // 'quick' | 'agent'
  onMeta,
  onThought,
  onToolStart,
  onToolResult,
  onChunk,
  onSources,
  onDone,
  onError,
}) {
  let answer = ''
  let degradedToastShown = false

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kb_id: kbId,
        question,
        stream: true,
        agent_mode: agentMode,
      }),
    })
    
    if (!response.ok) {
      const err = await response.json()
      onError(err.message || '请求失败')
      return
    }
    
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()  // 保留未完成的行
      
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const payload = line.slice(6).trim()
        if (!payload || payload === '[DONE]') continue
        
        try {
          const data = JSON.parse(payload)
          if (data.type === 'meta') {
            onMeta?.(data)
            if (data.degraded && data.message && !degradedToastShown) {
              degradedToastShown = true
              // Toast.show(data.message, 'info')
            }
          } else if (data.type === 'thought') {
            onThought?.(data)
          } else if (data.type === 'tool_start') {
            onToolStart?.(data)
          } else if (data.type === 'tool_result') {
            onToolResult?.(data)
          } else if (data.type === 'chunk') {
            answer += data.content
            onChunk(data.content, answer)
          } else if (data.type === 'sources') {
            onSources(data.sources)
          } else if (data.type === 'done') {
            onDone(answer)
          }
        } catch (e) {
          console.warn('SSE parse error:', line)
        }
      }
    }
  } catch (err) {
    onError(err.message || '网络错误，请检查连接')
  }
}
```

**与总体方案的关系：** 智能推理在 UltraRAG 中的分层实现与 WeKnora 对照见仓库根文档 [WeKnora智能推理与UltraRAG移植指南.md](../WeKnora智能推理与UltraRAG移植指南.md)；本节为 **前后端对接契约**，实现顺序可：先 UI + 请求字段 + 解析 `meta` 降级，再迭代真 `agent` 流。

---

### 2. 知识库接口

#### `GET /api/kb/` — 获取知识库列表

**响应：**
```json
{
  "success": true,
  "data": [
    {
      "id": "agv_demo",
      "name": "AGV 操作手册",
      "kb_type": "操作手册",
      "status": "ready",
      "doc_count": 12,
      "created_at": "2026-04-01T10:00:00"
    }
  ]
}
```

#### `POST /api/kb/` — 创建知识库

**请求：**
```json
{
  "name": "维修规程 2024",
  "kb_type": "维护规程",
  "description": "AGV 维修操作规程"
}
```

**响应：**
```json
{
  "success": true,
  "data": { "id": "uuid-xxx", "name": "维修规程 2024", "status": "building" }
}
```

#### `GET /api/kb/<id>` — 知识库详情 + 文档列表

**响应：**
```json
{
  "success": true,
  "data": {
    "id": "agv_demo",
    "name": "AGV 操作手册",
    "status": "ready",
    "doc_count": 12,
    "chunk_count": 342,
    "documents": [
      {
        "id": "doc-001",
        "filename": "BatterySOP.docx",
        "file_size": 2145280,
        "chunk_count": 45,
        "status": "done",
        "uploaded_at": "2026-04-01T09:30:00"
      }
    ]
  }
}
```

#### `DELETE /api/kb/<id>` — 删除知识库

**响应：**
```json
{ "success": true, "message": "知识库已删除" }
```

#### `POST /api/kb/<id>/documents` — 上传文档

**请求：** `multipart/form-data`，字段名 `file`（支持多文件）

**响应：**
```json
{
  "success": true,
  "data": {
    "queued": ["BatterySOP.docx"],
    "message": "文件已加入处理队列"
  }
}
```

#### `DELETE /api/kb/<id>/documents/<doc_id>` — 删除文档

**响应：**
```json
{ "success": true, "message": "文档已删除，索引重建已触发" }
```

#### `GET /api/kb/<id>/status` — 查询索引状态

**响应：**
```json
{
  "success": true,
  "data": {
    "kb_status": "building",
    "documents": [
      { "id": "doc-003", "filename": "PartsManual.pdf", "status": "indexing", "progress": 0.6 }
    ]
  }
}
```

---

## 前端错误处理规范

### 统一错误格式

所有后端 API 错误响应遵循：
```json
{
  "success": false,
  "message": "知识库不存在",
  "code": "KB_NOT_FOUND"   // 可选，便于前端差异化处理
}
```

### 前端错误处理策略

```js
// services/apiClient.js
async function request(url, options = {}) {
  try {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options
    })
    
    const data = await response.json()
    
    if (!response.ok || !data.success) {
      throw new ApiError(data.message || `HTTP ${response.status}`, response.status)
    }
    
    return data.data
  } catch (err) {
    if (err instanceof ApiError) throw err
    throw new ApiError('网络连接失败，请检查网络设置')
  }
}

class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}
```

### 错误码 → 用户提示映射

| HTTP 状态码 | 提示文字 |
|------------|---------|
| 400 | 参数错误：{message} |
| 404 | 资源不存在 |
| 409 | 操作冲突（如索引构建中，禁止删除） |
| 500 | 服务器内部错误，请联系管理员 |
| 网络错误 | 网络连接失败，请检查网络设置 |

---

## 文件上传进度跟踪

使用 `XMLHttpRequest` 替代 `fetch`（fetch 不支持上传进度）：

```js
export function uploadDocument(kbId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const form = new FormData()
    form.append('file', file)
    
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) {
        onProgress(Math.round(e.loaded / e.total * 100))
      }
    })
    
    xhr.addEventListener('load', () => {
      const data = JSON.parse(xhr.responseText)
      if (data.success) resolve(data.data)
      else reject(new Error(data.message))
    })
    
    xhr.addEventListener('error', () => reject(new Error('上传失败')))
    
    xhr.open('POST', `/api/kb/${kbId}/documents`)
    xhr.send(form)
  })
}
```

---

## 轮询策略（索引状态）

```js
// 轮询知识库状态，直到所有文档完成
export function pollKbStatus(kbId, onUpdate, intervalMs = 3000) {
  const timer = setInterval(async () => {
    try {
      const status = await kbApi.getStatus(kbId)
      onUpdate(status)
      
      // 所有文档都处于终态（done/error），停止轮询
      const allDone = status.documents.every(d => ['done', 'error'].includes(d.status))
      if (allDone) clearInterval(timer)
    } catch (e) {
      // 轮询错误不弹 toast，静默处理，下次继续
      console.error('Status poll error:', e)
    }
  }, intervalMs)
  
  return () => clearInterval(timer)  // 返回清理函数
}
```
