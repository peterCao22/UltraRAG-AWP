import { beforeEach, describe, expect, it, vi } from 'vitest'

import { buildAgentToolsPanel } from '../components/agentToolsPanel.js'

const SAMPLE_ALL = [
  { name: 'knowledge_search', label: '搜索知识库', required: false },
  { name: 'keyword_search', label: '文本关键词搜索', required: false },
  { name: 'list_knowledge_chunks', label: 'Deep Read', required: true },
  { name: 'final_answer', label: '提交最终答案', required: true },
]

describe('buildAgentToolsPanel', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('renders one checkbox per tool with labels', () => {
    const { root } = buildAgentToolsPanel({
      allTools: SAMPLE_ALL,
      enabledTools: ['knowledge_search', 'list_knowledge_chunks', 'final_answer'],
      onSave: vi.fn(),
    })
    document.body.append(root)
    const cbs = root.querySelectorAll('input[type="checkbox"]')
    expect(cbs.length).toBe(4)
    expect(root.textContent).toContain('搜索知识库')
    expect(root.textContent).toContain('Deep Read')
  })

  it('disables and forces-checked required tools', () => {
    const { root } = buildAgentToolsPanel({
      allTools: SAMPLE_ALL,
      enabledTools: [], // 故意空，required 应仍被勾选
      onSave: vi.fn(),
    })
    document.body.append(root)
    const final = root.querySelector('input[data-tool-name="final_answer"]')
    const lc = root.querySelector('input[data-tool-name="list_knowledge_chunks"]')
    expect(final.disabled).toBe(true)
    expect(final.checked).toBe(true)
    expect(lc.disabled).toBe(true)
    expect(lc.checked).toBe(true)
  })

  it('marks panel dirty after toggling and enables save', () => {
    const { root } = buildAgentToolsPanel({
      allTools: SAMPLE_ALL,
      enabledTools: ['knowledge_search', 'list_knowledge_chunks', 'final_answer'],
      onSave: vi.fn(),
    })
    document.body.append(root)
    const saveBtn = root.querySelector('[data-role="agent-tools-save"]')
    expect(saveBtn.disabled).toBe(true) // pristine

    const ks = root.querySelector('input[data-tool-name="knowledge_search"]')
    ks.checked = false
    ks.dispatchEvent(new Event('change'))

    expect(saveBtn.disabled).toBe(false)
    expect(root.querySelector('[data-role="agent-tools-status"]').textContent).toContain('未保存')
  })

  it('calls onSave with current enabled set when save clicked', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    const { root } = buildAgentToolsPanel({
      allTools: SAMPLE_ALL,
      enabledTools: ['knowledge_search', 'keyword_search', 'list_knowledge_chunks', 'final_answer'],
      onSave,
    })
    document.body.append(root)

    const kw = root.querySelector('input[data-tool-name="keyword_search"]')
    kw.checked = false
    kw.dispatchEvent(new Event('change'))

    const saveBtn = root.querySelector('[data-role="agent-tools-save"]')
    saveBtn.click()
    await Promise.resolve()
    await Promise.resolve()

    expect(onSave).toHaveBeenCalledTimes(1)
    const arg = onSave.mock.calls[0][0]
    expect(arg).toContain('knowledge_search')
    expect(arg).not.toContain('keyword_search')
  })

  it('refresh resets state and re-paints', () => {
    const { root, refresh } = buildAgentToolsPanel({
      allTools: SAMPLE_ALL,
      enabledTools: ['knowledge_search', 'list_knowledge_chunks', 'final_answer'],
      onSave: vi.fn(),
    })
    document.body.append(root)

    // toggle to dirty
    const ks = root.querySelector('input[data-tool-name="knowledge_search"]')
    ks.checked = false
    ks.dispatchEvent(new Event('change'))
    expect(root.querySelector('[data-role="agent-tools-save"]').disabled).toBe(false)

    refresh({
      allTools: SAMPLE_ALL,
      enabledTools: ['keyword_search', 'list_knowledge_chunks', 'final_answer'],
    })

    const ks2 = root.querySelector('input[data-tool-name="knowledge_search"]')
    const kw2 = root.querySelector('input[data-tool-name="keyword_search"]')
    expect(ks2.checked).toBe(false)
    expect(kw2.checked).toBe(true)
    expect(root.querySelector('[data-role="agent-tools-save"]').disabled).toBe(true) // 干净
  })
})
