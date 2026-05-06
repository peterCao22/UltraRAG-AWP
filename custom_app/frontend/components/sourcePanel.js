/**
 * 对话页「引用来源」折叠面板（Sprint 2 P0）。
 * 纯 DOM 构建，便于 Vitest + happy-dom 单测；文本一律 textContent，避免 XSS。
 *
 * 缩略图尺寸与 style.css 中 --source-thumb-* 保持一致（约 128×96），便于辨认又不过大。
 */
import { bindImageErrorToPlaceholder, deferOversizedDataUrlIfNeeded } from '../utils/brokenImagePlaceholder.js'

const SOURCE_THUMB_WIDTH = 128
const SOURCE_THUMB_HEIGHT = 96

/**
 * 单条来源卡片 DOM（左侧强调条 + 标题 + 摘要 + 可选缩略图行）。
 *
 * @param {Record<string, unknown>} src 后端 sources 单项（title / display_title / snippet / excerpt / images）
 * @returns {HTMLElement}
 */
export function createSourceCardElement(src) {
  const card = document.createElement('article')
  card.className = 'source-card'
  card.dataset.role = 'source-card'

  const titleText = String(src.display_title || src.title || '(untitled)')
  const heading = document.createElement('h3')
  heading.className = 'source-card__title'
  heading.textContent = titleText

  const raw = String(src.snippet || src.excerpt || '').trim()
  const excerpt = document.createElement('p')
  excerpt.className = 'source-card__excerpt'
  excerpt.textContent = raw

  card.append(heading, excerpt)

  const images = Array.isArray(src.images) ? src.images.filter(Boolean) : []
  if (images.length) {
    const row = document.createElement('div')
    row.className = 'source-card__thumbs'
    row.dataset.role = 'source-card-thumbs'
    for (const url of images) {
      const img = document.createElement('img')
      img.className = 'source-card__thumb'
      img.src = String(url)
      img.alt = ''
      img.title = '点击查看大图'
      img.width = SOURCE_THUMB_WIDTH
      img.height = SOURCE_THUMB_HEIGHT
      img.loading = 'lazy'
      deferOversizedDataUrlIfNeeded(img)
      bindImageErrorToPlaceholder(img)
      row.append(img)
    }
    card.append(row)
  }

  return card
}

/**
 * 构建整块引用区域：默认折叠，点击展开全部 SourceCard。
 *
 * @param {unknown[]} sources 后端返回的 sources 数组
 * @returns {HTMLElement | null} 无有效项时返回 null
 */
export function buildSourcesPanel(sources) {
  const list = Array.isArray(sources) ? sources.filter((s) => s && typeof s === 'object') : []
  if (!list.length) return null

  const section = document.createElement('section')
  section.className = 'source-panel'
  section.dataset.role = 'source-panel'

  const toggle = document.createElement('button')
  toggle.type = 'button'
  toggle.className = 'source-panel-toggle'
  toggle.dataset.role = 'source-panel-toggle'
  toggle.setAttribute('aria-expanded', 'false')
  const n = list.length
  toggle.textContent = `📄 引用来源（${n} 处）▾`

  const body = document.createElement('div')
  body.className = 'source-panel-body'
  body.dataset.role = 'source-panel-body'
  body.hidden = true

  for (const src of list) {
    body.append(createSourceCardElement(src))
  }

  toggle.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') === 'true'
    const next = !open
    toggle.setAttribute('aria-expanded', String(next))
    body.hidden = !next
    toggle.textContent = next ? `📄 引用来源（${n} 处）▴` : `📄 引用来源（${n} 处）▾`
  })

  section.append(toggle, body)
  return section
}
