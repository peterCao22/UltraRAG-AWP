import { describe, expect, it } from 'vitest'

import { openConfirmModal } from '../components/confirmModal.js'

describe('openConfirmModal', () => {
  it('resolves false on cancel', async () => {
    const p = openConfirmModal({ message: 'Sure?' })
    document.querySelector('.modal-overlay .button-secondary').click()
    await expect(p).resolves.toBe(false)
    expect(document.querySelector('.modal-overlay')).toBeNull()
  })

  it('resolves true on confirm', async () => {
    const p = openConfirmModal({ message: 'Go?' })
    document.querySelector('.modal-overlay .button-primary').click()
    await expect(p).resolves.toBe(true)
  })

  it('resolves false on Escape', async () => {
    const p = openConfirmModal({ message: 'Esc?' })
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await expect(p).resolves.toBe(false)
  })

  it('resolves false when clicking the dimmed backdrop', async () => {
    const p = openConfirmModal({ message: 'backdrop' })
    document.querySelector('.modal-overlay').click()
    await expect(p).resolves.toBe(false)
  })
})
