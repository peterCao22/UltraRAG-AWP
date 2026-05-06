/**
 * 对话气泡 DOM（Sprint 1：从内联 `main.js` 拆出，便于单测与复用）。
 * Markdown 渲染路径仍依赖 `window.marked` + `window.DOMPurify`（与 `index.html` 脚本顺序一致）。
 */

function setThinkingIndicator(body) {
  body.textContent = ''
  body.classList.add('message-content--thinking')
  const wrap = document.createElement('span')
  wrap.className = 'thinking-indicator'
  wrap.setAttribute('role', 'status')
  wrap.append('思考中')
  const dots = document.createElement('span')
  dots.className = 'typing-dots'
  dots.setAttribute('aria-hidden', 'true')
  for (let i = 0; i < 3; i += 1) {
    const d = document.createElement('span')
    d.className = 'typing-dot'
    dots.append(d)
  }
  wrap.append(dots)
  body.append(wrap)
}

export function clearThinkingIndicator(body) {
  body.classList.remove('message-content--thinking')
}

/** 流式结束或切 Markdown 前移除尾部闪烁光标样式 */
export function clearStreamingCursor(body) {
  body.classList.remove('message-content--streaming')
}

export function renderTextContent(element, content) {
  element.textContent = content || ''
}

export function renderMarkdownContent(element, content) {
  if (window.marked && window.DOMPurify) {
    element.innerHTML = window.DOMPurify.sanitize(window.marked.parse(content || ''))
    return
  }

  renderTextContent(element, content)
}

/**
 * @param {'user'|'ai'|'ai error'} role
 * @param {string} content
 * @param {{ streaming?: boolean, thinking?: boolean }} [options]
 */
export function createMessageElement(role, content, { streaming = false, thinking = false } = {}) {
  const article = document.createElement('article')
  article.className = `message ${role}`

  const label = document.createElement('strong')
  label.textContent = role === 'user' ? '我' : '助手'
  article.append(label)

  const body = document.createElement('div')
  body.dataset.role = 'message-content'
  if (role === 'ai' && thinking) {
    setThinkingIndicator(body)
  } else if (role === 'ai' && !streaming) {
    renderMarkdownContent(body, content)
  } else {
    renderTextContent(body, content)
  }
  if (role === 'ai' && streaming) {
    body.classList.add('message-content--streaming')
  }
  article.append(body)

  return { article, body }
}
