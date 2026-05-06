/**
 * Phase 1 会话 REST 封装（与 chat 流式 `session_id` 配套）。
 */

export async function createSession(kbId, { agentMode = 'quick', title = '' } = {}) {
  const r = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      kb_id: kbId,
      agent_mode: agentMode,
      title: title || '',
    }),
  })
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.success === false) {
    throw new Error(j.error || j.message || `HTTP ${r.status}`)
  }
  return j.data
}

export async function getSession(sessionId) {
  const id = encodeURIComponent(sessionId)
  const r = await fetch(`/api/sessions/${id}`)
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.success === false) {
    throw new Error(j.error || j.message || `HTTP ${r.status}`)
  }
  return j.data
}

export async function listSessions(kbId, { limit = 100 } = {}) {
  const q = new URLSearchParams({ kb_id: kbId, limit: String(limit) })
  const r = await fetch(`/api/sessions?${q}`)
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.success === false) {
    throw new Error(j.error || j.message || `HTTP ${r.status}`)
  }
  return j.data?.items ?? []
}

export async function fetchSessionMessages(sessionId) {
  const id = encodeURIComponent(sessionId)
  const r = await fetch(`/api/sessions/${id}/messages`)
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.success === false) {
    throw new Error(j.error || j.message || `HTTP ${r.status}`)
  }
  return j.data?.items ?? []
}

export async function deleteSession(sessionId) {
  const id = encodeURIComponent(sessionId)
  const r = await fetch(`/api/sessions/${id}`, { method: 'DELETE' })
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.success === false) {
    throw new Error(j.error || j.message || `HTTP ${r.status}`)
  }
  return j.data
}

export async function renameSession(sessionId, title) {
  const id = encodeURIComponent(sessionId)
  const r = await fetch(`/api/sessions/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.success === false) {
    throw new Error(j.error || j.message || `HTTP ${r.status}`)
  }
  return j.data
}
