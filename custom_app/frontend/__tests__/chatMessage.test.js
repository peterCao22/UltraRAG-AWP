import { beforeEach, describe, expect, it } from 'vitest'

import {
  clearStreamingCursor,
  clearThinkingIndicator,
  createMessageElement,
  renderTextContent,
} from '../components/chatMessage.js'

describe('chatMessage', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('creates user and ai message structure', () => {
    const { article, body } = createMessageElement('user', '你好')
    expect(article.className).toContain('message')
    expect(article.className).toContain('user')
    expect(body.dataset.role).toBe('message-content')
    expect(body.textContent).toBe('你好')
  })

  it('creates thinking state for streaming ai', () => {
    const { body } = createMessageElement('ai', '', { streaming: true, thinking: true })
    expect(body.querySelector('.thinking-indicator')).toBeTruthy()
    expect(body.classList.contains('message-content--streaming')).toBe(true)
    clearThinkingIndicator(body)
    expect(body.classList.contains('message-content--thinking')).toBe(false)
    expect(body.classList.contains('message-content--streaming')).toBe(true)
    clearStreamingCursor(body)
    expect(body.classList.contains('message-content--streaming')).toBe(false)
  })

  it('renderTextContent clears previous nodes', () => {
    const div = document.createElement('div')
    div.append(document.createTextNode('old'))
    renderTextContent(div, 'new')
    expect(div.textContent).toBe('new')
  })
})
