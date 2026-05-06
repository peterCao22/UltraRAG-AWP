import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { Toast, showToast } from '../components/toast.js'

describe('toast', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    document.getElementById('ultrarag-toast-root')?.remove()
  })

  it('showToast appends a toast and removes after duration', () => {
    showToast('hello', 'success', 100)
    const root = document.getElementById('ultrarag-toast-root')
    expect(root).toBeTruthy()
    expect(root.querySelector('.toast--success')?.textContent).toBe('hello')
    vi.advanceTimersByTime(100)
    vi.advanceTimersByTime(250)
    expect(root.querySelector('.toast')).toBeNull()
  })

  it('Toast.show is an alias', () => {
    Toast.show('x', 'error', 50)
    expect(document.querySelector('.toast--error')?.textContent).toBe('x')
  })

  it('stacks multiple toasts in one container', () => {
    showToast('a', 'success', 200)
    showToast('b', 'info', 200)
    const root = document.getElementById('ultrarag-toast-root')
    expect(root.children.length).toBe(2)
  })

  it('maps unknown kind to success', () => {
    showToast('m', 'weird', 20)
    expect(document.querySelector('.toast--success')?.textContent).toBe('m')
  })

  it('uses default duration when durationMs is zero', () => {
    showToast('d', 'error', 0)
    vi.advanceTimersByTime(4999)
    expect(document.querySelector('.toast')).toBeTruthy()
    vi.advanceTimersByTime(2)
    vi.advanceTimersByTime(250)
    expect(document.querySelector('.toast')).toBeNull()
  })
})
