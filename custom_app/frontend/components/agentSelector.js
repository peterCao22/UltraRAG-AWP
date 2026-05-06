/**
 * 对话页智能体模式下拉：恢复 localStorage、补充「即将推出」占位项。
 */

export const AGENT_STORAGE_KEY = 'ultrarag_agent_mode'

const ANALYST_VALUE = 'analyst'

/**
 * 若尚未存在，则追加禁用的「数据分析师」选项（对齐 WeKnora 第三项占位）。
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
 * 从 localStorage 恢复 `quick` / `agent`（忽略无效值与占位项）。
 *
 * @param {HTMLSelectElement} selectEl
 * @param {Storage} storage
 */
export function applyStoredAgentMode(selectEl, storage) {
  const v = storage.getItem(AGENT_STORAGE_KEY)
  if (v === 'agent' || v === 'quick') {
    selectEl.value = v
  }
}
