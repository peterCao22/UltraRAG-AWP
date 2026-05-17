const DEFAULT_CHAT_ENDPOINT = '/api/chat/stream'

/**
 * 单次对话请求（含首字节等待与 SSE 读流）的最长等待时间。
 * 超时后中止 fetch 并走 onError，避免界面永久停在「思考中」。
 * 设为 0 可关闭（仅测试或自建网关另行限流时使用）。
 */
export const DEFAULT_CHAT_FETCH_TIMEOUT_MS = 300_000

export function parseSsePayloads(chunkText, previousBuffer = '') {
  const events = []
  const combined = `${previousBuffer}${chunkText}`.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  const frames = combined.split('\n\n')
  const rawBuffer = frames.pop() ?? ''
  const buffer = rawBuffer.trim() ? rawBuffer : ''

  for (const frame of frames) {
    const lines = frame.split('\n')

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue

      const payload = line.slice(6).trim()
      if (!payload || payload === '[DONE]') continue

      try {
        events.push(JSON.parse(payload))
      } catch {
        // 服务端偶发非 JSON 调试行时跳过，避免中断后续合法 SSE 事件。
      }
    }
  }

  return { events, buffer }
}

async function readErrorMessage(response) {
  try {
    const data = await response.json()
    return data.message || data.error || `HTTP ${response.status}`
  } catch {
    return `HTTP ${response.status}`
  }
}

function dispatchSseEvent(event, state, handlers) {
  if (event.type === 'status') {
    const t = String(event.content ?? '').trim()
    if (t) handlers.onStatus?.(t)
    return
  }

  if (event.type === 'meta') {
    handlers.onMeta?.(event)
    return
  }

  if (event.type === 'thought') {
    handlers.onThought?.(event)
    return
  }

  if (event.type === 'tool_call' || event.type === 'tool_start') {
    handlers.onToolStart?.(event)
    return
  }

  if (event.type === 'tool_result') {
    handlers.onToolResult?.(event)
    return
  }

  if (event.type === 'chunk') {
    const content = event.content ?? ''
    state.answer += content
    handlers.onChunk?.(content, state.answer)
    return
  }

  if (event.type === 'sources') {
    handlers.onSources?.(event.sources ?? event.content?.sources ?? [])
    return
  }

  if (event.type === 'done') {
    const fromServer = typeof event.answer === 'string' ? event.answer : ''
    const finalText = fromServer.trim() ? fromServer : state.answer
    handlers.onDone?.(finalText)
    return
  }

  if (event.type === 'error') {
    handlers.onError?.(event.message || event.content || '对话请求失败')
  }
}

export async function sendChatMessage({
  kbId,
  question,
  agentMode = 'quick',
  /** Phase 7: 可选 model_id；缺省时服务端取 ChatModelRepository 默认 */
  modelId = '',
  /** Phase 7.2.A: 可选 agent_id；缺省时按 agentMode 取 builtin */
  agentId = '',
  /** 与 Phase 1 会话落库对应；有值时写入 kb_session_messages */
  sessionId = '',
  /** 为 true 时请求体带 profile，服务端在 meta SSE 中返回 phase_timings_ms（Phase P 排障） */
  profile = false,
  endpoint = DEFAULT_CHAT_ENDPOINT,
  signal,
  /** 0 = 不启用总超时 */
  fetchTimeoutMs = DEFAULT_CHAT_FETCH_TIMEOUT_MS,
  onMeta,
  /** 服务端阶段提示（如加载索引、检索中），不参与最终 answer 拼接 */
  onStatus,
  onThought,
  onToolStart,
  onToolResult,
  onChunk,
  onSources,
  onDone,
  onError,
  onAbort,
} = {}) {
  const handlers = {
    onMeta,
    onStatus,
    onThought,
    onToolStart,
    onToolResult,
    onChunk,
    onSources,
    onDone,
    onError,
    onAbort,
  }
  const state = { answer: '' }

  const merged = new AbortController()
  if (signal) {
    if (signal.aborted) merged.abort()
    else signal.addEventListener('abort', () => merged.abort(), { once: true })
  }

  let timeoutId = null
  let timedOut = false
  if (typeof fetchTimeoutMs === 'number' && fetchTimeoutMs > 0) {
    timeoutId = globalThis.setTimeout?.(() => {
      timedOut = true
      merged.abort()
    }, fetchTimeoutMs)
  }

  try {
    const bodyPayload = {
      kb_id: kbId,
      question,
      stream: true,
      agent_mode: agentMode,
    }
    if (profile) {
      bodyPayload.profile = true
    }
    if (sessionId) {
      bodyPayload.session_id = sessionId
    }
    if (modelId) {
      bodyPayload.model_id = modelId
    }
    if (agentId) {
      bodyPayload.agent_id = agentId
    }
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyPayload),
      signal: merged.signal,
    })

    if (!response.ok) {
      handlers.onError?.(await readErrorMessage(response))
      return
    }

    const reader = response.body?.getReader()
    if (!reader) {
      handlers.onError?.('浏览器不支持流式响应')
      return
    }

    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      const parsed = parseSsePayloads(decoder.decode(value, { stream: true }), buffer)
      buffer = parsed.buffer

      for (const event of parsed.events) {
        dispatchSseEvent(event, state, handlers)
      }
    }

    if (buffer.trim()) {
      const parsed = parseSsePayloads('\n\n', buffer)
      for (const event of parsed.events) {
        dispatchSseEvent(event, state, handlers)
      }
    }
  } catch (error) {
    if (error?.name === 'AbortError') {
      if (timedOut && !signal?.aborted) {
        handlers.onError?.(
          '请求超时（长时间无响应）。请检查 vLLM/embedding 服务与网络，或稍后重试。',
        )
      } else {
        handlers.onAbort?.(state.answer)
      }
      return
    }
    handlers.onError?.(error?.message || '网络错误，请检查连接')
  } finally {
    if (timeoutId != null) {
      globalThis.clearTimeout?.(timeoutId)
    }
  }
}
