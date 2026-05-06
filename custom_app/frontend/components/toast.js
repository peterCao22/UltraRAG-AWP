/**
 * 全局轻提示：固定右上角，自动消失；供对话页、管理页共用。
 */

const CONTAINER_ID = 'ultrarag-toast-root'
const DEFAULT_DURATION = { success: 3000, error: 5000, info: 4000 }

let containerEl = null

function ensureContainer() {
  if (containerEl && document.body.contains(containerEl)) return containerEl
  containerEl = document.getElementById(CONTAINER_ID)
  if (!containerEl) {
    containerEl = document.createElement('div')
    containerEl.id = CONTAINER_ID
    containerEl.className = 'toast-container'
    containerEl.setAttribute('aria-live', 'polite')
    document.body.append(containerEl)
  }
  return containerEl
}

/**
 * 显示一条 Toast。
 *
 * 参数：
 *   message — 纯文本（内部使用 textContent，不信任 HTML）
 *   kind     — `'success'` | `'error'` | `'info'`，默认 `'success'`
 *   durationMs — 毫秒；省略时用 kind 对应默认（success 3s / error 5s / info 4s）
 */
export function showToast(message, kind = 'success', durationMs) {
  const normalizedKind =
    kind === 'error' || kind === 'success' || kind === 'info' ? kind : 'success'
  const root = ensureContainer()
  const el = document.createElement('div')
  el.className = `toast toast--${normalizedKind}`
  el.setAttribute('role', 'status')
  el.textContent = String(message ?? '')

  const ms =
    typeof durationMs === 'number' && durationMs > 0
      ? durationMs
      : DEFAULT_DURATION[normalizedKind] ?? DEFAULT_DURATION.success

  root.append(el)
  window.setTimeout(() => {
    el.classList.add('toast--out')
    window.setTimeout(() => el.remove(), 200)
  }, ms)
}

/** 与清单命名一致：`Toast.show('msg', 'success')` */
export const Toast = { show: showToast }
