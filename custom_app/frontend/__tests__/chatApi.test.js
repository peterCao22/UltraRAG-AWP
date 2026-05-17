import { beforeEach, describe, expect, it, vi } from 'vitest'

import { parseSsePayloads, sendChatMessage } from '../services/chatApi.js'

function createSseResponse(chunks, ok = true, status = 200) {
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk))
      }
      controller.close()
    },
  })

  return new Response(stream, {
    status,
    statusText: ok ? 'OK' : 'Error',
  })
}

describe('parseSsePayloads', () => {
  it('parses complete data lines and ignores blank frames', () => {
    const result = parseSsePayloads('data: {"type":"chunk","content":"A"}\n\n\n')

    expect(result.events).toEqual([{ type: 'chunk', content: 'A' }])
    expect(result.buffer).toBe('')
  })

  it('keeps partial frame in buffer', () => {
    const first = parseSsePayloads('data: {"type":"chunk"', '')
    const second = parseSsePayloads(',"content":"A"}\n\n', first.buffer)

    expect(first.events).toEqual([])
    expect(second.events).toEqual([{ type: 'chunk', content: 'A' }])
    expect(second.buffer).toBe('')
  })

  it('ignores invalid JSON frames without stopping later events', () => {
    const result = parseSsePayloads('data: nope\n\ndata: {"type":"done"}\n\n')

    expect(result.events).toEqual([{ type: 'done' }])
  })

  it('supports CRLF line endings and ignores DONE markers', () => {
    const result = parseSsePayloads('data: {"type":"chunk","content":"A"}\r\n\r\ndata: [DONE]\r\n\r\n')

    expect(result.events).toEqual([{ type: 'chunk', content: 'A' }])
  })
})

describe('sendChatMessage', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('posts chat request and dispatches all supported SSE callbacks', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createSseResponse([
      'data: {"type":"meta","degraded":true,"message":"fallback"}\n\n',
      'data: {"type":"thought","content":"thinking"}\n\n',
      'data: {"type":"tool_start","name":"search"}\n\n',
      'data: {"type":"tool_result","success":true}\n\n',
      'data: {"type":"chunk","content":"答"}\n\n',
      'data: {"type":"sources","sources":[{"title":"SOP"}]}\n\n',
      'data: {"type":"done"}\n\n',
    ]))
    vi.stubGlobal('fetch', fetchMock)

    const callbacks = {
      onMeta: vi.fn(),
      onThought: vi.fn(),
      onToolStart: vi.fn(),
      onToolResult: vi.fn(),
      onChunk: vi.fn(),
      onSources: vi.fn(),
      onDone: vi.fn(),
      onError: vi.fn(),
    }

    await sendChatMessage({
      kbId: 'agv_demo',
      question: '换电步骤',
      agentMode: 'agent',
      ...callbacks,
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/chat/stream',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kb_id: 'agv_demo',
          question: '换电步骤',
          stream: true,
          agent_mode: 'agent',
        }),
      }),
    )
    expect(callbacks.onMeta).toHaveBeenCalledWith(expect.objectContaining({ degraded: true }))
    expect(callbacks.onThought).toHaveBeenCalledWith(expect.objectContaining({ content: 'thinking' }))
    expect(callbacks.onToolStart).toHaveBeenCalledWith(expect.objectContaining({ name: 'search' }))
    expect(callbacks.onToolResult).toHaveBeenCalledWith(expect.objectContaining({ success: true }))
    expect(callbacks.onChunk).toHaveBeenCalledWith('答', '答')
    expect(callbacks.onSources).toHaveBeenCalledWith([{ title: 'SOP' }])
    expect(callbacks.onDone).toHaveBeenCalledWith('答')
    expect(callbacks.onError).not.toHaveBeenCalled()
  })

  it('defaults agentMode to quick', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createSseResponse(['data: {"type":"done"}\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    await sendChatMessage({ kbId: 'agv_demo', question: 'test' })

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).agent_mode).toBe('quick')
  })

  it('includes agent_id in body when provided', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createSseResponse(['data: {"type":"done"}\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    await sendChatMessage({
      kbId: 'agv_demo',
      question: 'test',
      agentId: 'builtin-quick',
    })

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).agent_id).toBe('builtin-quick')
  })

  it('omits agent_id when empty', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createSseResponse(['data: {"type":"done"}\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', agentId: '' })

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).agent_id).toBeUndefined()
  })

  it('invokes onStatus for SSE status events', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      createSseResponse([
        'data: {"type":"status","content":"加载中"}\n\n',
        'data: {"type":"done"}\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)
    const onStatus = vi.fn()
    await sendChatMessage({ kbId: 'k', question: 'q', onStatus, onDone: vi.fn() })
    expect(onStatus).toHaveBeenCalledWith('加载中')
  })

  it('includes session_id in JSON body when sessionId set', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createSseResponse(['data: {"type":"done"}\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', sessionId: 'sess_abc' })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.session_id).toBe('sess_abc')
  })

  it('onDone prefers server done.answer over streamed chunks', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      createSseResponse([
        'data: {"type":"chunk","content":"partial"}\n\n',
        'data: {"type":"done","answer":"## Final"}\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)
    const onDone = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'q', onDone })

    expect(onDone).toHaveBeenCalledWith('## Final')
  })

  it('includes profile in JSON body when profile true', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createSseResponse(['data: {"type":"done"}\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', profile: true })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.profile).toBe(true)
  })

  it('reports HTTP errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ message: 'bad request' }),
      { status: 400 },
    )))
    const onError = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', onError })

    expect(onError).toHaveBeenCalledWith('bad request')
  })

  it('falls back to status text for non-JSON HTTP errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('nope', { status: 500 })))
    const onError = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', onError })

    expect(onError).toHaveBeenCalledWith('HTTP 500')
  })

  it('reports unsupported streaming response bodies', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      body: null,
    }))
    const onError = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', onError })

    expect(onError).toHaveBeenCalledWith('浏览器不支持流式响应')
  })

  it('reports server error events', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(createSseResponse([
      'data: {"type":"error","message":"server failed"}\n\n',
    ])))
    const onError = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', onError })

    expect(onError).toHaveBeenCalledWith('server failed')
  })

  it('supports legacy sources payload and empty chunk content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(createSseResponse([
      'data: {"type":"chunk"}\n\n',
      'data: {"type":"sources","content":{"sources":[{"title":"Legacy"}]}}\n\n',
      'data: {"type":"error","content":"legacy error"}\n\n',
    ])))
    const onChunk = vi.fn()
    const onSources = vi.fn()
    const onError = vi.fn()

    await sendChatMessage({
      kbId: 'agv_demo',
      question: 'test',
      onChunk,
      onSources,
      onError,
    })

    expect(onChunk).toHaveBeenCalledWith('', '')
    expect(onSources).toHaveBeenCalledWith([{ title: 'Legacy' }])
    expect(onError).toHaveBeenCalledWith('legacy error')
  })

  it('reports fetch exceptions', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    const onError = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', onError })

    expect(onError).toHaveBeenCalledWith('offline')
  })

  it('calls onAbort with empty partial and not onError when fetch rejects AbortError', async () => {
    const err = new Error('The user aborted a request.')
    err.name = 'AbortError'
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(err))
    const onAbort = vi.fn()
    const onError = vi.fn()

    await sendChatMessage({
      kbId: 'agv_demo',
      question: 'test',
      onAbort,
      onError,
    })

    expect(onAbort).toHaveBeenCalledWith('')
    expect(onError).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('calls onError when wall-clock timeout fires before fetch completes', async () => {
    vi.useFakeTimers()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((_url, opts) => {
        const { signal } = opts
        return new Promise((_resolve, reject) => {
          const fail = () =>
            reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' }))
          if (signal.aborted) fail()
          else signal.addEventListener('abort', fail, { once: true })
        })
      }),
    )
    const onError = vi.fn()
    const onAbort = vi.fn()
    const p = sendChatMessage({
      kbId: 'agv_demo',
      question: 'test',
      fetchTimeoutMs: 40,
      onError,
      onAbort,
    })
    await vi.advanceTimersByTimeAsync(100)
    await p
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('请求超时'))
    expect(onAbort).not.toHaveBeenCalled()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('dispatches tool_call event type via onToolStart', async () => {
    const fetchMock = vi.fn().mockResolvedValue(createSseResponse([
      'data: {"type":"tool_call","tool_name":"knowledge_search","hint":"搜索知识库：\\"换电步骤\\""}\n\n',
      'data: {"type":"done"}\n\n',
    ]))
    vi.stubGlobal('fetch', fetchMock)
    const onToolStart = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', onToolStart, onDone: vi.fn() })

    expect(onToolStart).toHaveBeenCalledWith(expect.objectContaining({ type: 'tool_call', hint: expect.stringContaining('搜索') }))
  })

  it('flushes a final SSE frame without trailing blank line', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(createSseResponse([
      'data: {"type":"chunk","content":"尾"}',
    ])))
    const onChunk = vi.fn()

    await sendChatMessage({ kbId: 'agv_demo', question: 'test', onChunk })

    expect(onChunk).toHaveBeenCalledWith('尾', '尾')
  })
})
