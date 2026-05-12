import { beforeEach, describe, expect, it } from 'vitest'

import { buildSourcesPanel, createSourceCardElement } from '../components/sourcePanel.js'

describe('createSourceCardElement', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('renders title and excerpt with blue-bar card class', () => {
    const el = createSourceCardElement({
      display_title: 'SOP A',
      excerpt: '第一段说明文字',
    })
    document.body.append(el)
    expect(el.dataset.role).toBe('source-card')
    expect(el.querySelector('.source-card__title').textContent).toBe('SOP A')
    expect(el.querySelector('.source-card__excerpt').textContent).toBe('第一段说明文字')
  })

  it('prefers snippet over excerpt and falls back title', () => {
    const el = createSourceCardElement({ title: 'T', snippet: '短摘要' })
    expect(el.querySelector('.source-card__title').textContent).toBe('T')
    expect(el.querySelector('.source-card__excerpt').textContent).toBe('短摘要')
  })

  it('renders thumbnail row for images', () => {
    const el = createSourceCardElement({
      title: 'X',
      excerpt: 'e',
      images: ['data:image/png;base64,abc', 'data:image/png;base64,def'],
    })
    const row = el.querySelector('[data-role="source-card-thumbs"]')
    expect(row).toBeTruthy()
    expect(row.querySelectorAll('img')).toHaveLength(2)
    expect(row.querySelector('img').width).toBe(128)
  })

  it('replaces broken thumbnail with placeholder on error', () => {
    const el = createSourceCardElement({
      title: 'X',
      excerpt: 'e',
      images: ['https://invalid.invalid/broken.png'],
    })
    const img = el.querySelector('img.source-card__thumb')
    expect(img).toBeTruthy()
    img.dispatchEvent(new Event('error'))
    expect(img.src.startsWith('data:image/svg+xml')).toBe(true)
  })
})

describe('buildSourcesPanel', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('returns null for empty or non-array sources', () => {
    expect(buildSourcesPanel([])).toBeNull()
    expect(buildSourcesPanel(null)).toBeNull()
    expect(buildSourcesPanel([null, false])).toBeNull()
  })

  it('creates collapsed toggle with count and expands on click', () => {
    const panel = buildSourcesPanel([
      { title: 'A', excerpt: 'a1' },
      { title: 'B', excerpt: 'b1' },
    ])
    document.body.append(panel)

    const toggle = panel.querySelector('[data-role="source-panel-toggle"]')
    const body = panel.querySelector('[data-role="source-panel-body"]')
    expect(toggle.textContent).toContain('引用来源（2）')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(body.hidden).toBe(true)
    expect(panel.querySelectorAll('[data-role="source-card"]')).toHaveLength(2)

    toggle.click()
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(body.hidden).toBe(false)
    expect(toggle.textContent).toContain('收起')

    toggle.click()
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(body.hidden).toBe(true)
  })
})
