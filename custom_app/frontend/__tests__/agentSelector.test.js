import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  applyStoredAgentMode,
  getSelectedAgent,
  mountAgentSelect,
  populateAgentSelect,
} from '../components/agentSelector.js'

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

  // ── Phase 7.2.A ────────────────────────────────────────────────────────

  it('populateAgentSelect replaces options and preserves disabled placeholders', () => {
    const sel = document.createElement('select')
    sel.innerHTML = '<option value="quick">Q</option><option value="agent">A</option>'
    mountAgentSelect(sel) // adds disabled "analyst"
    populateAgentSelect(sel, [
      { agent_id: 'builtin-quick', name: '快速问答', agent_mode: 'quick' },
      { agent_id: 'agent_x', name: '商业资料助手', agent_mode: 'quick' },
    ])
    // 占位项保留
    const analyst = [...sel.options].find((o) => o.value === 'analyst')
    expect(analyst).toBeDefined()
    expect(analyst.disabled).toBe(true)

    const real = [...sel.options].filter((o) => !o.disabled)
    expect(real.map((o) => o.value)).toEqual(['builtin-quick', 'agent_x'])
    expect(real[0].textContent).toBe('智能体：快速问答')
  })

  it('getSelectedAgent returns agent_id and mode from dataset', () => {
    const sel = document.createElement('select')
    populateAgentSelect(sel, [
      { agent_id: 'builtin-agent', name: '智能推理', agent_mode: 'agent' },
    ])
    sel.value = 'builtin-agent'
    expect(getSelectedAgent(sel)).toEqual({
      agentId: 'builtin-agent',
      agentMode: 'agent',
    })
  })

  it('getSelectedAgent supports legacy quick/agent options', () => {
    const sel = document.createElement('select')
    sel.innerHTML = '<option value="quick">Q</option><option value="agent">A</option>'
    sel.value = 'agent'
    expect(getSelectedAgent(sel)).toEqual({ agentId: '', agentMode: 'agent' })
  })

  it('applyStoredAgentMode upgrades legacy quick/agent string to builtin id', () => {
    const sel = document.createElement('select')
    populateAgentSelect(sel, [
      { agent_id: 'builtin-quick', name: '快速问答', agent_mode: 'quick' },
      { agent_id: 'builtin-agent', name: '智能推理', agent_mode: 'agent' },
    ])
    applyStoredAgentMode(sel, { getItem: () => 'agent' })
    expect(sel.value).toBe('builtin-agent')
    applyStoredAgentMode(sel, { getItem: () => 'quick' })
    expect(sel.value).toBe('builtin-quick')
  })

  it('applyStoredAgentMode picks stored agent_id verbatim', () => {
    const sel = document.createElement('select')
    populateAgentSelect(sel, [
      { agent_id: 'builtin-quick', name: '快速问答', agent_mode: 'quick' },
      { agent_id: 'agent_x', name: '自定义', agent_mode: 'quick' },
    ])
    applyStoredAgentMode(sel, { getItem: () => 'agent_x' })
    expect(sel.value).toBe('agent_x')
  })
})
