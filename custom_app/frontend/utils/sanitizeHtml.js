/**
 * 将 HTML 字符串交给 DOMPurify 过滤后再写入 DOM（与清单「innerHTML 须 sanitize」一致）。
 * 管理页在 `admin.html` 中先于模块脚本加载 `vendor/DOMPurify.min.js`，运行时必有全局 `DOMPurify`。
 */

/**
 * @param {string} html
 * @returns {string}
 */
export function sanitizeHtml(html) {
  const raw = String(html ?? '')
  const purify = typeof globalThis !== 'undefined' ? globalThis.DOMPurify : undefined
  if (purify && typeof purify.sanitize === 'function') {
    return purify.sanitize(raw, { USE_PROFILES: { html: true } })
  }
  return raw
}
