import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  applyImageSrcMaybeDeferred,
  attachMarkdownImageFallbacks,
  bindImageErrorToPlaceholder,
  BROKEN_IMAGE_PLACEHOLDER_SVG,
  deferOversizedDataUrlIfNeeded,
  estimateDataUrlDecodedBytes,
  getImageEffectiveSrc,
  LOADING_IMAGE_PLACEHOLDER_SVG,
} from '../utils/brokenImagePlaceholder.js'

describe('brokenImagePlaceholder', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('replaces broken src with placeholder data URL on error', () => {
    const img = document.createElement('img')
    img.src = 'https://invalid.invalid/no-such-image.png'
    document.body.append(img)
    bindImageErrorToPlaceholder(img)
    img.dispatchEvent(new Event('error'))
    expect(img.src.startsWith('data:image/svg+xml')).toBe(true)
    expect(img.alt).toBe('图片加载失败')
  })

  it('attachMarkdownImageFallbacks binds all imgs under root', () => {
    const wrap = document.createElement('div')
    wrap.innerHTML = '<img src="https://invalid.invalid/x" /><img src="https://invalid.invalid/y" />'
    document.body.append(wrap)
    attachMarkdownImageFallbacks(wrap)
    wrap.querySelectorAll('img').forEach((im) => im.dispatchEvent(new Event('error')))
    expect([...wrap.querySelectorAll('img')].every((im) => im.src.includes('data:image/svg+xml'))).toBe(true)
  })

  it('exports stable placeholder prefix', () => {
    expect(BROKEN_IMAGE_PLACEHOLDER_SVG.startsWith('data:image/svg+xml')).toBe(true)
  })

  it('does not overwrite existing alt on error', () => {
    const img = document.createElement('img')
    img.alt = 'Doc'
    img.src = 'https://invalid.invalid/x'
    bindImageErrorToPlaceholder(img)
    img.dispatchEvent(new Event('error'))
    expect(img.alt).toBe('Doc')
  })

  it('ignores non-img and skips double bind', () => {
    expect(() => bindImageErrorToPlaceholder(null)).not.toThrow()
    const img = document.createElement('img')
    img.src = 'https://invalid.invalid/y'
    bindImageErrorToPlaceholder(img)
    bindImageErrorToPlaceholder(img)
    expect(img.dataset.placeholderBound).toBe('1')
  })

  it('attachMarkdownImageFallbacks no-ops on null root', () => {
    expect(() => attachMarkdownImageFallbacks(null)).not.toThrow()
  })

  it('encodes raw spaces in img src so the browser can fetch them', () => {
    const wrap = document.createElement('div')
    const img = document.createElement('img')
    // happy-dom 的 setAttribute('src', ...) 不会自动 encode，模拟 LLM 直接写
    img.setAttribute('src', '/images/IFS 系统培训手册/img_0001.png')
    wrap.append(img)
    document.body.append(wrap)

    attachMarkdownImageFallbacks(wrap)

    const got = img.getAttribute('src')
    expect(got).not.toContain(' ')
    expect(got).toContain('%20')
    expect(got).toMatch(/img_0001\.png$/)
  })

  it('does not double-encode an already-encoded src', () => {
    const wrap = document.createElement('div')
    const img = document.createElement('img')
    const already = '/images/IFS%20%E7%B3%BB%E7%BB%9F/img_0001.png'
    img.setAttribute('src', already)
    wrap.append(img)
    document.body.append(wrap)

    attachMarkdownImageFallbacks(wrap)

    expect(img.getAttribute('src')).toBe(already)
  })

  it('leaves data: URLs untouched', () => {
    const wrap = document.createElement('div')
    const img = document.createElement('img')
    const dataUrl = 'data:image/png;base64,iVBORw0KGgo='
    img.setAttribute('src', dataUrl)
    wrap.append(img)
    document.body.append(wrap)

    attachMarkdownImageFallbacks(wrap)

    expect(img.getAttribute('src')).toBe(dataUrl)
  })

  it('estimateDataUrlDecodedBytes returns 0 for non-data or empty payload', () => {
    expect(estimateDataUrlDecodedBytes('https://x/')).toBe(0)
    expect(estimateDataUrlDecodedBytes('data:,')).toBe(0)
  })

  it('estimateDataUrlDecodedBytes handles non-base64 payload and decode errors', () => {
    const plain = 'data:text/plain;charset=utf-8,' + encodeURIComponent('你好')
    expect(estimateDataUrlDecodedBytes(plain)).toBeGreaterThan(0)
    expect(estimateDataUrlDecodedBytes('data:text/plain,%ZZ%')).toBeGreaterThan(0)
  })

  it('estimateDataUrlDecodedBytes approximates base64 length', () => {
    const b64 = btoa('x'.repeat(100))
    const url = `data:image/png;base64,${b64}`
    expect(estimateDataUrlDecodedBytes(url)).toBe(100)
  })

  it('deferOversizedDataUrlIfNeeded queues loading then applies real src', () => {
    const img = document.createElement('img')
    const b64 = btoa('y'.repeat(200))
    const big = `data:image/png;base64,${b64}`
    img.setAttribute('src', big)
    document.body.append(img)
    const q = []
    deferOversizedDataUrlIfNeeded(img, {
      byteThreshold: 100,
      scheduleApply: (cb) => q.push(cb),
    })
    expect(q).toHaveLength(1)
    expect(img.src).toContain(encodeURIComponent('图片加载中'))
    expect(img.dataset.deferredLargeSrc).toBe(big)
    q[0]()
    expect(img.dataset.deferredLargeSrc).toBeUndefined()
    expect(img.getAttribute('src')).toContain(b64.slice(0, 30))
  })

  it('defer apply skips when img removed before idle', () => {
    const img = document.createElement('img')
    const b64 = btoa('z'.repeat(200))
    const big = `data:image/png;base64,${b64}`
    img.setAttribute('src', big)
    document.body.append(img)
    const q = []
    deferOversizedDataUrlIfNeeded(img, {
      byteThreshold: 100,
      scheduleApply: (cb) => q.push(cb),
    })
    img.remove()
    q[0]()
    expect(img.dataset.deferredLargeSrc).toBe(big)
  })

  it('attachMarkdownImageFallbacks uses requestIdleCallback when present', async () => {
    vi.stubGlobal('requestIdleCallback', (cb) => {
      setTimeout(cb, 0)
    })
    const wrap = document.createElement('div')
    const b64 = btoa('a'.repeat(200))
    wrap.innerHTML = `<img src="data:image/png;base64,${b64}" />`
    document.body.append(wrap)
    attachMarkdownImageFallbacks(wrap, { byteThreshold: 100 })
    await vi.waitFor(() => {
      const im = wrap.querySelector('img')
      expect(im.dataset.deferredLargeSrc).toBeUndefined()
      expect(im.getAttribute('src')).toContain(b64.slice(0, 20))
    })
  })

  it('attachMarkdownImageFallbacks falls back to setTimeout without ric', async () => {
    vi.stubGlobal('requestIdleCallback', undefined)
    const wrap = document.createElement('div')
    const b64 = btoa('b'.repeat(200))
    wrap.innerHTML = `<img src="data:image/png;base64,${b64}" />`
    document.body.append(wrap)
    attachMarkdownImageFallbacks(wrap, { byteThreshold: 100 })
    await vi.waitFor(() => {
      const im = wrap.querySelector('img')
      expect(im.dataset.deferredLargeSrc).toBeUndefined()
      expect(im.getAttribute('src')).toContain(b64.slice(0, 20))
    })
  })

  it('getImageEffectiveSrc prefers deferredLargeSrc', () => {
    const img = document.createElement('img')
    img.src = LOADING_IMAGE_PLACEHOLDER_SVG
    img.dataset.deferredLargeSrc = 'data:image/gif;base64,QQ'
    expect(getImageEffectiveSrc(img)).toBe('data:image/gif;base64,QQ')
  })

  it('getImageEffectiveSrc returns empty for non-img', () => {
    expect(getImageEffectiveSrc(/** @type {any} */ (null))).toBe('')
  })

  it('applyImageSrcMaybeDeferred no-ops on invalid img or url', () => {
    expect(() =>
      applyImageSrcMaybeDeferred(/** @type {any} */ (null), 'data:image/gif;base64,QQ', {
        byteThreshold: 1,
      }),
    ).not.toThrow()
    const img = document.createElement('img')
    document.body.append(img)
    applyImageSrcMaybeDeferred(img, '', { byteThreshold: 1 })
    expect(img.getAttribute('src')).toBeNull()
  })

  it('applyImageSrcMaybeDeferred drops stale apply when src changes quickly', () => {
    const img = document.createElement('img')
    document.body.append(img)
    const queue = []
    const bigA = `data:image/png;base64,${'A'.repeat(300)}`
    const bigB = `data:image/png;base64,${'C'.repeat(300)}`
    applyImageSrcMaybeDeferred(img, bigA, {
      byteThreshold: 100,
      scheduleApply: (cb) => queue.push(cb),
    })
    applyImageSrcMaybeDeferred(img, bigB, {
      byteThreshold: 100,
      scheduleApply: (cb) => queue.push(cb),
    })
    expect(queue).toHaveLength(2)
    queue[0]()
    expect(img.src).toContain(encodeURIComponent('图片加载中'))
    queue[1]()
    expect(img.src).toContain('CCC')
  })
})
