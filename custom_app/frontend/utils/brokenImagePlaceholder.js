/**
 * 图片占位与延迟加载：失败占位、超大 base64 先轻量占位再赋真实 src，减轻主线程卡顿。
 */

/** 超过该解码后体积（字节）的 data URL 先显示加载占位，再在 idle 时赋真实 src */
export const LARGE_DATA_URL_THRESHOLD_BYTES = 1_000_000

let _deferApplySeq = 0

/**
 * 估算 data URL 解码后的字节数（base64 按 3/4；非 base64 按 URI 解码后 UTF-8 长度）。
 *
 * @param {string} dataUrl
 * @returns {number}
 */
export function estimateDataUrlDecodedBytes(dataUrl) {
  if (typeof dataUrl !== 'string' || !dataUrl.startsWith('data:')) return 0
  const comma = dataUrl.indexOf(',')
  if (comma === -1 || comma >= dataUrl.length - 1) return 0
  const header = dataUrl.slice(0, comma).toLowerCase()
  const payload = dataUrl.slice(comma + 1)
  if (header.includes(';base64') || /;base64(?:;|$)/.test(header)) {
    const clean = payload.replace(/\s/g, '')
    const pad = clean.endsWith('==') ? 2 : clean.endsWith('=') ? 1 : 0
    return Math.floor((clean.length * 3) / 4) - pad
  }
  try {
    return new TextEncoder().encode(decodeURIComponent(payload)).length
  } catch {
    return payload.length
  }
}

/**
 * 超大 inline 图占位：先让用户看到提示，再在空闲回调里挂上真实 data URL。
 */
export const LOADING_IMAGE_PLACEHOLDER_SVG =
  'data:image/svg+xml,' +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="128" height="96" viewBox="0 0 128 96">
      <rect fill="#f5f5f5" width="128" height="96" rx="4"/>
      <text x="64" y="54" text-anchor="middle" fill="#595959" font-size="11" font-family="system-ui,sans-serif">图片加载中</text>
    </svg>`,
  )

/**
 * 图片加载失败时使用的内联 SVG（data URL），避免依赖外链资源。
 */
export const BROKEN_IMAGE_PLACEHOLDER_SVG =
  'data:image/svg+xml,' +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="128" height="96" viewBox="0 0 128 96">
      <rect fill="#f0f0f0" width="128" height="96" rx="4"/>
      <text x="64" y="54" text-anchor="middle" fill="#8c8c8c" font-size="12" font-family="system-ui,sans-serif">加载失败</text>
    </svg>`,
  )

/**
 * 在 img 上绑定一次性 error：替换为占位图并标记 alt，避免反复触发。
 *
 * @param {HTMLImageElement} img
 */
export function bindImageErrorToPlaceholder(img) {
  if (!(img instanceof HTMLImageElement)) return
  if (img.dataset.placeholderBound === '1') return
  img.dataset.placeholderBound = '1'
  img.addEventListener(
    'error',
    () => {
      if (img.dataset.fallbackApplied === '1') return
      img.dataset.fallbackApplied = '1'
      img.removeAttribute('srcset')
      img.src = BROKEN_IMAGE_PLACEHOLDER_SVG
      if (!img.alt) img.alt = '图片加载失败'
    },
    { once: true },
  )
}

/**
 * @param {() => void} fn
 * @param {{ scheduleApply?: (cb: () => void) => void }} [options] 测试注入同步 schedule
 */
function scheduleIdle(fn, options = {}) {
  if (typeof options.scheduleApply === 'function') {
    options.scheduleApply(fn)
    return
  }
  if (typeof requestIdleCallback === 'function') {
    requestIdleCallback(() => fn(), { timeout: 2000 })
    return
  }
  setTimeout(fn, 0)
}

/**
 * 若当前 `src` 为超大 data URL，则先换「图片加载中」再在 idle 恢复真实地址（用于 Markdown / 缩略图）。
 *
 * @param {HTMLImageElement} img
 * @param {{ byteThreshold?: number, scheduleApply?: (cb: () => void) => void }} [options]
 */
export function deferOversizedDataUrlIfNeeded(img, options = {}) {
  if (!(img instanceof HTMLImageElement)) return
  const threshold = options.byteThreshold ?? LARGE_DATA_URL_THRESHOLD_BYTES
  const rawSrc = img.getAttribute('src')
  if (rawSrc == null || rawSrc === '') return
  if (!rawSrc.startsWith('data:')) return
  if (estimateDataUrlDecodedBytes(rawSrc) <= threshold) return
  if (img.dataset.deferredLargeSrc) return

  img.dataset.deferredLargeSrc = rawSrc
  img.src = LOADING_IMAGE_PLACEHOLDER_SVG

  scheduleIdle(() => {
    if (!img.isConnected) return
    const real = img.dataset.deferredLargeSrc
    if (!real) return
    delete img.dataset.deferredLargeSrc
    img.src = real
  }, options)
}

/**
 * Lightbox 等场景：为元素设置图片地址，超大 data URL 时先占位并取消过期的异步赋值。
 *
 * @param {HTMLImageElement} img
 * @param {string} url
 * @param {{ byteThreshold?: number, scheduleApply?: (cb: () => void) => void }} [options]
 */
export function applyImageSrcMaybeDeferred(img, url, options = {}) {
  if (!(img instanceof HTMLImageElement) || typeof url !== 'string' || !url) return
  const threshold = options.byteThreshold ?? LARGE_DATA_URL_THRESHOLD_BYTES
  if (!url.startsWith('data:') || estimateDataUrlDecodedBytes(url) <= threshold) {
    delete img.dataset.deferredPendingId
    delete img.dataset.deferredLargeSrc
    img.src = url
    return
  }

  const myId = String(++_deferApplySeq)
  img.dataset.deferredPendingId = myId
  img.dataset.deferredLargeSrc = url
  img.src = LOADING_IMAGE_PLACEHOLDER_SVG

  scheduleIdle(() => {
    if (!img.isConnected) return
    if (img.dataset.deferredPendingId !== myId) return
    delete img.dataset.deferredPendingId
    delete img.dataset.deferredLargeSrc
    img.src = url
  }, options)
}

/**
 * 从 img 取 Lightbox / 多图列表应使用的真实地址（延迟加载期间 src 可能仍是占位图）。
 *
 * @param {HTMLImageElement} img
 * @returns {string}
 */
export function getImageEffectiveSrc(img) {
  if (!(img instanceof HTMLImageElement)) return ''
  return img.dataset.deferredLargeSrc || img.getAttribute('src') || img.src || ''
}

/**
 * 为某条消息正文内的 Markdown 图片：超大 data URL 延迟赋值 + 绑定一次性 error → 占位图。
 *
 * @param {HTMLElement | null} contentRoot 通常为 `[data-role="message-content"]`
 * @param {{ byteThreshold?: number, scheduleApply?: (cb: () => void) => void }} [options]
 */
export function attachMarkdownImageFallbacks(contentRoot, options = {}) {
  if (!contentRoot) return
  contentRoot.querySelectorAll('img').forEach((node) => {
    if (node instanceof HTMLImageElement) {
      deferOversizedDataUrlIfNeeded(node, options)
      bindImageErrorToPlaceholder(node)
    }
  })
}
