import { beforeEach, describe, expect, it, vi } from 'vitest'

import { KB_STORAGE_KEY, populateKbSelect } from '../components/kbSelector.js'

describe('kbSelector', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('fills options and returns first kb when storage empty', () => {
    const sel = document.createElement('select')
    const storage = { getItem: vi.fn(() => null), setItem: vi.fn() }
    const id = populateKbSelect(
      sel,
      [
        { kb_id: 'a', name: 'A' },
        { kb_id: 'b', name: 'B' },
      ],
      storage,
    )
    expect(id).toBe('a')
    expect(sel.value).toBe('a')
    expect(storage.setItem).toHaveBeenCalledWith(KB_STORAGE_KEY, 'a')
  })

  it('restores saved kb when present in list', () => {
    const sel = document.createElement('select')
    const storage = { getItem: vi.fn(() => 'b'), setItem: vi.fn() }
    const id = populateKbSelect(
      sel,
      [
        { kb_id: 'a', name: 'A' },
        { kb_id: 'b', name: 'B' },
      ],
      storage,
    )
    expect(id).toBe('b')
    expect(sel.value).toBe('b')
  })

  it('returns empty string and placeholder option when list empty', () => {
    const sel = document.createElement('select')
    const storage = { getItem: vi.fn(), setItem: vi.fn() }
    const id = populateKbSelect(sel, [], storage)
    expect(id).toBe('')
    expect(sel.options[0].disabled).toBe(true)
    expect(sel.options[0].textContent).toContain('暂无')
  })
})
