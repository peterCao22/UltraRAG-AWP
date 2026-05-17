/**
 * 对话页智能体下拉：
 *   - Phase 1：option.value = 'quick' / 'agent'（agent_mode 字符串）
 *   - Phase 7.2.A：option.value = agent_id（如 'builtin-quick' / 'agent_xxx'），
 *     dataset.agentMode = 'quick' / 'agent'，便于发消息时同时拿到两者。
 *
 * AGENT_STORAGE_KEY 沿用旧名，存的值升级为 agent_id；老用户首次进入时
 * 旧值 'quick' / 'agent' 会被映射到 builtin-quick / builtin-agent。
 */

export const AGENT_STORAGE_KEY = 'ultrarag_agent_mode'

const ANALYST_VALUE = 'analyst'

/**
 * 若尚未存在，则追加禁用的「数据分析师」占位项（对齐 WeKnora 第三项占位）。
 *
 * @param {HTMLSelectElement} selectEl
 */
export function mountAgentSelect(selectEl) {
  const exists = [...selectEl.options].some((o) => o.value === ANALYST_VALUE)
  if (exists) return
  const opt = document.createElement('option')
  opt.value = ANALYST_VALUE
  opt.textContent = '智能体：数据分析师（即将推出）'
  opt.disabled = true
  opt.title = '即将推出'
  selectEl.append(opt)
}

/**
 * 用后端 /api/chat/agents 列表替换下拉选项（保留禁用的「数据分析师」占位项）。
 *
 * @param {HTMLSelectElement} selectEl
 * @param {Array<{agent_id: string, name: string, agent_mode: string}>} agents
 */
export function populateAgentSelect(selectEl, agents) {
  // 清掉非占位项；占位项 disabled 由其它逻辑保证
  const placeholders = [...selectEl.options].filter((o) => o.disabled)
  selectEl.innerHTML = ''
  for (const a of agents || []) {
    const opt = document.createElement('option')
    opt.value = a.agent_id
    opt.textContent = `智能体：${a.name}`
    opt.dataset.agentMode = a.agent_mode || 'quick'
    selectEl.append(opt)
  }
  for (const ph of placeholders) {
    selectEl.append(ph)
  }
}

/**
 * 取当前选中 option 的 (agent_id, agent_mode)。
 *
 * 兼容旧 option（value='quick' / 'agent'，无 dataset.agentMode）。
 *
 * @param {HTMLSelectElement} selectEl
 * @returns {{ agentId: string, agentMode: string }}
 */
export function getSelectedAgent(selectEl) {
  const opt = selectEl.selectedOptions?.[0]
  if (!opt) return { agentId: '', agentMode: 'quick' }
  const value = opt.value || ''
  const mode = (opt.dataset?.agentMode || '').trim()
  if (mode) {
    return { agentId: value, agentMode: mode }
  }
  // 老 option（'quick' / 'agent' 字符串）回退
  if (value === 'agent' || value === 'quick') {
    return { agentId: '', agentMode: value }
  }
  return { agentId: value, agentMode: 'quick' }
}

/**
 * 从 localStorage 恢复上次选择。值升级路径：
 *   - 老值 'quick' / 'agent' → 对应 builtin agent_id（若 enabled 列表里有）
 *   - 新值 = 任意 agent_id；若已被 admin 禁用 / 删除则忽略
 *
 * @param {HTMLSelectElement} selectEl
 * @param {Storage} storage
 */
export function applyStoredAgentMode(selectEl, storage) {
  let v = ''
  try {
    v = storage?.getItem(AGENT_STORAGE_KEY) || ''
  } catch {
    v = ''
  }
  if (!v) return
  if (v === 'quick' || v === 'agent') {
    const target = v === 'agent' ? 'builtin-agent' : 'builtin-quick'
    if ([...selectEl.options].some((o) => o.value === target)) {
      selectEl.value = target
      return
    }
    // 没有 builtin option（如 vitest 里只有 quick/agent 兜底选项）→ 直接用原值
    if ([...selectEl.options].some((o) => o.value === v)) {
      selectEl.value = v
    }
    return
  }
  if ([...selectEl.options].some((o) => o.value === v)) {
    selectEl.value = v
  }
}
