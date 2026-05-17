import {
  AGENT_STORAGE_KEY,
  applyStoredAgentMode,
  getSelectedAgent,
  mountAgentSelect,
  populateAgentSelect,
} from './components/agentSelector.js'
import { bindChatImageLightbox } from './components/imageLightbox.js'
import { attachMarkdownImageFallbacks } from './utils/brokenImagePlaceholder.js'
import { KB_STORAGE_KEY, populateKbSelect } from './components/kbSelector.js'
import {
  clearStreamingCursor,
  clearThinkingIndicator,
  createMessageElement,
  renderMarkdownContent,
  renderTextContent,
} from './components/chatMessage.js'
import { buildSourcesPanel } from './components/sourcePanel.js'
import { Toast } from './components/toast.js'
import { openConfirmModal } from './components/confirmModal.js'
import { sendChatMessage } from './services/chatApi.js'
import { listChatAgents, listChatModels } from './services/kbApi.js'

const MODEL_STORAGE_KEY = 'ULTRARAG_SELECTED_MODEL_ID'
import { listKnowledgeBases } from './services/kbApi.js'
import * as defaultSessionApi from './services/sessionApi.js'

function getRequiredElement(root, selector) {
  const element = root.querySelector(selector)
  if (!element) {
    throw new Error(`Missing required chat element: ${selector}`)
  }
  return element
}

function formatThoughtSnippet(ev) {
  if (ev == null) return ''
  if (typeof ev === 'string') return ev
  if (typeof ev.content === 'string') return ev.content
  if (typeof ev.text === 'string') return ev.text
  try {
    return JSON.stringify(ev).slice(0, 500)
  } catch {
    return String(ev)
  }
}

/**
 * 在 AI 气泡内插入可折叠「推理步骤」占位；无事件时可整体移除。
 *
 * Multi-round grouping: each `thought` event starts a new numbered round.
 * Within a round, `tool_call` and `tool_result` are appended as sub-lines.
 *
 * @param {HTMLElement} article
 * @param {HTMLElement} bodyEl
 */
function createReasoningStepsPanel(article, bodyEl) {
  const details = document.createElement('details')
  details.className = 'reasoning-steps'
  details.dataset.role = 'reasoning-steps'
  details.hidden = true
  const sum = document.createElement('summary')
  sum.className = 'reasoning-steps__summary'
  sum.textContent = '推理步骤'
  const log = document.createElement('div')
  log.className = 'reasoning-steps__log'
  log.dataset.role = 'reasoning-steps-log'
  details.append(sum, log)
  article.insertBefore(details, bodyEl)

  let roundCount = 0
  let currentRound = null
  let hasToolResult = false

  function _ensureRound() {
    if (!currentRound) {
      roundCount += 1
      const wrap = document.createElement('div')
      wrap.className = 'reasoning-round'
      wrap.dataset.role = 'reasoning-round'
      const label = document.createElement('span')
      label.className = 'reasoning-round__label'
      label.dataset.role = 'round-label'
      label.textContent = `第 ${roundCount} 轮`
      wrap.append(label)
      log.append(wrap)
      currentRound = wrap
      hasToolResult = false
      details.hidden = false
    }
  }

  function thought(text) {
    const t = String(text || '').trim()
    if (!t) return
    // A new thought after a tool_result closes the previous round
    if (currentRound && hasToolResult) {
      currentRound = null
    }
    _ensureRound()
    const p = document.createElement('p')
    p.className = 'reasoning-steps__thought'
    p.textContent = t
    currentRound.append(p)
  }

  function toolCall(hint) {
    const t = String(hint || '').trim()
    if (!t) return
    // 如果上一轮已有 tool_result，tool_call 应开启新轮次（后端可能没有 thought 事件）
    if (currentRound && hasToolResult) {
      currentRound = null
    }
    _ensureRound()
    const p = document.createElement('p')
    p.className = 'reasoning-steps__tool-call'
    p.textContent = t
    currentRound.append(p)
  }

  function toolResult(summary, detailsText) {
    const t = String(summary || '').trim()
    if (!t) return
    _ensureRound()
    const p = document.createElement('p')
    p.className = 'reasoning-steps__tool-result'
    p.textContent = t
    currentRound.append(p)
    const dt = String(detailsText || '').trim()
    if (dt) {
      const block = document.createElement('details')
      block.className = 'reasoning-steps__tool-details'
      block.dataset.role = 'tool-result-details'
      const sm = document.createElement('summary')
      sm.className = 'reasoning-steps__tool-details-summary'
      sm.textContent = '查看原始结果'
      const pre = document.createElement('pre')
      pre.className = 'reasoning-steps__tool-details-body'
      pre.textContent = dt
      block.append(sm, pre)
      currentRound.append(block)
    }
    hasToolResult = true
  }

  function line(text) {
    thought(text)
  }

  function finish() {
    if (!log.childElementCount) {
      details.remove()
      return
    }
    // Update summary with round count
    sum.textContent = `推理步骤 · ${roundCount} 轮`
    details.open = false
  }

  function remove() {
    details.remove()
  }

  return { line, thought, toolCall, toolResult, finish, remove }
}

/**
 * Scans all text nodes in `el` for 【来源：...】 patterns and wraps them
 * in <span class="kb-citation"> elements for styling.
 *
 * @param {HTMLElement} el
 */
function highlightKbCitations(el) {
  const CITE_RE = /【来源：([^】]+)】/g
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  const nodes = []
  let node
  while ((node = walker.nextNode())) {
    if (CITE_RE.test(node.nodeValue)) nodes.push(node)
    CITE_RE.lastIndex = 0
  }
  for (const textNode of nodes) {
    const frag = document.createDocumentFragment()
    let last = 0
    let m
    CITE_RE.lastIndex = 0
    while ((m = CITE_RE.exec(textNode.nodeValue)) !== null) {
      if (m.index > last) frag.append(document.createTextNode(textNode.nodeValue.slice(last, m.index)))
      const span = document.createElement('span')
      span.className = 'kb-citation'
      span.textContent = m[0]
      frag.append(span)
      last = m.index + m[0].length
    }
    if (last < textNode.nodeValue.length) frag.append(document.createTextNode(textNode.nodeValue.slice(last)))
    textNode.parentNode.replaceChild(frag, textNode)
  }
}

/**
 * 把已落库的推理事件序列回放到一个新的推理步骤面板里。
 * reasoning 形如 { iterations, events: [{type, content/hint/summary, ...}] }。
 * 无事件时不挂面板，避免空 details 出现在历史会话里。
 *
 * @param {HTMLElement} article
 * @param {HTMLElement} bodyEl
 * @param {object|null|undefined} reasoning
 */
function replayReasoningEvents(article, bodyEl, reasoning) {
  if (!reasoning || typeof reasoning !== 'object') return
  const events = Array.isArray(reasoning.events) ? reasoning.events : []
  if (!events.length) return
  const panel = createReasoningStepsPanel(article, bodyEl)
  for (const ev of events) {
    const t = ev?.type
    if (t === 'thought') panel.thought(ev.content || '')
    else if (t === 'tool_call') panel.toolCall(ev.hint || ev.tool_name || '')
    else if (t === 'tool_result') panel.toolResult(ev.summary || '', ev.details || '')
  }
  panel.finish()
}

function parseSessionIdFromHash() {
  const raw = (typeof window !== 'undefined' ? window.location.hash : '').replace(/^#/, '').trim()
  if (!raw.startsWith('session=')) return ''
  const rest = raw.slice('session='.length).trim()
  try {
    return decodeURIComponent(rest)
  } catch {
    return rest
  }
}

function setSessionHash(sessionId) {
  if (typeof window === 'undefined') return
  const path = `${window.location.pathname}${window.location.search}`
  if (!sessionId) {
    window.history.replaceState(null, '', path)
    return
  }
  window.history.replaceState(null, '', `${path}#session=${encodeURIComponent(sessionId)}`)
}

export function initChatApp({
  root = document.querySelector('[data-page="chat"]'),
  kbApi = { listKnowledgeBases },
  chatApi = { sendChatMessage },
  sessionApi = defaultSessionApi,
  storage = window.localStorage,
} = {}) {
  if (!root) return null

  const elements = {
    kbSelect: getRequiredElement(root, '[data-role="kb-select"]'),
    messageList: getRequiredElement(root, '[data-role="message-list"]'),
    agentSelect: getRequiredElement(root, '[data-role="agent-select"]'),
    charCount: getRequiredElement(root, '[data-role="char-count"]'),
    input: getRequiredElement(root, '[data-role="composer-input"]'),
    sendButton: getRequiredElement(root, '[data-role="send-button"]'),
    newChatButton: getRequiredElement(root, '[data-role="new-chat"]'),
    sidebarToggle: root.querySelector('[data-role="sidebar-toggle"]'),
    sidebar: root.querySelector('.chat-sidebar'),
    sidebarBackdrop: root.querySelector('[data-role="sidebar-backdrop"]'),
    sessionList: root.querySelector('[data-role="session-list"]'),
    modelChip: root.querySelector('[data-role="model-chip"]'),
    modelChipName: root.querySelector('[data-role="model-chip-name"]'),
  }

  const state = {
    knowledgeBases: [],
    selectedKbId: '',
    currentSessionId: null,
    isStreaming: false,
    pendingSend: Promise.resolve(),
    streamAbort: null,
    // Phase 7: 对话模型 chip
    chatModels: [],
    selectedModelId: '',
  }

  mountAgentSelect(elements.agentSelect)
  bindChatImageLightbox(elements.messageList)
  initModelChip()

  function scrollMessageListToBottom() {
    const list = elements.messageList
    list.scrollTop = list.scrollHeight
    const raf = typeof window !== 'undefined' ? window.requestAnimationFrame : null
    if (typeof raf === 'function') {
      raf(() => {
        list.scrollTop = list.scrollHeight
      })
    }

    const timers = typeof window !== 'undefined' ? window : null
    if (timers?.setTimeout) {
      ;[40, 120, 280].forEach((delay) => {
        timers.setTimeout(() => {
          list.scrollTop = list.scrollHeight
        }, delay)
      })
    }
  }

  function renderWelcome() {
    elements.messageList.innerHTML = ''
    const { article } = createMessageElement('ai', '您好！请选择知识库后开始提问。')
    const sourcePlaceholder = document.createElement('div')
    sourcePlaceholder.className = 'source-placeholder'
    sourcePlaceholder.textContent = '引用来源将在回答完成后展示。'
    article.append(sourcePlaceholder)
    elements.messageList.append(article)
  }

  // ── Phase 7: 对话模型 chip ──────────────────────────────────────────────

  function updateModelChipLabel() {
    if (!elements.modelChipName) return
    if (!state.chatModels.length) {
      elements.modelChipName.textContent = '默认模型'
      return
    }
    const sel = state.chatModels.find((m) => m.model_id === state.selectedModelId)
        || state.chatModels.find((m) => m.is_default)
        || state.chatModels[0]
    elements.modelChipName.textContent = sel ? sel.name : '默认模型'
  }

  async function initModelChip() {
    if (!elements.modelChip) return
    let stored = ''
    try {
      stored = storage?.getItem(MODEL_STORAGE_KEY) || ''
    } catch {
      stored = ''
    }
    state.selectedModelId = stored
    try {
      const models = await listChatModels()
      state.chatModels = models
      // 校验 stored 是否还在 enabled 列表里；不在就清掉
      if (stored && !models.some((m) => m.model_id === stored)) {
        state.selectedModelId = ''
        try {
          storage?.removeItem(MODEL_STORAGE_KEY)
        } catch {
          /* noop */
        }
      }
      // 没选过 → 用 default
      if (!state.selectedModelId) {
        const def = models.find((m) => m.is_default)
        if (def) state.selectedModelId = def.model_id
      }
    } catch (err) {
      // 列表拉取失败不影响发消息（缺省走 .env）
      state.chatModels = []
      if (typeof console !== 'undefined') {
        console.warn('[UltraRAG] listChatModels failed', err)
      }
    }
    updateModelChipLabel()

    elements.modelChip.addEventListener('click', (e) => {
      e.stopPropagation()
      openModelPicker()
    })
  }

  function openModelPicker() {
    closeModelPicker()
    if (!elements.modelChip) return

    const overlay = document.createElement('div')
    overlay.className = 'model-picker-overlay'
    overlay.dataset.role = 'model-picker-overlay'

    const dropdown = document.createElement('div')
    dropdown.className = 'model-picker-dropdown'

    // 定位到 chip 上方（避免被发送按钮遮挡）
    const rect = elements.modelChip.getBoundingClientRect()
    dropdown.style.left = `${Math.max(8, rect.left)}px`
    dropdown.style.bottom = `${Math.max(8, window.innerHeight - rect.top + 6)}px`

    if (!state.chatModels.length) {
      const empty = document.createElement('div')
      empty.className = 'model-picker-empty'
      empty.textContent = '尚未配置任何模型；前往 /admin → 模型管理 添加。'
      dropdown.append(empty)
    } else {
      for (const m of state.chatModels) {
        const item = document.createElement('div')
        item.className = 'model-picker-item'
        if (m.model_id === state.selectedModelId) item.classList.add('is-active')
        const name = document.createElement('div')
        name.className = 'model-picker-item__name'
        name.textContent = m.name + (m.is_default ? ' · 默认' : '')
        const meta = document.createElement('div')
        meta.className = 'model-picker-item__meta'
        meta.textContent = `${m.provider} · ${m.model_name}`
        item.append(name, meta)
        item.addEventListener('click', () => {
          state.selectedModelId = m.model_id
          try {
            storage?.setItem(MODEL_STORAGE_KEY, m.model_id)
          } catch {
            /* noop */
          }
          updateModelChipLabel()
          closeModelPicker()
        })
        dropdown.append(item)
      }
    }

    overlay.append(dropdown)
    overlay.addEventListener('click', (ev) => {
      if (ev.target === overlay) closeModelPicker()
    })
    document.body.append(overlay)
  }

  function closeModelPicker() {
    document
      .querySelectorAll('[data-role="model-picker-overlay"]')
      .forEach((node) => node.remove())
  }

  function addMessage(role, content, options = {}) {
    const { article, body } = createMessageElement(role, content, options)
    elements.messageList.append(article)
    if (options.scroll !== false) {
      scrollMessageListToBottom()
    }
    return { article, body }
  }

  function setStreaming(nextValue) {
    state.isStreaming = nextValue
    elements.input.disabled = nextValue
    elements.kbSelect.disabled = nextValue
    elements.newChatButton.disabled = nextValue
    elements.agentSelect.disabled = nextValue
    if (nextValue) {
      elements.sendButton.disabled = false
      elements.sendButton.textContent = '停止'
      elements.sendButton.setAttribute('aria-label', '停止生成')
      elements.sendButton.classList.add('send-button--stop')
    } else {
      elements.sendButton.textContent = '发送'
      elements.sendButton.setAttribute('aria-label', '发送')
      elements.sendButton.classList.remove('send-button--stop')
    }
  }

  function updateCharCount() {
    const length = elements.input.value.length
    elements.charCount.textContent = `${length}/500`
    elements.input.classList.toggle('is-over-limit', length > 500)
  }

  function renderKnowledgeBases() {
    state.selectedKbId = populateKbSelect(elements.kbSelect, state.knowledgeBases, storage)
    applyStoredAgentMode(elements.agentSelect, storage)
  }

  async function loadKnowledgeBases() {
    try {
      state.knowledgeBases = await kbApi.listKnowledgeBases()
      renderKnowledgeBases()
      renderWelcome()
      await syncSessionFromUrl()
    } catch (error) {
      const msg = error?.message || '未知错误'
      Toast.show(`知识库加载失败：${msg}`, 'error')
      addMessage('ai error', `知识库加载失败：${msg}`)
    }
  }

  function resetChat() {
    renderWelcome()
    elements.input.focus()
  }

  function setChatSidebarOpen(open) {
    elements.sidebar?.classList.toggle('is-open', Boolean(open))
    if (elements.sidebarBackdrop) {
      elements.sidebarBackdrop.classList.toggle('is-active', Boolean(open))
      elements.sidebarBackdrop.setAttribute('aria-hidden', open ? 'false' : 'true')
    }
  }

  /** 重命名弹窗：返回新标题字符串，用户取消返回 null */
  function openRenameModal(currentTitle) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div')
      overlay.className = 'modal-overlay'
      overlay.setAttribute('role', 'dialog')
      overlay.setAttribute('aria-modal', 'true')

      const card = document.createElement('div')
      card.className = 'modal-card'

      const title = document.createElement('h2')
      title.className = 'modal-title'
      title.textContent = '重命名会话'

      const input = document.createElement('input')
      input.type = 'text'
      input.className = 'field'
      input.value = currentTitle
      input.style.marginBottom = 'var(--space-lg)'
      input.maxLength = 100

      const row = document.createElement('div')
      row.className = 'modal-actions'

      const btnCancel = document.createElement('button')
      btnCancel.type = 'button'
      btnCancel.className = 'button-secondary'
      btnCancel.textContent = '取消'

      const btnOk = document.createElement('button')
      btnOk.type = 'button'
      btnOk.className = 'button-primary'
      btnOk.textContent = '确定'

      function cleanup(result) {
        overlay.remove()
        document.removeEventListener('keydown', onKey)
        resolve(result)
      }

      function onKey(e) {
        if (e.key === 'Escape') cleanup(null)
        if (e.key === 'Enter') {
          const v = input.value.trim()
          if (v) cleanup(v)
        }
      }

      btnCancel.addEventListener('click', () => cleanup(null))
      btnOk.addEventListener('click', () => {
        const v = input.value.trim()
        if (v) cleanup(v)
      })
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) cleanup(null)
      })

      row.append(btnCancel, btnOk)
      card.append(title, input, row)
      overlay.append(card)
      document.body.append(overlay)
      document.addEventListener('keydown', onKey)

      // 全选已有标题，方便直接覆盖
      requestAnimationFrame(() => {
        input.focus()
        input.select()
      })
    })
  }

  /** 关闭页面上所有已打开的会话下拉菜单 */
  function closeAllSessionMenus() {
    document.querySelectorAll('.session-menu-dropdown').forEach((d) => {
      d.hidden = true
    })
  }

  async function handleRenameSession(sessionId, currentTitle) {
    const newTitle = await openRenameModal(currentTitle)
    if (!newTitle) return
    try {
      await sessionApi.renameSession(sessionId, newTitle)
      await refreshSessionList()
      Toast.show('会话已重命名', 'success')
    } catch (e) {
      Toast.show(`重命名失败：${e?.message || e}`, 'error')
    }
  }

  async function handleDeleteSession(sessionId) {
    const confirmed = await openConfirmModal({
      message: '确定要删除这个会话吗？删除后无法恢复。',
      confirmLabel: '删除',
      cancelLabel: '取消',
    })
    if (!confirmed) return
    try {
      await sessionApi.deleteSession(sessionId)
      if (state.currentSessionId === sessionId) {
        state.currentSessionId = null
        setSessionHash('')
        resetChat()
      }
      await refreshSessionList()
      Toast.show('会话已删除', 'success')
    } catch (e) {
      Toast.show(`删除失败：${e?.message || e}`, 'error')
    }
  }

  function renderSessionListHost(sessions) {
    if (!elements.sessionList) return
    const host = elements.sessionList
    host.innerHTML = ''
    if (!sessions.length) {
      const p = document.createElement('p')
      p.className = 'muted'
      p.textContent = '暂无历史会话，点击「新建对话」开始。'
      host.append(p)
      return
    }
    const ul = document.createElement('ul')
    ul.className = 'session-list'
    for (const s of sessions) {
      const li = document.createElement('li')
      li.className = 'session-list__item-wrap'

      // 标题按钮
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = `session-list__item-title${s.session_id === state.currentSessionId ? ' is-active' : ''}`
      btn.textContent = (s.title || s.session_id || '').slice(0, 80)
      btn.title = s.title || s.session_id || ''
      btn.addEventListener('click', () => {
        void openSession(s.session_id)
        setChatSidebarOpen(false)
      })

      // "···" 菜单触发按钮
      const menuBtn = document.createElement('button')
      menuBtn.type = 'button'
      menuBtn.className = 'session-menu-btn'
      menuBtn.setAttribute('aria-label', '更多操作')
      menuBtn.textContent = '···'

      // 下拉菜单
      const dropdown = document.createElement('div')
      dropdown.className = 'session-menu-dropdown'
      dropdown.hidden = true

      const renameItem = document.createElement('button')
      renameItem.type = 'button'
      renameItem.className = 'session-menu-item'
      renameItem.textContent = '重命名'
      renameItem.addEventListener('click', (e) => {
        e.stopPropagation()
        dropdown.hidden = true
        void handleRenameSession(s.session_id, s.title || '')
      })

      const deleteItem = document.createElement('button')
      deleteItem.type = 'button'
      deleteItem.className = 'session-menu-item session-menu-item--danger'
      deleteItem.textContent = '删除'
      deleteItem.addEventListener('click', (e) => {
        e.stopPropagation()
        dropdown.hidden = true
        void handleDeleteSession(s.session_id)
      })

      menuBtn.addEventListener('click', (e) => {
        e.stopPropagation()
        const wasHidden = dropdown.hidden
        closeAllSessionMenus()
        dropdown.hidden = !wasHidden
      })

      dropdown.append(renameItem, deleteItem)
      li.append(btn, menuBtn, dropdown)
      ul.append(li)
    }
    host.append(ul)
  }

  async function refreshSessionList() {
    if (!elements.sessionList || !state.selectedKbId) return
    try {
      const items = await sessionApi.listSessions(state.selectedKbId)
      renderSessionListHost(items)
    } catch {
      /* 侧栏列表失败不阻塞主对话 */
    }
  }

  async function openSession(sessionId, { skipHashWrite = false } = {}) {
    if (!elements.sessionList || !sessionId) return
    const meta = await sessionApi.getSession(sessionId)
    if (meta?.kb_id && meta.kb_id !== state.selectedKbId) {
      elements.kbSelect.value = meta.kb_id
      state.selectedKbId = meta.kb_id
      storage.setItem(KB_STORAGE_KEY, state.selectedKbId)
      const label =
        state.knowledgeBases.find((k) => k.kb_id === meta.kb_id)?.name ||
        elements.kbSelect.options[elements.kbSelect.selectedIndex]?.textContent?.trim() ||
        meta.kb_id
      Toast.show(
        `已按该历史会话切换知识库为「${label}」。请确认与当前问题一致；若不对请「新建对话」或清空地址栏 #session= 后再选库。`,
        'info',
        8000,
      )
    }
    state.currentSessionId = sessionId
    if (!skipHashWrite) setSessionHash(sessionId)
    const msgs = await sessionApi.fetchSessionMessages(sessionId)
    elements.messageList.innerHTML = ''
    for (const m of msgs) {
      if (m.role === 'user') addMessage('user', m.content || '')
      else {
        const { article, body } = addMessage('ai', '')
        replayReasoningEvents(article, body, m.reasoning)
        renderMarkdownContent(body, m.content || '')
        highlightKbCitations(body)
        attachMarkdownImageFallbacks(body)
      }
    }
    if (!msgs.length) renderWelcome()
    scrollMessageListToBottom()
    await refreshSessionList()
  }

  async function syncSessionFromUrl() {
    if (!elements.sessionList) return
    const sid = parseSessionIdFromHash()
    if (!sid) {
      await refreshSessionList()
      return
    }
    try {
      await openSession(sid, { skipHashWrite: true })
    } catch {
      setSessionHash('')
      await refreshSessionList()
    }
  }

  async function sendCurrentMessage(options = {}) {
    const resendText = typeof options.resendText === 'string' ? options.resendText : ''
    const fromResend = Boolean(resendText.trim())
    const question = fromResend ? resendText.trim() : elements.input.value.trim()
    if (!question || state.isStreaming) return
    if (!fromResend && elements.input.value.length > 500) {
      updateCharCount()
      return
    }

    if (!state.selectedKbId) {
      addMessage('ai error', '请先选择知识库。')
      return
    }

    const { agentId, agentMode } = getSelectedAgent(elements.agentSelect)

    if (!fromResend) {
      if (!state.currentSessionId && elements.sessionList) {
        try {
          const row = await sessionApi.createSession(state.selectedKbId, {
            agentMode,
          })
          state.currentSessionId = row.session_id
          setSessionHash(state.currentSessionId)
          await refreshSessionList()
        } catch (e) {
          const msg = e?.message || String(e)
          Toast.show(`无法创建会话：${msg}`, 'error')
          return
        }
      }
      addMessage('user', question, { scroll: false })
      elements.input.value = ''
      updateCharCount()
    }

    const aiMessage = addMessage('ai', '', { streaming: true, thinking: true, scroll: false })
    const reasoning = createReasoningStepsPanel(aiMessage.article, aiMessage.body)
    let sourcesPanelEl = null
    let degradedNotified = false
    let firstChunk = true
    let accumulatedAnswer = ''
    const ac = new AbortController()
    state.streamAbort = ac
    setStreaming(true)
    scrollMessageListToBottom()

    let profileRequest = false
    try {
      const u = new URL(window.location.href)
      profileRequest =
        u.searchParams.get('profile') === '1' || u.searchParams.get('debug_profile') === '1'
    } catch {
      profileRequest = false
    }
    if (!profileRequest) {
      try {
        profileRequest = storage.getItem('ultrarag_chat_profile') === '1'
      } catch {
        profileRequest = false
      }
    }

    try {
      await chatApi.sendChatMessage({
        kbId: state.selectedKbId,
        question,
        agentMode,
        agentId,
        modelId: state.selectedModelId || '',
        sessionId: state.currentSessionId || '',
        profile: profileRequest,
        signal: ac.signal,
        onStatus: (text) => {
          if (text) renderTextContent(aiMessage.body, text)
        },
        onMeta: (ev) => {
          if (ev.phase_timings_ms && typeof console !== 'undefined' && console.info) {
            console.info('[UltraRAG] phase_timings_ms', ev.phase_timings_ms)
          }
          if (degradedNotified) return
          if (ev.degraded === true) {
            degradedNotified = true
            const msg = ev.message || '当前请求已降级为快速问答模式。'
            Toast.show(msg, 'info', 5000)
          }
        },
        onThought: (ev) => {
          reasoning.thought(formatThoughtSnippet(ev))
          scrollMessageListToBottom()
        },
        onToolStart: (ev) => {
          const label = ev?.hint || ev?.name || String(ev?.tool_name ?? '?')
          reasoning.toolCall(label)
          scrollMessageListToBottom()
        },
        onToolResult: (ev) => {
          const summary = ev?.summary
          if (summary) {
            reasoning.toolResult(summary, ev?.details || '')
            scrollMessageListToBottom()
          }
        },
        onChunk: (_chunk, answer) => {
          accumulatedAnswer = answer
          if (firstChunk) {
            firstChunk = false
            clearThinkingIndicator(aiMessage.body)
          }
          renderTextContent(aiMessage.body, answer)
          scrollMessageListToBottom()
        },
        onSources: (sources) => {
          if (!sources?.length) return
          if (sourcesPanelEl) {
            sourcesPanelEl.remove()
            sourcesPanelEl = null
          }
          const panel = buildSourcesPanel(sources)
          if (!panel) return
          sourcesPanelEl = panel
          aiMessage.article.append(panel)
          scrollMessageListToBottom()
        },
        onDone: (answer) => {
          clearThinkingIndicator(aiMessage.body)
          clearStreamingCursor(aiMessage.body)
          renderMarkdownContent(aiMessage.body, answer)
          highlightKbCitations(aiMessage.body)
          attachMarkdownImageFallbacks(aiMessage.body)
          reasoning.finish()
          scrollMessageListToBottom()
          void refreshSessionList()
        },
        onAbort: (partial) => {
          clearThinkingIndicator(aiMessage.body)
          clearStreamingCursor(aiMessage.body)
          const text = typeof partial === 'string' ? partial.trim() : ''
          if (text) {
            renderMarkdownContent(aiMessage.body, text)
            attachMarkdownImageFallbacks(aiMessage.body)
          } else {
            renderTextContent(aiMessage.body, '已停止生成')
          }
          reasoning.finish()
          scrollMessageListToBottom()
        },
        onError: (message) => {
          reasoning.remove()
          Toast.show(`对话失败：${message}`, 'error')
          aiMessage.article.classList.add('error')
          clearThinkingIndicator(aiMessage.body)
          clearStreamingCursor(aiMessage.body)
          renderTextContent(aiMessage.body, `请求失败：${message}`)
          const retry = document.createElement('button')
          retry.type = 'button'
          retry.className = 'button-secondary message-retry'
          retry.textContent = '重试'
          retry.addEventListener('click', () => {
            if (state.isStreaming) return
            retry.remove()
            aiMessage.article.remove()
            void sendCurrentMessage({ resendText: question })
          })
          aiMessage.article.append(retry)
          scrollMessageListToBottom()
        },
      })
    } finally {
      state.streamAbort = null
      setStreaming(false)
    }
  }

  elements.kbSelect.addEventListener('change', () => {
    if (state.isStreaming) return
    state.selectedKbId = elements.kbSelect.value
    storage.setItem(KB_STORAGE_KEY, state.selectedKbId)
    state.currentSessionId = null
    setSessionHash('')
    resetChat()
    void refreshSessionList()
  })

  elements.agentSelect.addEventListener('change', () => {
    storage.setItem(AGENT_STORAGE_KEY, elements.agentSelect.value)
  })

  elements.newChatButton.addEventListener('click', async () => {
    if (state.isStreaming) return
    if (!state.selectedKbId) {
      Toast.show('请先选择知识库', 'info')
      return
    }
    if (!elements.sessionList) {
      resetChat()
      return
    }
    try {
      const { agentMode } = getSelectedAgent(elements.agentSelect)
      const row = await sessionApi.createSession(state.selectedKbId, {
        agentMode,
      })
      state.currentSessionId = row.session_id
      setSessionHash(state.currentSessionId)
      resetChat()
      await refreshSessionList()
    } catch (e) {
      const msg = e?.message || String(e)
      Toast.show(`新建会话失败：${msg}`, 'error')
    }
  })

  // 点击任意其他区域时关闭所有会话下拉菜单
  document.addEventListener('click', () => closeAllSessionMenus())

  elements.sidebarToggle?.addEventListener('click', () => {
    const next = !elements.sidebar?.classList.contains('is-open')
    setChatSidebarOpen(next)
  })
  elements.sidebarBackdrop?.addEventListener('click', () => {
    setChatSidebarOpen(false)
  })
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return
    if (!elements.sidebar?.classList.contains('is-open')) return
    setChatSidebarOpen(false)
  })
  window.addEventListener('hashchange', () => {
    void syncSessionFromUrl()
  })
  elements.input.addEventListener('input', updateCharCount)
  elements.input.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey) return
    event.preventDefault()
    state.pendingSend = sendCurrentMessage()
  })
  elements.sendButton.addEventListener('click', () => {
    if (state.isStreaming) {
      state.streamAbort?.abort()
      return
    }
    state.pendingSend = sendCurrentMessage()
  })

  renderWelcome()
  updateCharCount()
  applyStoredAgentMode(elements.agentSelect, storage)

  // Phase 7.2.A: 用后端 /api/chat/agents 替换静态 dropdown 选项
  initAgentSelect()
  // 选择变化时把 agent_id 写回 localStorage（沿用 AGENT_STORAGE_KEY，存的是 agent_id）
  elements.agentSelect.addEventListener('change', () => {
    try {
      storage?.setItem(AGENT_STORAGE_KEY, elements.agentSelect.value || '')
    } catch {
      /* noop */
    }
  })

  async function initAgentSelect() {
    try {
      const agents = await listChatAgents()
      if (agents && agents.length) {
        populateAgentSelect(elements.agentSelect, agents)
        mountAgentSelect(elements.agentSelect)
        applyStoredAgentMode(elements.agentSelect, storage)
      }
    } catch (err) {
      // 列表拉不到 → 保留 index.html 里的 quick/agent 兜底，向后兼容
      if (typeof console !== 'undefined') {
        console.warn('[UltraRAG] listChatAgents failed', err)
      }
    }
  }

  const controller = {
    ready: loadKnowledgeBases(),
    addMessage,
    sendCurrentMessage,
    waitForIdle: () => state.pendingSend,
    getState: () => ({ ...state }),
  }

  root.dataset.ready = 'true'
  root.chatApi = { sendChatMessage: chatApi.sendChatMessage }
  root.chatApp = controller

  return controller
}

initChatApp()
