/**
 * 知识库 / 文档状态徽章：纯 DOM，无外部依赖。
 *
 * Phase 6.1 扩展：
 *   - 文档状态新增 parsing / embedding / indexing / completed / deleting；
 *     processing 类带 spinner（纯 CSS @keyframes）。
 *   - createStatusBadge 接受 options.title 用于 hover tooltip（显示错误信息）。
 */

const LABELS = {
  // KB & job
  active: '可用',
  ready: '就绪',
  indexed: '已索引',
  uploaded: '已上传',
  running: '运行中',
  building: '构建中',
  archived: '已归档',
  cancelled: '已取消',
  success: '成功',
  error: '错误',
  // 文档状态
  pending: '待处理',
  parsing: '解析中…',
  embedding: '嵌入中…',
  indexing: '写入索引…',
  completed: '已完成',
  failed: '失败',
  deleting: '删除中',
}

const PROCESSING_STATUSES = new Set(['parsing', 'embedding', 'indexing', 'running', 'building'])

/**
 * 将后端 status 映射为展示用短文案。
 */
export function formatStatusLabel(status) {
  const key = String(status || '').toLowerCase()
  return LABELS[key] || status || '未知'
}

/**
 * 返回徽章根节点的 CSS 修饰类名（不含基础类 `status-badge`）。
 */
export function getStatusBadgeModifier(status) {
  const s = String(status || '').toLowerCase()
  if (['indexed', 'ready', 'active', 'success', 'completed'].includes(s)) {
    return 'status-badge--ok'
  }
  if (['failed', 'error'].includes(s)) return 'status-badge--err'
  if (PROCESSING_STATUSES.has(s)) return 'status-badge--processing'
  if (['pending', 'uploaded', 'cancelled', 'deleting'].includes(s)) {
    return 'status-badge--pending'
  }
  if (s === 'archived') return 'status-badge--muted'
  return 'status-badge--neutral'
}

/**
 * 创建 `<span class="status-badge ...">` 元素。
 *
 * @param {string} status
 * @param {{ title?: string }} [options]
 */
export function createStatusBadge(status, options = {}) {
  const s = String(status || '').toLowerCase()
  const el = document.createElement('span')
  el.className = `status-badge ${getStatusBadgeModifier(s)}`
  if (PROCESSING_STATUSES.has(s)) {
    const spinner = document.createElement('span')
    spinner.className = 'status-spinner'
    spinner.setAttribute('aria-hidden', 'true')
    el.append(spinner)
  }
  const label = document.createElement('span')
  label.textContent = formatStatusLabel(s)
  el.append(label)
  if (options && options.title) {
    el.title = options.title
  }
  return el
}
