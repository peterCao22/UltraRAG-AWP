/**
 * P-Perm C7.3：角色管理 API 客户端（CRUD + KB 权限）。
 *
 * 鉴权：与 usersApi 一致 — admin cookie 自动带；可选 X-Admin-Token 兜底。
 */

const ROLES_BASE = '/api/roles'
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

function headers(extra = {}) {
  const h = { Accept: 'application/json', ...extra }
  const token = getStoredAdminToken()
  if (token) h[ADMIN_TOKEN_HEADER] = token
  return h
}

function jsonHeaders() {
  return headers({ 'Content-Type': 'application/json' })
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

export async function listRoles() {
  const resp = await fetch(ROLES_BASE, {
    headers: headers(), credentials: 'same-origin',
  })
  const body = await jsonOrThrow(resp)
  return body.data ?? []
}

export async function createRole({ name, description = '' }) {
  const resp = await fetch(ROLES_BASE, {
    method: 'POST', headers: jsonHeaders(), credentials: 'same-origin',
    body: JSON.stringify({ name, description }),
  })
  const body = await jsonOrThrow(resp)
  return body.data
}

export async function deleteRole(roleId) {
  const resp = await fetch(
    `${ROLES_BASE}/${encodeURIComponent(roleId)}`,
    { method: 'DELETE', headers: headers(), credentials: 'same-origin' },
  )
  return jsonOrThrow(resp)
}

export async function listRolePermissions(roleId) {
  const resp = await fetch(
    `${ROLES_BASE}/${encodeURIComponent(roleId)}/permissions`,
    { headers: headers(), credentials: 'same-origin' },
  )
  const body = await jsonOrThrow(resp)
  return body.data ?? []
}

export async function assignKbPermission(roleId, kbId, accessLevel = 'read') {
  const resp = await fetch(
    `${ROLES_BASE}/${encodeURIComponent(roleId)}/permissions`,
    {
      method: 'POST', headers: jsonHeaders(), credentials: 'same-origin',
      body: JSON.stringify({ kb_id: kbId, access_level: accessLevel }),
    },
  )
  return jsonOrThrow(resp)
}

export async function revokeKbPermission(roleId, kbId) {
  const resp = await fetch(
    `${ROLES_BASE}/${encodeURIComponent(roleId)}/permissions/${encodeURIComponent(kbId)}`,
    { method: 'DELETE', headers: headers(), credentials: 'same-origin' },
  )
  return jsonOrThrow(resp)
}
