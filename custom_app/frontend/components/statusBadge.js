/**
 * 知识库 / 文档状态徽章：纯 DOM，无外部依赖。
 */

const LABELS = {
  active: '可用',
  ready: '就绪',
  indexed: '已索引',
  pending: '待处理',
  uploaded: '已上传',
  running: '运行中',
  building: '构建中',
  failed: '失败',
  error: '错误',
  archived: '已归档',
  cancelled: '已取消',
  success: '成功',
}

/**
 * 将后端 status 映射为展示用短文案。
 *
 * 参数：
 *   status — 原始状态字符串
 *
 * 返回：
 *   str — 中文或原文回退
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
  if (['indexed', 'ready', 'active', 'success'].includes(s)) return 'status-badge--ok'
  if (['failed', 'error'].includes(s)) return 'status-badge--err'
  if (['running', 'pending', 'building', 'uploaded', 'cancelled'].includes(s)) {
    return 'status-badge--pending'
  }
  if (s === 'archived') return 'status-badge--muted'
  return 'status-badge--neutral'
}

/**
 * 创建 `<span class="status-badge ...">` 元素。
 */
export function createStatusBadge(status) {
  const el = document.createElement('span')
  el.className = `status-badge ${getStatusBadgeModifier(status)}`
  el.textContent = formatStatusLabel(status)
  return el
}
