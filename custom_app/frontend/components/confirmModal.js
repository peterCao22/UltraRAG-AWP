/**
 * 简易确认层：返回 Promise<boolean>，避免业务页散落 `window.confirm`。
 */

/**
 * 显示确认对话框。
 *
 * 参数：
 *   options.message       主文案（纯文本）
 *   options.confirmLabel  确认按钮文案，默认「确定」
 *   options.cancelLabel   取消按钮文案，默认「取消」
 *
 * 返回：
 *   Promise<boolean> — 点击确定为 true，取消或关闭为 false
 */
export function openConfirmModal({
  message,
  confirmLabel = '确定',
  cancelLabel = '取消',
} = {}) {
  return new Promise((resolve) => {
    const root = document.createElement('div')
    root.className = 'modal-overlay'
    root.setAttribute('role', 'dialog')
    root.setAttribute('aria-modal', 'true')

    const card = document.createElement('div')
    card.className = 'modal-card'

    const p = document.createElement('p')
    p.className = 'modal-message'
    p.textContent = message || ''

    const row = document.createElement('div')
    row.className = 'modal-actions'

    const btnCancel = document.createElement('button')
    btnCancel.type = 'button'
    btnCancel.className = 'button-secondary'
    btnCancel.textContent = cancelLabel

    const btnOk = document.createElement('button')
    btnOk.type = 'button'
    btnOk.className = 'button-primary'
    btnOk.textContent = confirmLabel

    function cleanup(result) {
      root.remove()
      document.removeEventListener('keydown', onKey)
      resolve(result)
    }

    function onKey(e) {
      if (e.key === 'Escape') cleanup(false)
    }

    btnCancel.addEventListener('click', () => cleanup(false))
    btnOk.addEventListener('click', () => cleanup(true))
    root.addEventListener('click', (e) => {
      if (e.target === root) cleanup(false)
    })

    row.append(btnCancel, btnOk)
    card.append(p, row)
    root.append(card)
    document.body.append(root)
    document.addEventListener('keydown', onKey)
    btnOk.focus()
  })
}
