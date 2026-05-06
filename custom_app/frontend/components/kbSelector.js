/**
 * 对话页知识库下拉：填充选项、恢复 localStorage 选中项、空列表占位。
 */

export const KB_STORAGE_KEY = 'ultrarag_kb_id'

/**
 * 将知识库列表写入 `<select>`，并返回当前选中的 `kb_id`（同时写回 storage）。
 *
 * @param {HTMLSelectElement} selectEl
 * @param {Array<{ kb_id: string, name?: string }>} knowledgeBases
 * @param {Storage} storage
 * @returns {string} 选中的知识库 id；无可用项时返回空字符串
 */
export function populateKbSelect(selectEl, knowledgeBases, storage) {
  selectEl.innerHTML = ''

  if (!knowledgeBases.length) {
    const option = document.createElement('option')
    option.value = ''
    option.textContent = '暂无可用知识库'
    option.disabled = true
    option.selected = true
    selectEl.append(option)
    return ''
  }

  for (const kb of knowledgeBases) {
    const option = document.createElement('option')
    option.value = kb.kb_id
    option.textContent = kb.name || kb.kb_id
    selectEl.append(option)
  }

  const savedKbId = storage.getItem(KB_STORAGE_KEY)
  const savedExists = knowledgeBases.some((kb) => kb.kb_id === savedKbId)
  const selectedId = savedExists ? savedKbId : knowledgeBases[0].kb_id
  selectEl.value = selectedId
  storage.setItem(KB_STORAGE_KEY, selectedId)
  return selectedId
}
