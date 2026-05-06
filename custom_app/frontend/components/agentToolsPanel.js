/**
 * 管理后台·智能推理工具配置面板。
 *
 * 职责：渲染勾选框列表，管理脏状态，调用回调保存。Required 工具显示但禁用勾选。
 */

/**
 * 构建工具配置面板。
 *
 * @param {{
 *   allTools: Array<{ name: string, label: string, required: boolean }>,
 *   enabledTools: string[],
 *   onSave: (enabledTools: string[]) => Promise<void>,
 * }} options
 * @returns {{ root: HTMLElement, refresh: (data: { allTools, enabledTools }) => void }}
 */
export function buildAgentToolsPanel({ allTools, enabledTools, onSave }) {
  const root = document.createElement('section')
  root.className = 'admin-agent-tools'
  root.dataset.role = 'agent-tools-panel'

  const title = document.createElement('h2')
  title.textContent = '智能推理工具配置'

  const desc = document.createElement('p')
  desc.className = 'muted'
  desc.textContent =
    '控制本知识库的智能推理使用哪些工具。必填项始终启用、不可关闭。修改后点「保存」生效。'

  const list = document.createElement('div')
  list.className = 'admin-agent-tools__list'
  list.dataset.role = 'agent-tools-list'

  const actions = document.createElement('div')
  actions.className = 'admin-agent-tools__actions'

  const saveBtn = document.createElement('button')
  saveBtn.type = 'button'
  saveBtn.className = 'button-primary'
  saveBtn.textContent = '保存'
  saveBtn.dataset.role = 'agent-tools-save'

  const status = document.createElement('span')
  status.className = 'admin-agent-tools__status muted'
  status.dataset.role = 'agent-tools-status'

  actions.append(saveBtn, status)
  root.append(title, desc, list, actions)

  let state = {
    allTools: allTools || [],
    enabledTools: new Set(enabledTools || []),
    dirty: false,
    saving: false,
  }

  function paintItems() {
    list.replaceChildren()
    for (const tool of state.allTools) {
      const row = document.createElement('label')
      row.className = 'admin-agent-tools__item'
      const cb = document.createElement('input')
      cb.type = 'checkbox'
      cb.dataset.toolName = tool.name
      cb.checked = state.enabledTools.has(tool.name) || tool.required
      cb.disabled = !!tool.required || state.saving
      cb.addEventListener('change', () => {
        if (cb.checked) state.enabledTools.add(tool.name)
        else state.enabledTools.delete(tool.name)
        state.dirty = true
        paintStatus()
      })
      const span = document.createElement('span')
      span.textContent = tool.label || tool.name
      if (tool.required) {
        const badge = document.createElement('em')
        badge.className = 'admin-agent-tools__required'
        badge.textContent = '必填'
        span.append(' ', badge)
      }
      row.append(cb, span)
      list.append(row)
    }
  }

  function paintStatus() {
    if (state.saving) {
      status.textContent = '保存中…'
      saveBtn.disabled = true
      return
    }
    if (state.dirty) {
      status.textContent = '有未保存的更改'
      saveBtn.disabled = false
    } else {
      status.textContent = '已保存'
      saveBtn.disabled = true
    }
  }

  saveBtn.addEventListener('click', async () => {
    if (!state.dirty || state.saving || typeof onSave !== 'function') return
    state.saving = true
    paintItems()
    paintStatus()
    try {
      const tools = Array.from(state.enabledTools)
      await onSave(tools)
      state.dirty = false
    } finally {
      state.saving = false
      paintItems()
      paintStatus()
    }
  })

  paintItems()
  paintStatus()

  function refresh(data) {
    state.allTools = data.allTools || state.allTools
    state.enabledTools = new Set(data.enabledTools || [])
    state.dirty = false
    state.saving = false
    paintItems()
    paintStatus()
  }

  return { root, refresh }
}
