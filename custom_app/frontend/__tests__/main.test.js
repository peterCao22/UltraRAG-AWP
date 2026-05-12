import { beforeEach, describe, expect, it, vi } from 'vitest'

import { initChatApp } from '../main.js'

function createStorage(initial = {}) {
  const values = new Map(Object.entries(initial))

  return {
    getItem: vi.fn((key) => values.get(key) ?? null),
    setItem: vi.fn((key, value) => values.set(key, value)),
  }
}

function renderChatShell({ withSessionList = false } = {}) {
  const sessionListHtml = withSessionList
    ? '<aside class="chat-sidebar"><div data-role="session-list"></div></aside>'
    : '<aside class="chat-sidebar"></aside>'
  document.body.innerHTML = `
    <div data-page="chat">
      ${sessionListHtml}
      <header class="mobile-topbar">
        <button type="button" data-role="sidebar-toggle">☰</button>
      </header>
      <button type="button" data-role="new-chat">+ 新建对话</button>
      <select id="kb-select" data-role="kb-select"></select>
      <section data-role="message-list"></section>
      <select data-role="agent-select">
        <option value="quick">快速问答</option>
        <option value="agent">智能推理</option>
      </select>
      <span data-role="char-count">0/500</span>
      <textarea id="composer-input" data-role="composer-input"></textarea>
      <button type="button" data-role="send-button">发送</button>
      <div class="chat-sidebar-backdrop" data-role="sidebar-backdrop" aria-hidden="true"></div>
    </div>
  `
  return document.querySelector('[data-page="chat"]')
}

describe('initChatApp', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
    delete window.marked
    delete window.DOMPurify
  })

  it('returns null when chat shell is absent', () => {
    expect(initChatApp({ root: null })).toBeNull()
  })

  it('throws when required shell elements are missing', () => {
    const root = document.createElement('div')

    expect(() => initChatApp({ root })).toThrow('Missing required chat element')
  })

  it('loads usable knowledge bases and restores previous selection', async () => {
    const root = renderChatShell()
    const storage = createStorage({ ultrarag_kb_id: 'agv_demo' })
    const kbApi = {
      listKnowledgeBases: vi.fn().mockResolvedValue([
        { kb_id: 'agv_demo_3', name: 'AGV Demo 3', status: 'active' },
        { kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' },
      ]),
    }

    const app = initChatApp({ root, kbApi, chatApi: { sendChatMessage: vi.fn() }, storage })
    await app.ready

    const options = [...root.querySelector('[data-role="kb-select"]').options]
    expect(options.map((option) => option.value)).toEqual(['agv_demo_3', 'agv_demo'])
    expect(root.querySelector('[data-role="kb-select"]').value).toBe('agv_demo')
  })

  it('stores changed knowledge base and resets the chat', async () => {
    const root = renderChatShell()
    const storage = createStorage()
    const kbApi = {
      listKnowledgeBases: vi.fn().mockResolvedValue([
        { kb_id: 'agv_demo_3', name: 'AGV Demo 3', status: 'active' },
        { kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' },
      ]),
    }
    const app = initChatApp({ root, kbApi, chatApi: { sendChatMessage: vi.fn() }, storage })
    await app.ready

    app.addMessage('user', '旧消息')
    const select = root.querySelector('[data-role="kb-select"]')
    select.value = 'agv_demo'
    select.dispatchEvent(new Event('change'))

    expect(storage.setItem).toHaveBeenCalledWith('ultrarag_kb_id', 'agv_demo')
    expect(root.querySelectorAll('.message')).toHaveLength(1)
    expect(root.querySelector('[data-role="message-list"]').textContent).toContain('请选择知识库后开始提问')
  })

  it('renders empty and loading-error knowledge base states', async () => {
    const root = renderChatShell()
    const emptyApp = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([]) },
      chatApi: { sendChatMessage: vi.fn() },
      storage: createStorage(),
    })
    await emptyApp.ready

    expect(root.querySelector('[data-role="kb-select"]').textContent).toContain('暂无可用知识库')

    const failingRoot = renderChatShell()
    const failingApp = initChatApp({
      root: failingRoot,
      kbApi: { listKnowledgeBases: vi.fn().mockRejectedValue(new Error('网络错误')) },
      chatApi: { sendChatMessage: vi.fn() },
      storage: createStorage(),
    })
    await failingApp.ready

    expect(failingRoot.querySelector('[data-role="message-list"]').textContent).toContain('知识库加载失败：网络错误')
    const errToasts = [...document.querySelectorAll('.toast--error')].map((el) => el.textContent)
    expect(errToasts.some((t) => t.includes('知识库加载失败'))).toBe(true)
  })

  it('restores agent mode from storage and persists on change', async () => {
    const root = renderChatShell()
    const storage = createStorage({ ultrarag_agent_mode: 'agent' })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage: vi.fn() },
      storage,
    })
    await app.ready

    const agentSel = root.querySelector('[data-role="agent-select"]')
    expect([...agentSel.options].some((o) => o.value === 'analyst' && o.disabled)).toBe(true)
    expect(agentSel.value).toBe('agent')
    root.querySelector('[data-role="agent-select"]').value = 'quick'
    root.querySelector('[data-role="agent-select"]').dispatchEvent(new Event('change'))
    expect(storage.setItem).toHaveBeenCalledWith('ultrarag_agent_mode', 'quick')
  })

  it('shows thinking indicator until first chunk then clears', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onChunk, onDone }) => {
      expect(root.querySelector('.thinking-indicator')).toBeTruthy()
      const bodies = root.querySelectorAll('[data-role="message-content"]')
      const aiBody = bodies[bodies.length - 1]
      expect(aiBody.classList.contains('message-content--streaming')).toBe(true)
      onChunk('', '第一段')
      expect(root.querySelector('.thinking-indicator')).toBeFalsy()
      expect(aiBody.classList.contains('message-content--streaming')).toBe(true)
      onDone('第一段')
      expect(aiBody.classList.contains('message-content--streaming')).toBe(false)
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()
    expect(sendChatMessage).toHaveBeenCalledTimes(1)
  })

  it('shows reasoning steps when onThought fires and keeps panel after done', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onThought, onChunk, onDone }) => {
      onThought({ content: '检索相关段落' })
      onChunk('', '答')
      onDone('答')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const panel = root.querySelector('[data-role="reasoning-steps"]')
    expect(panel).toBeTruthy()
    expect(root.querySelector('[data-role="reasoning-steps-log"]').textContent).toContain('检索相关段落')
  })

  it('removes reasoning panel on done when no thought or tool events', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onChunk, onDone }) => {
      onChunk('', 'x')
      onDone('x')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    expect(root.querySelector('[data-role="reasoning-steps"]')).toBeFalsy()
  })

  it('logs tool start and tool result in reasoning panel', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onToolStart, onToolResult, onChunk, onDone }) => {
      onToolStart({ type: 'tool_call', tool_name: 'knowledge_search', hint: '搜索知识库："换电步骤"' })
      onToolResult({ tool_name: 'knowledge_search', summary: '找到 3 个相关片段', duration_ms: 120 })
      onChunk('', 'r')
      onDone('r')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const log = root.querySelector('[data-role="reasoning-steps-log"]')
    expect(log.textContent).toContain('搜索知识库')
    expect(log.textContent).toContain('找到 3 个相关片段')
  })

  it('removes reasoning panel when stream errors', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onThought, onError }) => {
      onThought({ content: 'thinking' })
      onError('boom')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    expect(root.querySelector('[data-role="reasoning-steps"]')).toBeFalsy()
  })

  it('aborts in-flight stream when stop is clicked', async () => {
    const sendChatMessage = vi.fn().mockImplementation((opts) => {
      opts.onChunk('', 'Part')
      return new Promise((resolve) => {
        const onAbort = () => {
          opts.onAbort?.('Part')
          resolve()
        }
        if (opts.signal.aborted) onAbort()
        else opts.signal.addEventListener('abort', onAbort, { once: true })
      })
    })

    const root = renderChatShell()
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    const pending = app.sendCurrentMessage()
    await vi.waitFor(() => root.querySelector('[data-role="send-button"]').textContent === '停止')
    root.querySelector('[data-role="send-button"]').click()
    await pending

    expect(sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(root.querySelector('[data-role="message-list"]').textContent).toContain('Part')
  })

  it('retry removes error bubble and resends without duplicating user message', async () => {
    const root = renderChatShell()
    let n = 0
    const sendChatMessage = vi.fn().mockImplementation(async ({ onChunk, onDone, onError }) => {
      n += 1
      if (n === 1) {
        onError('网络中断')
      } else {
        onChunk('', '好的')
        onDone('好的')
      }
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问题一'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    expect(root.querySelectorAll('.message.user')).toHaveLength(1)
    const retry = root.querySelector('.message-retry')
    expect(retry).toBeTruthy()
    expect([...document.querySelectorAll('.toast--error')].some((el) => el.textContent.includes('对话失败'))).toBe(true)

    await retry.click()
    await app.waitForIdle()

    expect(sendChatMessage).toHaveBeenCalledTimes(2)
    expect(root.querySelectorAll('.message.user')).toHaveLength(1)
    expect(root.querySelector('[data-role="message-list"]').textContent).toContain('好的')
  })

  it('sends a question with selected kb and agent mode, then renders streamed answer', async () => {
    const root = renderChatShell()
    const kbApi = {
      listKnowledgeBases: vi.fn().mockResolvedValue([
        { kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' },
      ]),
    }
    const sendChatMessage = vi.fn().mockImplementation(async ({ onChunk, onSources, onDone }) => {
      onChunk('换电', '换电')
      onChunk('步骤', '换电步骤')
      onSources([{ title: '电池 SOP', excerpt: '摘要一行' }])
      onDone('换电步骤')
    })
    const app = initChatApp({ root, kbApi, chatApi: { sendChatMessage }, storage: createStorage() })
    await app.ready

    root.querySelector('[data-role="agent-select"]').value = 'agent'
    root.querySelector('[data-role="composer-input"]').value = 'AGV 如何换电？'
    await app.sendCurrentMessage()

    expect(sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      kbId: 'agv_demo',
      question: 'AGV 如何换电？',
      agentMode: 'agent',
    }))
    expect(root.querySelector('[data-role="message-list"]').textContent).toContain('AGV 如何换电？')
    expect(root.querySelector('[data-role="message-list"]').textContent).toContain('换电步骤')
    expect(root.querySelector('[data-role="composer-input"]').value).toBe('')
    const panel = root.querySelector('[data-role="source-panel"]')
    expect(panel).toBeTruthy()
    expect(panel.querySelector('[data-role="source-panel-toggle"]').textContent).toContain('引用来源（1）')
    expect(panel.querySelector('.source-card__title').textContent).toBe('电池 SOP')
  })

  it('replaces source panel when onSources fires twice in one stream', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onSources, onDone }) => {
      onSources([{ title: 'First', excerpt: '1' }])
      onSources([{ title: 'Second', excerpt: '2' }])
      onDone('完成')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = 'q'
    await app.sendCurrentMessage()

    expect(root.querySelectorAll('[data-role="source-panel"]')).toHaveLength(1)
    expect(root.querySelector('.source-card__title').textContent).toBe('Second')
  })

  it('disables kb, new-chat, and agent while streaming; send shows stop and stays clickable', async () => {
    const root = renderChatShell()
    let resolveStream
    const streamDone = new Promise((resolve) => {
      resolveStream = resolve
    })
    const sendChatMessage = vi.fn().mockImplementation(async ({ onChunk }) => {
      onChunk('处理中', '处理中')
      await streamDone
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '长问题'
    const pending = app.sendCurrentMessage()

    expect(root.querySelector('[data-role="kb-select"]').disabled).toBe(true)
    expect(root.querySelector('[data-role="new-chat"]').disabled).toBe(true)
    expect(root.querySelector('[data-role="agent-select"]').disabled).toBe(true)
    const sendBtn = root.querySelector('[data-role="send-button"]')
    expect(sendBtn.disabled).toBe(false)
    expect(sendBtn.textContent).toBe('停止')
    expect(sendBtn.classList.contains('send-button--stop')).toBe(true)

    resolveStream()
    await pending

    expect(root.querySelector('[data-role="kb-select"]').disabled).toBe(false)
    expect(root.querySelector('[data-role="new-chat"]').disabled).toBe(false)
    expect(root.querySelector('[data-role="agent-select"]').disabled).toBe(false)
    expect(sendBtn.textContent).toBe('发送')
    expect(sendBtn.classList.contains('send-button--stop')).toBe(false)
  })

  it('shows validation and stream errors without sending invalid questions', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onError }) => onError('后端错误'))
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    await app.sendCurrentMessage()
    expect(sendChatMessage).not.toHaveBeenCalled()

    root.querySelector('[data-role="composer-input"]').value = '没有知识库'
    await app.sendCurrentMessage()
    expect(root.querySelector('[data-role="message-list"]').textContent).toContain('请先选择知识库')

    const readyRoot = renderChatShell()
    const readyApp = initChatApp({
      root: readyRoot,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await readyApp.ready
    readyRoot.querySelector('[data-role="composer-input"]').value = '触发错误'
    await readyApp.sendCurrentMessage()

    expect(readyRoot.querySelector('[data-role="message-list"]').textContent).toContain('请求失败：后端错误')
  })

  it('uses markdown sanitizer when available and updates character limit state', async () => {
    window.marked = { parse: vi.fn((text) => `<strong>${text}</strong>`) }
    window.DOMPurify = { sanitize: vi.fn((html) => html.replace('<script>', '')) }
    const root = renderChatShell()
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage: vi.fn().mockImplementation(async ({ onDone }) => onDone('**完成**')) },
      storage: createStorage(),
    })
    await app.ready

    const input = root.querySelector('[data-role="composer-input"]')
    input.value = 'x'.repeat(501)
    input.dispatchEvent(new Event('input'))
    expect(input.classList.contains('is-over-limit')).toBe(true)

    input.value = '测试 Markdown'
    await app.sendCurrentMessage()

    expect(window.marked.parse).toHaveBeenCalledWith('**完成**')
    expect(window.DOMPurify.sanitize).toHaveBeenCalled()
  })

  it('does not send messages over 500 characters', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockResolvedValue()
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    const input = root.querySelector('[data-role="composer-input"]')
    input.value = 'x'.repeat(501)
    input.dispatchEvent(new Event('input'))
    await app.sendCurrentMessage()

    expect(sendChatMessage).not.toHaveBeenCalled()
    expect(input.classList.contains('is-over-limit')).toBe(true)
  })

  it('uses Enter to send and Shift+Enter to keep editing', async () => {
    const root = renderChatShell()
    const kbApi = {
      listKnowledgeBases: vi.fn().mockResolvedValue([
        { kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' },
      ]),
    }
    const sendChatMessage = vi.fn().mockResolvedValue()
    const app = initChatApp({ root, kbApi, chatApi: { sendChatMessage }, storage: createStorage() })
    await app.ready

    const input = root.querySelector('[data-role="composer-input"]')
    input.value = '不要发送'
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true }))
    expect(sendChatMessage).not.toHaveBeenCalled()

    input.value = '发送'
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await app.waitForIdle()
    expect(sendChatMessage).toHaveBeenCalledTimes(1)
  })

  it('sends through button click and resets through new-chat button', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockResolvedValue()
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '按钮发送'
    root.querySelector('[data-role="send-button"]').click()
    await app.waitForIdle()
    expect(sendChatMessage).toHaveBeenCalledTimes(1)

    app.addMessage('user', '需要清空')
    root.querySelector('[data-role="new-chat"]').click()
    expect(root.querySelectorAll('.message')).toHaveLength(1)
  })

  it('shows info toast once when meta indicates degraded', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async (opts) => {
      opts.onMeta?.({ degraded: true, message: '降级提示' })
      opts.onMeta?.({ degraded: true, message: '应被忽略' })
      opts.onDone?.('完成')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const infos = document.querySelectorAll('.toast--info')
    expect(infos.length).toBeGreaterThanOrEqual(1)
    expect(infos[infos.length - 1].textContent).toBe('降级提示')
  })

  it('toggles the mobile sidebar', async () => {
    const root = renderChatShell()
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([]) },
      chatApi: { sendChatMessage: vi.fn() },
      storage: createStorage(),
    })
    await app.ready

    const sidebar = root.querySelector('.chat-sidebar')
    root.querySelector('[data-role="sidebar-toggle"]').click()
    expect(sidebar.classList.contains('is-open')).toBe(true)

    root.querySelector('[data-role="sidebar-toggle"]').click()
    expect(sidebar.classList.contains('is-open')).toBe(false)
  })

  it('closes the mobile sidebar when the backdrop is clicked', async () => {
    const root = renderChatShell()
    await initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([]) },
      chatApi: { sendChatMessage: vi.fn() },
      storage: createStorage(),
    }).ready

    const sidebar = root.querySelector('.chat-sidebar')
    const backdrop = root.querySelector('[data-role="sidebar-backdrop"]')
    root.querySelector('[data-role="sidebar-toggle"]').click()
    expect(sidebar.classList.contains('is-open')).toBe(true)
    expect(backdrop.classList.contains('is-active')).toBe(true)

    backdrop.click()
    expect(sidebar.classList.contains('is-open')).toBe(false)
    expect(backdrop.classList.contains('is-active')).toBe(false)
  })

  // ── Sprint 6: Multi-round grouping ────────────────────────────────────────

  it('groups thought+tool_call+tool_result into iteration rounds', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onThought, onToolStart, onToolResult, onChunk, onDone }) => {
      // Round 1
      onThought({ content: '第一轮思考' })
      onToolStart({ type: 'tool_call', tool_name: 'knowledge_search', hint: '搜索知识库："换电"' })
      onToolResult({ tool_name: 'knowledge_search', summary: '找到 2 个片段', duration_ms: 80 })
      // Round 2
      onThought({ content: '第二轮思考' })
      onToolStart({ type: 'tool_call', tool_name: 'list_knowledge_chunks', hint: '阅读文档：《IFSSOP》' })
      onToolResult({ tool_name: 'list_knowledge_chunks', summary: '读取完成', duration_ms: 50 })
      onChunk('', '最终答案')
      onDone('最终答案')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '如何换电？'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const rounds = root.querySelectorAll('[data-role="reasoning-round"]')
    expect(rounds.length).toBeGreaterThanOrEqual(2)

    const firstRound = rounds[0]
    expect(firstRound.querySelector('[data-role="round-label"]').textContent).toContain('第 1 轮')
    expect(firstRound.textContent).toContain('第一轮思考')
    expect(firstRound.textContent).toContain('搜索知识库')
    expect(firstRound.textContent).toContain('找到 2 个片段')

    const secondRound = rounds[1]
    expect(secondRound.querySelector('[data-role="round-label"]').textContent).toContain('第 2 轮')
    expect(secondRound.textContent).toContain('第二轮思考')
    expect(secondRound.textContent).toContain('阅读文档')
  })

  it('starts a new round when second thought fires after first tool_result', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onThought, onToolStart, onToolResult, onDone }) => {
      onThought({ content: '思考一' })
      onToolStart({ type: 'tool_call', tool_name: 'knowledge_search', hint: '搜索' })
      onToolResult({ tool_name: 'knowledge_search', summary: '完成', duration_ms: 10 })
      onThought({ content: '思考二' })
      onDone('答案')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const rounds = root.querySelectorAll('[data-role="reasoning-round"]')
    expect(rounds.length).toBe(2)
    expect(rounds[0].textContent).toContain('思考一')
    expect(rounds[1].textContent).toContain('思考二')
  })

  it('shows single round (no grouping) for single-iteration agent response', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onThought, onChunk, onDone }) => {
      onThought({ content: '仅一次思考' })
      onChunk('', 'ok')
      onDone('ok')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const rounds = root.querySelectorAll('[data-role="reasoning-round"]')
    expect(rounds.length).toBe(1)
    expect(rounds[0].textContent).toContain('仅一次思考')
  })

  // ── Sprint 6: Citation highlighting ───────────────────────────────────────

  it('wraps 【来源：...】 citations in cite spans after done', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onChunk, onDone }) => {
      onChunk('', '电池需定期检查。【来源：《AGV维护手册》】')
      onDone('电池需定期检查。【来源：《AGV维护手册》】')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const bodies = root.querySelectorAll('[data-role="message-content"]')
    const aiBody = bodies[bodies.length - 1]
    expect(aiBody.querySelectorAll('.kb-citation').length).toBeGreaterThanOrEqual(1)
    const cites = [...aiBody.querySelectorAll('.kb-citation')]
    expect(cites.some((el) => el.textContent.includes('AGV维护手册'))).toBe(true)
  })

  // ── Sprint 10: Tool result details (collapsible) ─────────────────────────

  it('renders tool_result with details as a collapsible block', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onToolStart, onToolResult, onChunk, onDone }) => {
      onToolStart({ type: 'tool_call', tool_name: 'knowledge_search', hint: '搜索知识库："换电"' })
      onToolResult({
        tool_name: 'knowledge_search',
        summary: '找到 2 个结果',
        duration_ms: 80,
        details: '[\n  {"title": "STEP 1", "contents": "打开舱门"},\n  {"title": "STEP 2", "contents": "取出电池"}\n]',
      })
      onChunk('', '答案')
      onDone('答案')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    const detailsEls = root.querySelectorAll('[data-role="tool-result-details"]')
    expect(detailsEls.length).toBe(1)
    // 默认折叠
    expect(detailsEls[0].open).toBe(false)
    // 展开后能看到原始内容
    expect(detailsEls[0].textContent).toContain('打开舱门')
    expect(detailsEls[0].textContent).toContain('STEP 2')
  })

  it('omits collapsible block when tool_result has no details', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onToolStart, onToolResult, onChunk, onDone }) => {
      onToolStart({ type: 'tool_call', tool_name: 'final_answer', hint: '提交最终答案' })
      onToolResult({ tool_name: 'final_answer', summary: '已生成最终答案', duration_ms: 1 })
      onChunk('', '答案')
      onDone('答案')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    expect(root.querySelectorAll('[data-role="tool-result-details"]').length).toBe(0)
  })

  it('replays tool_result details from stored history', async () => {
    const root = renderChatShell({ withSessionList: true })
    window.location.hash = '#session=sess_details_replay'
    const sessionApi = {
      getSession: vi.fn().mockResolvedValue({ session_id: 'sess_details_replay', kb_id: 'agv_demo' }),
      listSessions: vi.fn().mockResolvedValue([]),
      fetchSessionMessages: vi.fn().mockResolvedValue([
        { id: 1, role: 'user', content: 'q', reasoning: {} },
        {
          id: 2,
          role: 'assistant',
          content: 'a',
          reasoning: {
            iterations: 1,
            events: [
              { type: 'tool_call', tool_name: 'knowledge_search', hint: '搜索知识库' },
              {
                type: 'tool_result',
                tool_name: 'knowledge_search',
                summary: '找到 1 个结果',
                duration_ms: 50,
                details: '原始 chunk: 打开舱门按钮',
              },
            ],
          },
        },
      ]),
      createSession: vi.fn(),
      renameSession: vi.fn(),
      deleteSession: vi.fn(),
    }
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage: vi.fn() },
      sessionApi,
      storage: createStorage(),
    })
    await app.ready

    const detailsEls = root.querySelectorAll('[data-role="tool-result-details"]')
    expect(detailsEls.length).toBe(1)
    expect(detailsEls[0].textContent).toContain('打开舱门按钮')
    window.location.hash = ''
  })

  // ── Sprint 9: Reasoning replay on session reload ─────────────────────────

  it('replays stored reasoning events into a new reasoning panel on session open', async () => {
    const root = renderChatShell({ withSessionList: true })
    window.location.hash = '#session=sess_replay'
    const sessionApi = {
      getSession: vi.fn().mockResolvedValue({ session_id: 'sess_replay', kb_id: 'agv_demo' }),
      listSessions: vi.fn().mockResolvedValue([]),
      fetchSessionMessages: vi.fn().mockResolvedValue([
        { id: 1, role: 'user', content: '换电步骤？', reasoning: {} },
        {
          id: 2,
          role: 'assistant',
          content: '请按以下步骤操作。【来源：BatterySOP】',
          reasoning: {
            iterations: 2,
            events: [
              { type: 'thought', content: '我需要查文档' },
              { type: 'tool_call', tool_name: 'knowledge_search', hint: '搜索知识库："换电"' },
              { type: 'tool_result', tool_name: 'knowledge_search', summary: '找到 5 个结果', duration_ms: 80 },
            ],
          },
        },
      ]),
      createSession: vi.fn(),
      renameSession: vi.fn(),
      deleteSession: vi.fn(),
    }
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage: vi.fn() },
      sessionApi,
      storage: createStorage(),
    })
    await app.ready

    expect(sessionApi.fetchSessionMessages).toHaveBeenCalledWith('sess_replay')
    const panels = root.querySelectorAll('[data-role="reasoning-steps"]')
    expect(panels.length).toBe(1)
    const log = root.querySelector('[data-role="reasoning-steps-log"]')
    expect(log.textContent).toContain('我需要查文档')
    expect(log.textContent).toContain('搜索知识库')
    expect(log.textContent).toContain('找到 5 个结果')
    // 引用高亮也在历史回放上工作
    expect(root.querySelectorAll('.kb-citation').length).toBeGreaterThanOrEqual(1)

    window.location.hash = ''
  })

  it('does not render reasoning panel when assistant message has no events', async () => {
    const root = renderChatShell({ withSessionList: true })
    window.location.hash = '#session=sess_quick'
    const sessionApi = {
      getSession: vi.fn().mockResolvedValue({ session_id: 'sess_quick', kb_id: 'agv_demo' }),
      listSessions: vi.fn().mockResolvedValue([]),
      fetchSessionMessages: vi.fn().mockResolvedValue([
        { id: 1, role: 'user', content: 'q', reasoning: {} },
        { id: 2, role: 'assistant', content: 'a', reasoning: {} },
      ]),
      createSession: vi.fn(),
      renameSession: vi.fn(),
      deleteSession: vi.fn(),
    }
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage: vi.fn() },
      sessionApi,
      storage: createStorage(),
    })
    await app.ready

    expect(root.querySelectorAll('[data-role="reasoning-steps"]').length).toBe(0)
    window.location.hash = ''
  })

  it('does not produce citation spans in quick mode when no 【来源:】 pattern', async () => {
    const root = renderChatShell()
    const sendChatMessage = vi.fn().mockImplementation(async ({ onChunk, onDone }) => {
      onChunk('', '普通回答无来源标注')
      onDone('普通回答无来源标注')
    })
    const app = initChatApp({
      root,
      kbApi: { listKnowledgeBases: vi.fn().mockResolvedValue([{ kb_id: 'agv_demo', name: 'AGV Demo', status: 'active' }]) },
      chatApi: { sendChatMessage },
      storage: createStorage(),
    })
    await app.ready

    root.querySelector('[data-role="composer-input"]').value = '问'
    await app.sendCurrentMessage()
    await app.waitForIdle()

    expect(root.querySelectorAll('.kb-citation').length).toBe(0)
  })
})
