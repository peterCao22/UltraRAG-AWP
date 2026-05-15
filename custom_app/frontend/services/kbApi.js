const DEFAULT_KB_ENDPOINT = '/api/kb'
const USABLE_STATUSES = new Set(['active', 'ready', 'indexed'])
const ADMIN_TOKEN_HEADER = 'X-Admin-Token'

/** 从浏览器存储读取管理 API 用 token（登录页写入；未配置时为空）。 */
export function getStoredAdminToken() {
  try {
    return (
      window.sessionStorage.getItem('ultrarag_admin_token') ||
      window.localStorage.getItem('ultrarag_admin_token') ||
      ''
    )
  } catch {
    return ''
  }
}

export function normalizeKnowledgeBase(raw = {}) {
  const kbId = raw.kb_id || raw.id || ''

  return {
    kb_id: kbId,
    id: kbId,
    name: raw.name || kbId || '未命名知识库',
    status: raw.status || '',
    type: raw.type || 'sop_docx',
    description: raw.description || '',
    created_at: raw.created_at || '',
    updated_at: raw.updated_at || '',
    last_indexed_at: raw.last_indexed_at || '',
    document_count: Number(raw.document_count) || 0,
  }
}

export function normalizeDocument(raw = {}) {
  return {
    doc_id: raw.doc_id || '',
    kb_id: raw.kb_id || '',
    file_name: raw.file_name || '',
    file_type: raw.file_type || '',
    file_path: raw.file_path || '',
    channel: raw.channel || '',
    status: raw.status || '',
    error_message: raw.error_message || '',
    // Phase 6.1
    chunk_count: Number(raw.chunk_count) || 0,
    processed_at: raw.processed_at || '',
    created_at: raw.created_at || '',
    updated_at: raw.updated_at || '',
  }
}

const EMPTY_DOC_SUMMARY = Object.freeze({
  pending: 0,
  parsing: 0,
  embedding: 0,
  indexing: 0,
  completed: 0,
  failed: 0,
  deleting: 0,
})

async function readApiError(response) {
  try {
    const body = await response.json()
    return body.message || body.error || `HTTP ${response.status}`
  } catch {
    return `HTTP ${response.status}`
  }
}

function adminHeaders(adminToken) {
  const token = adminToken ?? getStoredAdminToken()
  const headers = { Accept: 'application/json' }
  if (token) headers[ADMIN_TOKEN_HEADER] = token
  return headers
}

function jsonHeaders(adminToken) {
  return { ...adminHeaders(adminToken), 'Content-Type': 'application/json' }
}

/**
 * 对话页用：列出可用于问答的知识库（过滤状态）。
 *
 * 参数：
 *   options.endpoint      API 根路径，默认 `/api/kb`
 *   options.adminToken    可选，覆盖存储中的 token
 *   options.purpose       `'chat'`（默认）或 `'admin'`；admin 不过滤状态
 *
 * 返回：
 *   Promise<Array<normalizeKnowledgeBase 结果>>
 */
export async function listKnowledgeBases({
  endpoint = DEFAULT_KB_ENDPOINT,
  adminToken,
  purpose = 'chat',
} = {}) {
  const url = new URL(endpoint, window.location.origin)
  const response = await fetch(url.pathname + url.search, {
    headers: adminHeaders(adminToken),
  })

  if (!response.ok) {
    throw new Error(await readApiError(response))
  }

  const body = await response.json()
  const items = Array.isArray(body.data) ? body.data : []
  const normalized = items.map(normalizeKnowledgeBase)
  if (purpose === 'admin') {
    return normalized
  }
  return normalized.filter((kb) => USABLE_STATUSES.has(kb.status))
}

/**
 * 创建知识库。
 *
 * 参数：
 *   payload.kb_id / payload.name 必填
 *   payload.type   可选，"sop_docx"（默认）或 "general"；创建后不可改
 *   payload.description / payload.tenant_id 可选
 */
export async function createKnowledgeBase(payload, { adminToken } = {}) {
  const response = await fetch(DEFAULT_KB_ENDPOINT, {
    method: 'POST',
    headers: jsonHeaders(adminToken),
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * 获取单个知识库详情（含 document_count 等字段，不含列表过滤）。
 */
export async function getKnowledgeBase(kbId, { includeArchived = false, adminToken } = {}) {
  const url = new URL(`${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}`, window.location.origin)
  if (includeArchived) url.searchParams.set('include_archived', 'true')
  const response = await fetch(url.pathname + url.search, {
    headers: adminHeaders(adminToken),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return normalizeKnowledgeBase(body.data || {})
}

/**
 * 删除知识库（默认软删 archived；hard=true 时硬删）。
 */
export async function deleteKnowledgeBase(kbId, { hard = false, adminToken } = {}) {
  const url = new URL(`${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}`, window.location.origin)
  if (hard) url.searchParams.set('hard', 'true')
  const response = await fetch(url.pathname + url.search, {
    method: 'DELETE',
    headers: adminHeaders(adminToken),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * 文档列表（Phase 6.1：含 summary 派生字段）。
 *
 * 返回：{ documents: NormalizedDocument[], summary: { pending, parsing, ... } }
 */
export async function listDocuments(kbId, { adminToken } = {}) {
  const url = new URL(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents`,
    window.location.origin,
  )
  const response = await fetch(url.pathname + url.search, {
    headers: adminHeaders(adminToken),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  const data = body.data || {}
  // 兼容老格式：旧后端直接返回数组（如老的 e2e fixture）
  if (Array.isArray(data)) {
    return {
      documents: data.map(normalizeDocument),
      summary: { ...EMPTY_DOC_SUMMARY },
    }
  }
  const docs = Array.isArray(data.documents) ? data.documents.map(normalizeDocument) : []
  return {
    documents: docs,
    summary: { ...EMPTY_DOC_SUMMARY, ...(data.summary || {}) },
  }
}

/**
 * Phase 6.1：批量取指定 doc_ids 的最新状态（前端轮询用）。
 */
export async function batchDocumentStatus(kbId, docIds, { adminToken } = {}) {
  if (!Array.isArray(docIds) || docIds.length === 0) return []
  const url = new URL(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents/batch-status`,
    window.location.origin,
  )
  const response = await fetch(url.pathname + url.search, {
    method: 'POST',
    headers: jsonHeaders(adminToken),
    body: JSON.stringify({ doc_ids: docIds }),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  const items = Array.isArray(body.data) ? body.data : []
  return items.map((it) => ({
    doc_id: it.doc_id || '',
    status: it.status || '',
    error_message: it.error_message || '',
    chunk_count: Number(it.chunk_count) || 0,
    processed_at: it.processed_at || '',
    updated_at: it.updated_at || '',
  }))
}

/**
 * Phase 6.1：单文档失败重试。
 */
export async function retryDocument(kbId, docId, { adminToken } = {}) {
  const response = await fetch(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(docId)}/retry`,
    { method: 'POST', headers: jsonHeaders(adminToken) },
  )
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * Phase 6.2：单文档增量重建（不动其它文件）。
 */
export async function reindexDocument(kbId, docId, { adminToken } = {}) {
  const response = await fetch(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(docId)}/reindex`,
    { method: 'POST', headers: jsonHeaders(adminToken) },
  )
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * Phase 6.2：批量增量重建。
 */
export async function batchReindexDocuments(kbId, docIds, { adminToken } = {}) {
  const response = await fetch(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents/batch-reindex`,
    {
      method: 'POST',
      headers: jsonHeaders(adminToken),
      body: JSON.stringify({ doc_ids: docIds }),
    },
  )
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * Phase 6.1：取该文档的全部 chunk（详情面板用）。
 */
export async function listDocumentChunks(kbId, docId, { adminToken } = {}) {
  const url = new URL(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(docId)}/chunks`,
    window.location.origin,
  )
  const response = await fetch(url.pathname + url.search, {
    headers: adminHeaders(adminToken),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  const data = body.data || {}
  return {
    doc_id: data.doc_id || docId,
    doc_stem: data.doc_stem || '',
    chunks: Array.isArray(data.chunks) ? data.chunks : [],
  }
}

/**
 * 删除单条文档记录及 raw 下文件。
 */
export async function deleteDocument(kbId, docId, { adminToken } = {}) {
  const url = new URL(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents`,
    window.location.origin,
  )
  url.searchParams.set('doc_id', docId)
  const response = await fetch(url.pathname + url.search, {
    method: 'DELETE',
    headers: adminHeaders(adminToken),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024

/**
 * 上传文档（multipart，`files` 字段）；支持进度回调。
 */
export function uploadKbDocuments(kbId, files, { adminToken, onProgress } = {}) {
  const list = Array.from(files || [])
  for (const f of list) {
    if (f.size > MAX_UPLOAD_BYTES) {
      return Promise.reject(new Error(`文件超过 50MB 限制：${f.name}`))
    }
  }
  const fd = new FormData()
  for (const f of list) fd.append('files', f)

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const token = adminToken ?? getStoredAdminToken()
    xhr.open(
      'POST',
      `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/documents/upload`,
    )
    xhr.setRequestHeader('Accept', 'application/json')
    if (token) xhr.setRequestHeader(ADMIN_TOKEN_HEADER, token)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && typeof onProgress === 'function') {
        onProgress(e.loaded / e.total)
      }
    }
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText || '{}')
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body.data)
          return
        }
        reject(new Error(body.error || body.message || `HTTP ${xhr.status}`))
      } catch {
        reject(new Error(`HTTP ${xhr.status}`))
      }
    }
    xhr.onerror = () => reject(new Error('网络错误'))
    xhr.send(fd)
  })
}

/**
 * 触发入库 / 重建索引。
 */
export async function createIngestJob(
  kbId,
  { force_reindex = false, async: asyncMode = true, adminToken } = {},
) {
  const response = await fetch(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/ingest`,
    {
      method: 'POST',
      headers: jsonHeaders(adminToken),
      body: JSON.stringify({ force_reindex: force_reindex, async: asyncMode }),
    },
  )
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * 任务列表（含 payload/result/summary 装饰字段）。
 */
export async function listJobs(kbId, { limit = 50, offset = 0, adminToken } = {}) {
  const url = new URL(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/jobs`,
    window.location.origin,
  )
  url.searchParams.set('limit', String(limit))
  url.searchParams.set('offset', String(offset))
  const response = await fetch(url.pathname + url.search, {
    headers: adminHeaders(adminToken),
  })
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return Array.isArray(body.data) ? body.data : []
}

/**
 * 入库任务阶段进度（轮询用）。
 */
export async function getJobProgress(kbId, jobId, { adminToken } = {}) {
  const response = await fetch(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/jobs/${encodeURIComponent(jobId)}/progress`,
    { headers: adminHeaders(adminToken) },
  )
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * 读取某 KB 的 Agent 工具启用配置。
 * 响应包含 enabled_tools 和 all_tools（含 label / required 元数据）。
 */
export async function getAgentConfig(kbId, { adminToken } = {}) {
  const response = await fetch(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/agent_config`,
    { headers: adminHeaders(adminToken) },
  )
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}

/**
 * 更新某 KB 的 Agent 工具启用配置。
 * @param {string[]} enabledTools 启用工具 name 数组（必填项即使省略也会被服务端补回）。
 */
export async function updateAgentConfig(kbId, enabledTools, { adminToken } = {}) {
  const response = await fetch(
    `${DEFAULT_KB_ENDPOINT}/${encodeURIComponent(kbId)}/agent_config`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...adminHeaders(adminToken) },
      body: JSON.stringify({ enabled_tools: enabledTools }),
    },
  )
  if (!response.ok) throw new Error(await readApiError(response))
  const body = await response.json()
  return body.data
}
