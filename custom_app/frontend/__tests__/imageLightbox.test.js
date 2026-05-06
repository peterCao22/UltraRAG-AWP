import { afterEach, describe, expect, it } from 'vitest'

import { bindChatImageLightbox, closeLightbox, openLightbox } from '../components/imageLightbox.js'

const PIXEL =
  'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'

describe('openLightbox / closeLightbox', () => {
  afterEach(() => {
    closeLightbox()
    document.body.innerHTML = ''
  })

  it('mounts overlay and closes on Escape', () => {
    openLightbox([PIXEL, PIXEL], 0)
    const overlay = document.querySelector('[data-role="image-lightbox"]')
    expect(overlay).toBeTruthy()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(document.querySelector('[data-role="image-lightbox"]')).toBeNull()
  })

  it('closes when clicking backdrop', () => {
    openLightbox([PIXEL], 0)
    const backdrop = document.querySelector('.image-lightbox-backdrop')
    expect(backdrop).toBeTruthy()
    backdrop.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(document.querySelector('[data-role="image-lightbox"]')).toBeNull()
  })

  it('cycles images with Arrow keys when multiple', () => {
    const a = 'data:image/gif;base64,AAAA'
    const b = 'data:image/gif;base64,AAAB'
    openLightbox([a, b], 0)
    const img = document.querySelector('.image-lightbox-img')
    expect(img.src).toContain('AAAA')

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    expect(img.src).toContain('AAAB')

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft' }))
    expect(img.src).toContain('AAAA')
  })

  it('does not mount when urls are empty or all falsy', () => {
    openLightbox([], 0)
    openLightbox(['', null], 0)
    expect(document.querySelector('[data-role="image-lightbox"]')).toBeNull()
  })

  it('closes when clicking the × close button', () => {
    openLightbox([PIXEL], 0)
    document.querySelector('.image-lightbox-close').click()
    expect(document.querySelector('[data-role="image-lightbox"]')).toBeNull()
  })

  it('cycles with prev/next nav buttons', () => {
    const a = 'data:image/gif;base64,AAAA'
    const b = 'data:image/gif;base64,AAAB'
    openLightbox([a, b], 0)
    const img = document.querySelector('.image-lightbox-img')
    const [prev, next] = document.querySelectorAll('.image-lightbox-navbtn')
    next.click()
    expect(img.src).toContain('AAAB')
    prev.click()
    expect(img.src).toContain('AAAA')
  })
})

describe('bindChatImageLightbox', () => {
  afterEach(() => {
    closeLightbox()
    document.body.innerHTML = ''
  })

  it('no-ops when root is null', () => {
    expect(() => bindChatImageLightbox(null)).not.toThrow()
  })

  it('ignores thumb click when there is no source-panel ancestor', () => {
    const root = document.createElement('div')
    root.innerHTML = `<img class="source-card__thumb" src="${PIXEL}" alt="" />`
    document.body.append(root)
    bindChatImageLightbox(root)
    root.querySelector('.source-card__thumb').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(document.querySelector('[data-role="image-lightbox"]')).toBeNull()
  })

  it('ignores message images outside the bound root', () => {
    const root = document.createElement('div')
    const outer = document.createElement('div')
    outer.innerHTML = `<div data-role="message-content"><img src="${PIXEL}" alt="" /></div>`
    document.body.append(root, outer)
    bindChatImageLightbox(root)
    outer.querySelector('img').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(document.querySelector('[data-role="image-lightbox"]')).toBeNull()
  })

  it('thumb click prefers data-deferred-large-src over displayed src', () => {
    const root = document.createElement('div')
    root.innerHTML = `
      <section data-role="source-panel">
        <div data-role="source-card-thumbs">
          <img class="source-card__thumb" alt="" />
        </div>
      </section>
    `
    const thumb = root.querySelector('.source-card__thumb')
    thumb.removeAttribute('src')
    thumb.dataset.deferredLargeSrc = PIXEL
    document.body.append(root)
    bindChatImageLightbox(root)
    thumb.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(document.querySelector('.image-lightbox-img').src).toContain('R0lGOD')
  })

  it('opens lightbox when a thumbnail inside a source panel is clicked', () => {
    const root = document.createElement('div')
    root.innerHTML = `
      <section data-role="source-panel">
        <div data-role="source-card-thumbs">
          <img class="source-card__thumb" src="${PIXEL}" alt="" />
          <img class="source-card__thumb" src="data:image/gif;base64,AAAC" alt="" />
        </div>
      </section>
    `
    document.body.append(root)
    bindChatImageLightbox(root)

    root.querySelectorAll('.source-card__thumb')[1].dispatchEvent(new MouseEvent('click', { bubbles: true }))

    const overlay = document.querySelector('[data-role="image-lightbox"]')
    expect(overlay).toBeTruthy()
    const big = overlay.querySelector('.image-lightbox-img')
    expect(big.src).toContain('AAAC')
  })

  it('opens lightbox for inline images in message content', () => {
    const root = document.createElement('div')
    root.innerHTML = `
      <article class="message ai">
        <div data-role="message-content">
          <img src="${PIXEL}" alt="" />
          <img src="data:image/gif;base64,AAAD" alt="" />
        </div>
      </article>
    `
    document.body.append(root)
    bindChatImageLightbox(root)

    root.querySelector('img').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const overlay = document.querySelector('[data-role="image-lightbox"]')
    expect(overlay).toBeTruthy()
    expect(overlay.querySelector('.image-lightbox-img').src).toContain('R0lGOD')
  })
})
