/**
 * P-Perm Commit 5：管理后台用户 CRUD + 角色绑定 API 客户端。
 *
 * 鉴权方式：
 *   - 登录用户名为 admin：浏览器 Cookie 自带 Flask session，无需额外头
 *   - 仅配置 ULTRARAG_ADMIN_TOKEN 而未登录：附带 X-Admin-Token
 *   - 两种方式都不在前端持久化敏感凭据
 */

const BASE = '/api/admin/users'
const ADMIN_TOKEN_HEADER = 'X-Admin-Token'

function getStoredAdminToken() {
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

function jsonHeaders() {
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' }
  const token = getStoredAdminToken()
  if (token) headers[ADMIN_TOKEN_HEADER] = token
  return headers
}

function getHeaders() {
  const headers = { Accept: 'application/json' }
  const token = getStoredAdminToken()
  if (token) headers[ADMIN_TOKEN_HEADER] = token
  return headers
}

async function readError(resp) {
  try {
    const body = await resp.json()
    return body.error || body.message || `HTTP ${resp.status}`
  } catch {
    return `HTTP ${resp.status}`
  }
}

async function jsonOrThrow(resp) {
  if (!resp.ok) {
    const msg = await readError(resp)
    const err = new Error(msg)
    err.status = resp.status
    throw err
  }
  return resp.json()
}

export async function listUsers() {
  const resp = await fetch(BASE, {
    headers: getHeaders(), credentials: 'same-origin',
  })
  const body = await jsonOrThrow(resp)
  return body.data?.items ?? []
}

export async function createUser({ username, password, displayName = '' }) {
  const resp = await fetch(BASE, {
    method: 'POST', headers: jsonHeaders(), credentials: 'same-origin',
    body: JSON.stringify({
      username, password, display_name: displayName,
    }),
  })
  const body = await jsonOrThrow(resp)
  return body.data
}

export async function deleteUser(userId) {
  const resp = await fetch(`${BASE}/${encodeURIComponent(userId)}`, {
    method: 'DELETE', headers: getHeaders(), credentials: 'same-origin',
  })
  return jsonOrThrow(resp)
}

export async function resetPassword(userId, newPassword) {
  const resp = await fetch(
    `${BASE}/${encodeURIComponent(userId)}/password`,
    {
      method: 'POST', headers: jsonHeaders(), credentials: 'same-origin',
      body: JSON.stringify({ password: newPassword }),
    },
  )
  return jsonOrThrow(resp)
}

export async function setStatus(userId, status) {
  const resp = await fetch(
    `${BASE}/${encodeURIComponent(userId)}/status`,
    {
      method: 'POST', headers: jsonHeaders(), credentials: 'same-origin',
      body: JSON.stringify({ status }),
    },
  )
  return jsonOrThrow(resp)
}

export async function listUserRoles(userId) {
  const resp = await fetch(
    `${BASE}/${encodeURIComponent(userId)}/roles`,
    { headers: getHeaders(), credentials: 'same-origin' },
  )
  const body = await jsonOrThrow(resp)
  return body.data?.items ?? []
}

export async function assignRole(userId, roleId) {
  const resp = await fetch(
    `${BASE}/${encodeURIComponent(userId)}/roles`,
    {
      method: 'POST', headers: jsonHeaders(), credentials: 'same-origin',
      body: JSON.stringify({ role_id: roleId }),
    },
  )
  return jsonOrThrow(resp)
}

export async function revokeRole(userId, roleId) {
  const resp = await fetch(
    `${BASE}/${encodeURIComponent(userId)}/roles/${encodeURIComponent(roleId)}`,
    {
      method: 'DELETE', headers: getHeaders(), credentials: 'same-origin',
    },
  )
  return jsonOrThrow(resp)
}

/** 复用现有 /api/roles 取所有角色（用于绑定下拉框）。 */
export async function listAllRoles() {
  const resp = await fetch('/api/roles', {
    headers: getHeaders(), credentials: 'same-origin',
  })
  const body = await jsonOrThrow(resp)
  return body.data?.items ?? body.data ?? []
}
