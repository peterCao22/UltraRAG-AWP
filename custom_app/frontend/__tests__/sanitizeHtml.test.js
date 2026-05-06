import { afterEach, describe, expect, it, vi } from 'vitest'

import { sanitizeHtml } from '../utils/sanitizeHtml.js'

describe('sanitizeHtml', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('returns raw string when DOMPurify is absent', () => {
    expect(sanitizeHtml('<p>hi</p>')).toBe('<p>hi</p>')
  })

  it('uses DOMPurify when present', () => {
    vi.stubGlobal('DOMPurify', {
      sanitize: vi.fn((s) => `[sanitized]${s}`),
    })
    expect(sanitizeHtml('<p>x</p>')).toBe('[sanitized]<p>x</p>')
  })
})
