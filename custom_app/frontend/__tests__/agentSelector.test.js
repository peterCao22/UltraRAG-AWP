import { beforeEach, describe, expect, it, vi } from 'vitest'

import { applyStoredAgentMode, mountAgentSelect } from '../components/agentSelector.js'

describe('agentSelector', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('mountAgentSelect appends disabled analyst option once', () => {
    const sel = document.createElement('select')
    sel.innerHTML = '<option value="quick">Q</option><option value="agent">A</option>'
    mountAgentSelect(sel)
    mountAgentSelect(sel)
    expect(sel.options.length).toBe(3)
    expect(sel.options[2].value).toBe('analyst')
    expect(sel.options[2].disabled).toBe(true)
    expect(sel.options[2].title).toBe('即将推出')
  })

  it('applyStoredAgentMode sets quick or agent from storage', () => {
    const sel = document.createElement('select')
    sel.innerHTML = '<option value="quick">Q</option><option value="agent">A</option>'
    applyStoredAgentMode(sel, { getItem: () => 'agent' })
    expect(sel.value).toBe('agent')
    applyStoredAgentMode(sel, { getItem: () => 'bogus' })
    expect(sel.value).toBe('agent')
    applyStoredAgentMode(sel, { getItem: () => 'quick' })
    expect(sel.value).toBe('quick')
  })

  it('applyStoredAgentMode ignores invalid storage values', () => {
    const sel = document.createElement('select')
    sel.innerHTML = '<option value="quick">Q</option><option value="agent">A</option>'
    sel.value = 'agent'
    applyStoredAgentMode(sel, { getItem: vi.fn(() => null) })
    expect(sel.value).toBe('agent')
    applyStoredAgentMode(sel, { getItem: () => 'analyst' })
    expect(sel.value).toBe('agent')
  })
})
