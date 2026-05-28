/**
 * Phase 12.1 指代消解提示横幅。
 *
 * 后端 SSE `reference_resolution` 事件命中（applied=true）时，在对话气泡顶部
 * 渲染一行灰色提示，告诉用户系统把"它/第 2 个/继续"理解成了什么。
 *
 * 输出结构（纯 DOM，textContent 防 XSS）：
 *
 *   <aside class="reference-resolution" data-role="reference-resolution">
 *     <div class="reference-resolution__row">
 *       <span class="reference-resolution__icon">↳</span>
 *       <span class="reference-resolution__label">系统理解为：</span>
 *       <span class="reference-resolution__rewritten">急停按钮如何检查</span>
 *       <span class="reference-resolution__confidence" title="置信度">0.92</span>
 *     </div>
 *     <details class="reference-resolution__details">
 *       <summary>不是这意思？点这里修正</summary>
 *       <div>...修正 UI（未来扩展）...</div>
 *     </details>
 *   </aside>
 */

/**
 * @typedef {Object} ReferenceResolutionEvent
 * @property {boolean} applied
 * @property {string} original_query
 * @property {string} rewritten_query
 * @property {number} confidence
 * @property {Array<{reference: string, meaning: string}>} resolved
 * @property {number} ms
 * @property {string=} model
 */

/**
 * 构造指代消解横幅。
 * @param {ReferenceResolutionEvent} ev
 * @returns {HTMLElement|null}
 */
export function buildReferenceResolutionBanner(ev) {
  if (!ev || !ev.applied) return null
  const rewritten = String(ev.rewritten_query || '').trim()
  if (!rewritten) return null

  const root = document.createElement('aside')
  root.className = 'reference-resolution'
  root.dataset.role = 'reference-resolution'

  const row = document.createElement('div')
  row.className = 'reference-resolution__row'

  const icon = document.createElement('span')
  icon.className = 'reference-resolution__icon'
  icon.setAttribute('aria-hidden', 'true')
  icon.textContent = '↳'

  const label = document.createElement('span')
  label.className = 'reference-resolution__label'
  label.textContent = '系统理解为：'

  const rewrittenEl = document.createElement('span')
  rewrittenEl.className = 'reference-resolution__rewritten'
  rewrittenEl.textContent = rewritten

  row.append(icon, label, rewrittenEl)

  // 置信度小标（hover 显示完整模型 / 耗时）
  const conf = Number(ev.confidence)
  if (Number.isFinite(conf)) {
    const confEl = document.createElement('span')
    confEl.className = 'reference-resolution__confidence'
    const tipParts = [`置信度 ${conf.toFixed(2)}`]
    if (ev.model) tipParts.push(`模型 ${ev.model}`)
    if (Number.isFinite(ev.ms)) tipParts.push(`${ev.ms} ms`)
    confEl.title = tipParts.join(' · ')
    confEl.textContent = conf.toFixed(2)
    row.append(confEl)
  }

  root.append(row)

  // 详情区：列出每个 reference -> meaning + 修正入口
  const resolved = Array.isArray(ev.resolved) ? ev.resolved : []
  const details = document.createElement('details')
  details.className = 'reference-resolution__details'

  const summary = document.createElement('summary')
  summary.textContent = '不是这意思？点这里修正'
  details.append(summary)

  if (resolved.length) {
    const list = document.createElement('ul')
    list.className = 'reference-resolution__resolved-list'
    for (const item of resolved) {
      if (!item || typeof item !== 'object') continue
      const ref = String(item.reference || '').trim()
      const meaning = String(item.meaning || '').trim()
      if (!ref || !meaning) continue
      const li = document.createElement('li')
      const refEl = document.createElement('span')
      refEl.className = 'reference-resolution__ref'
      refEl.textContent = ref
      const arrow = document.createElement('span')
      arrow.className = 'reference-resolution__arrow'
      arrow.textContent = ' → '
      const meaningEl = document.createElement('span')
      meaningEl.className = 'reference-resolution__meaning'
      meaningEl.textContent = meaning
      li.append(refEl, arrow, meaningEl)
      list.append(li)
    }
    if (list.childElementCount > 0) {
      details.append(list)
    }
  }

  // 修正按钮（占位；W2 D2 接入真实重发逻辑）
  const correctBtn = document.createElement('button')
  correctBtn.type = 'button'
  correctBtn.className = 'reference-resolution__correct-btn'
  correctBtn.textContent = '我来手动改写'
  correctBtn.dataset.original = String(ev.original_query || '')
  correctBtn.dataset.rewritten = rewritten
  correctBtn.addEventListener('click', () => {
    // 派发自定义事件，让 main.js 决定如何处理（重新填回输入框 / 直接重发）
    root.dispatchEvent(
      new CustomEvent('reference-resolution-correct-request', {
        bubbles: true,
        detail: {
          original: String(ev.original_query || ''),
          rewritten,
        },
      }),
    )
  })
  details.append(correctBtn)

  root.append(details)

  return root
}
