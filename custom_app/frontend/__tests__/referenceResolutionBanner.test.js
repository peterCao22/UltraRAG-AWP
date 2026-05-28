import { beforeEach, describe, expect, it } from 'vitest'

import { buildReferenceResolutionBanner } from '../components/referenceResolutionBanner.js'

describe('buildReferenceResolutionBanner', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('returns null when event is null or missing', () => {
    expect(buildReferenceResolutionBanner(null)).toBeNull()
    expect(buildReferenceResolutionBanner(undefined)).toBeNull()
    expect(buildReferenceResolutionBanner({})).toBeNull()
  })

  it('returns null when applied is false', () => {
    expect(
      buildReferenceResolutionBanner({
        applied: false,
        rewritten_query: 'x',
      }),
    ).toBeNull()
  })

  it('returns null when rewritten_query is empty', () => {
    expect(
      buildReferenceResolutionBanner({
        applied: true,
        rewritten_query: '',
      }),
    ).toBeNull()
  })

  it('renders rewritten query and label', () => {
    const el = buildReferenceResolutionBanner({
      applied: true,
      original_query: '它怎么操作？',
      rewritten_query: '急停按钮如何检查',
      confidence: 0.92,
      resolved: [],
    })
    document.body.append(el)
    expect(el.dataset.role).toBe('reference-resolution')
    expect(el.querySelector('.reference-resolution__label').textContent).toContain('系统理解为')
    expect(el.querySelector('.reference-resolution__rewritten').textContent).toBe('急停按钮如何检查')
  })

  it('renders confidence chip with tooltip', () => {
    const el = buildReferenceResolutionBanner({
      applied: true,
      original_query: '它呢',
      rewritten_query: '急停按钮检查',
      confidence: 0.9234,
      ms: 612,
      model: 'claude-haiku-4-5-20251001',
      resolved: [],
    })
    const chip = el.querySelector('.reference-resolution__confidence')
    expect(chip).toBeTruthy()
    expect(chip.textContent).toBe('0.92')
    expect(chip.title).toContain('claude-haiku-4-5-20251001')
    expect(chip.title).toContain('612 ms')
  })

  it('renders resolved list when items present', () => {
    const el = buildReferenceResolutionBanner({
      applied: true,
      original_query: '第 2 个怎么操作',
      rewritten_query: '急停按钮如何检查',
      confidence: 0.95,
      resolved: [
        { reference: '第 2 个', meaning: '急停按钮' },
        { reference: '操作', meaning: '检查' },
      ],
    })
    const list = el.querySelector('.reference-resolution__resolved-list')
    expect(list).toBeTruthy()
    expect(list.querySelectorAll('li')).toHaveLength(2)
  })

  it('skips resolved items with missing fields', () => {
    const el = buildReferenceResolutionBanner({
      applied: true,
      original_query: 'x',
      rewritten_query: 'y',
      confidence: 0.9,
      resolved: [
        { reference: 'a', meaning: 'A' },
        { reference: '', meaning: 'B' },   // 缺 reference
        { reference: 'c', meaning: '' },    // 缺 meaning
        null,                                // 非 dict
      ],
    })
    const items = el.querySelectorAll('.reference-resolution__resolved-list li')
    expect(items).toHaveLength(1)
  })

  it('fires custom event on correct button click with original/rewritten payload', () => {
    const el = buildReferenceResolutionBanner({
      applied: true,
      original_query: '第 2 个怎么操作？',
      rewritten_query: '急停按钮如何检查',
      confidence: 0.92,
      resolved: [],
    })
    document.body.append(el)
    const events = []
    el.addEventListener('reference-resolution-correct-request', (ev) => events.push(ev.detail))
    const btn = el.querySelector('.reference-resolution__correct-btn')
    expect(btn).toBeTruthy()
    btn.click()
    expect(events).toHaveLength(1)
    expect(events[0]).toEqual({
      original: '第 2 个怎么操作？',
      rewritten: '急停按钮如何检查',
    })
  })

  it('treats invalid confidence numbers as missing chip', () => {
    const el = buildReferenceResolutionBanner({
      applied: true,
      original_query: 'x',
      rewritten_query: 'y',
      confidence: NaN,
      resolved: [],
    })
    expect(el.querySelector('.reference-resolution__confidence')).toBeNull()
  })

  it('uses textContent to avoid XSS in rewritten content', () => {
    const el = buildReferenceResolutionBanner({
      applied: true,
      original_query: 'safe',
      rewritten_query: '<img src=x onerror=alert(1)>',
      confidence: 0.9,
      resolved: [],
    })
    const rewritten = el.querySelector('.reference-resolution__rewritten')
    expect(rewritten.textContent).toBe('<img src=x onerror=alert(1)>')
    expect(rewritten.querySelector('img')).toBeNull()
  })
})
