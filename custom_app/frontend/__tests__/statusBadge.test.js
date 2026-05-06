import { describe, expect, it } from 'vitest'

import { createStatusBadge, formatStatusLabel, getStatusBadgeModifier } from '../components/statusBadge.js'

describe('statusBadge', () => {
  it('formats known statuses', () => {
    expect(formatStatusLabel('active')).toBe('可用')
    expect(formatStatusLabel('indexed')).toBe('已索引')
  })

  it('getStatusBadgeModifier returns a stable class token', () => {
    expect(getStatusBadgeModifier('failed')).toBe('status-badge--err')
    expect(getStatusBadgeModifier('unknown_xyz')).toBe('status-badge--neutral')
  })

  it('createStatusBadge mounts label and class', () => {
    const el = createStatusBadge('ready')
    expect(el.className).toContain('status-badge')
    expect(el.textContent).toBe('就绪')
  })

  it('handles empty status with neutral modifier', () => {
    expect(getStatusBadgeModifier('')).toBe('status-badge--neutral')
    expect(formatStatusLabel('')).toBe('未知')
  })
})
