import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('Phase 3 frontend boot scripts', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    vi.resetModules()
    vi.restoreAllMocks()
  })

  it('marks chat shell as ready', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: [] }))))
    document.body.innerHTML = `
      <div data-page="chat">
        <button type="button" data-role="new-chat">+ 新建对话</button>
        <select data-role="kb-select"></select>
        <section data-role="message-list"></section>
        <select data-role="agent-select"><option value="quick">快速问答</option></select>
        <span data-role="char-count">0/500</span>
        <textarea data-role="composer-input"></textarea>
        <button type="button" data-role="send-button">发送</button>
      </div>
    `

    await import('../main.js')

    const chatShell = document.querySelector('[data-page="chat"]')
    expect(chatShell.dataset.ready).toBe('true')
    expect(chatShell.chatApi.sendChatMessage).toBeTypeOf('function')
  })

  it('does nothing when chat shell is absent', async () => {
    document.body.innerHTML = '<div data-page="other"></div>'

    await import('../main.js')

    expect(document.querySelector('[data-page="other"]').dataset.ready).toBeUndefined()
  })

  it('marks admin shell as ready', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: [] }))))
    document.body.innerHTML = `
      <div data-page="admin">
        <header><div class="admin-header-left">
          <button type="button" data-role="admin-sidebar-toggle">☰</button>
          <strong data-role="admin-title"></strong>
        </div></header>
        <div class="admin-body">
          <nav data-role="admin-sidebar"><a href="#/" data-nav="kb">KB</a></nav>
          <main data-role="admin-outlet"></main>
          <div data-role="admin-sidebar-backdrop"></div>
        </div>
      </div>
    `

    await import('../admin.js')

    expect(document.querySelector('[data-page="admin"]').dataset.ready).toBe('true')
  })

  it('does nothing when admin shell is absent', async () => {
    document.body.innerHTML = '<div data-page="other"></div>'

    await import('../admin.js')

    expect(document.querySelector('[data-page="other"]').dataset.ready).toBeUndefined()
  })
})
