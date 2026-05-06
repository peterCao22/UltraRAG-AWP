/**
 * 全屏图片 Lightbox：引用区缩略图点击放大，多图左右切换，ESC / 背景 / 关闭钮关闭。
 */

import { applyImageSrcMaybeDeferred, getImageEffectiveSrc } from '../utils/brokenImagePlaceholder.js'

let overlayEl = null
/** @type {((e: KeyboardEvent) => void) | null} */
let keyHandler = null

export function closeLightbox() {
  if (keyHandler) {
    document.removeEventListener('keydown', keyHandler)
    keyHandler = null
  }
  if (overlayEl) {
    overlayEl.remove()
    overlayEl = null
  }
}

/**
 * @param {string[]} urls 图片 URL 列表（通常为 data URL）
 * @param {number} startIndex 起始下标
 */
export function openLightbox(urls, startIndex = 0) {
  const list = (urls || []).filter(Boolean)
  if (!list.length) return

  closeLightbox()

  let idx = Math.min(Math.max(0, startIndex), list.length - 1)

  const overlay = document.createElement('div')
  overlay.className = 'image-lightbox'
  overlay.dataset.role = 'image-lightbox'

  const backdrop = document.createElement('div')
  backdrop.className = 'image-lightbox-backdrop'
  backdrop.addEventListener('click', () => closeLightbox())

  const panel = document.createElement('div')
  panel.className = 'image-lightbox-panel'

  const closeBtn = document.createElement('button')
  closeBtn.type = 'button'
  closeBtn.className = 'image-lightbox-close'
  closeBtn.setAttribute('aria-label', '关闭')
  closeBtn.textContent = '×'
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation()
    closeLightbox()
  })

  const img = document.createElement('img')
  img.className = 'image-lightbox-img'
  img.alt = '放大预览'

  const nav = document.createElement('div')
  nav.className = 'image-lightbox-nav'

  const prevBtn = document.createElement('button')
  prevBtn.type = 'button'
  prevBtn.className = 'image-lightbox-navbtn'
  prevBtn.setAttribute('aria-label', '上一张')
  prevBtn.textContent = '‹'

  const label = document.createElement('span')
  label.className = 'image-lightbox-counter'

  const nextBtn = document.createElement('button')
  nextBtn.type = 'button'
  nextBtn.className = 'image-lightbox-navbtn'
  nextBtn.setAttribute('aria-label', '下一张')
  nextBtn.textContent = '›'

  function syncCounter() {
    label.textContent = `${idx + 1} / ${list.length}`
  }

  function show() {
    applyImageSrcMaybeDeferred(img, list[idx])
    syncCounter()
    prevBtn.hidden = list.length < 2
    nextBtn.hidden = list.length < 2
    label.hidden = list.length < 2
  }

  prevBtn.addEventListener('click', (e) => {
    e.stopPropagation()
    idx = (idx - 1 + list.length) % list.length
    show()
  })
  nextBtn.addEventListener('click', (e) => {
    e.stopPropagation()
    idx = (idx + 1) % list.length
    show()
  })

  keyHandler = (e) => {
    if (e.key === 'Escape') {
      closeLightbox()
      return
    }
    if (list.length < 2) return
    if (e.key === 'ArrowLeft') {
      idx = (idx - 1 + list.length) % list.length
      show()
    } else if (e.key === 'ArrowRight') {
      idx = (idx + 1) % list.length
      show()
    }
  }
  document.addEventListener('keydown', keyHandler)

  nav.append(prevBtn, label, nextBtn)
  panel.append(closeBtn, img, nav)
  overlay.append(backdrop, panel)
  document.body.append(overlay)
  overlayEl = overlay

  show()
}

/**
 * 在消息列表上事件委托：引用区缩略图、主回答 Markdown 内嵌图片均可点开 Lightbox。
 *
 * @param {HTMLElement} root 通常为 `data-role="message-list"`
 */
export function bindChatImageLightbox(root) {
  if (!root) return
  root.addEventListener('click', (e) => {
    const t = e.target
    if (!(t instanceof Element)) return

    const thumb = t.closest('.source-card__thumb')
    if (thumb && root.contains(thumb)) {
      const panel = thumb.closest('[data-role="source-panel"]')
      if (!panel) return
      const thumbs = [...panel.querySelectorAll('.source-card__thumb')]
      const urls = thumbs.map((el) => (el instanceof HTMLImageElement ? getImageEffectiveSrc(el) : ''))
      const i = thumbs.indexOf(thumb)
      openLightbox(urls, Math.max(0, i))
      return
    }

    if (t instanceof HTMLImageElement && t.closest('[data-role="message-content"]')) {
      const box = t.closest('[data-role="message-content"]')
      if (!box || !root.contains(box)) return
      const imgs = [...box.querySelectorAll('img')]
      const urls = imgs.map((im) => getImageEffectiveSrc(im)).filter(Boolean)
      const idx = urls.indexOf(getImageEffectiveSrc(t))
      openLightbox(urls, Math.max(0, idx))
    }
  })
}

/** @deprecated 使用 {@link bindChatImageLightbox} */
export const bindSourceThumbnailLightbox = bindChatImageLightbox
