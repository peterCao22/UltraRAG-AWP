import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { initAdminApp } from '../admin.js'

function mockKbApi(overrides = {}) {
  // Phase 6.1：默认 listDocuments 返回 {documents, summary} 形状
  const emptyBundle = {
    documents: [],
    summary: {
      pending: 0, parsing: 0, embedding: 0, indexing: 0,
      completed: 0, failed: 0, deleting: 0,
    },
  }
  return {
    batchDocumentStatus: vi.fn().mockResolvedValue([]),
    batchReindexDocuments: vi.fn(),
    listKnowledgeBases: vi.fn().mockResolvedValue([]),
    createKnowledgeBase: vi.fn(),
    createIngestJob: vi.fn(),
    deleteDocument: vi.fn(),
    deleteKnowledgeBase: vi.fn(),
    getAgentConfig: vi.fn().mockResolvedValue({ all_tools: [], enabled_tools: [] }),
    getJobProgress: vi.fn(),
    getKnowledgeBase: vi.fn(),
    listDocumentChunks: vi.fn().mockResolvedValue({ chunks: [], doc_id: '', doc_stem: '' }),
    listDocuments: vi.fn().mockResolvedValue(emptyBundle),
    listJobs: vi.fn(),
    reindexDocument: vi.fn(),
    retryDocument: vi.fn(),
    updateAgentConfig: vi.fn(),
    uploadKbDocuments: vi.fn(),
    ...overrides,
  }
}

function renderAdminRoot() {
  document.body.innerHTML = `
    <div data-page="admin">
      <header class="admin-header">
        <div class="admin-header-left">
          <button type="button" data-role="admin-sidebar-toggle">☰</button>
          <strong data-role="admin-title"></strong>
        </div>
      </header>
      <div class="admin-body">
        <nav data-role="admin-sidebar">
          <a href="#/" data-nav="kb">知识库管理</a>
          <a href="#/status" data-nav="status">系统状态</a>
        </nav>
        <main data-role="admin-outlet"></main>
        <div data-role="admin-sidebar-backdrop" aria-hidden="true"></div>
      </div>
    </div>
  `
  return document.querySelector('[data-page="admin"]')
}

describe('initAdminApp', () => {
  beforeEach(() => {
    window.location.hash = '#/'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    window.location.hash = '#/'
  })

  it('renders kb cards from listKnowledgeBases', async () => {
    const root = renderAdminRoot()
    const kbApi = mockKbApi({
      listKnowledgeBases: vi.fn().mockResolvedValue([
        { kb_id: 'k1', name: 'KB One', status: 'active', document_count: 2 },
      ]),
    })
    const app = initAdminApp({ root, kbApi })
    await app.ready
    expect(kbApi.listKnowledgeBases).toHaveBeenCalledWith({ purpose: 'admin' })
    expect(root.querySelector('.admin-kb-card h2')?.textContent).toBe('KB One')
    app.destroy()
  })

  it('renders detail view for #/kb/:id', async () => {
    window.location.hash = '#/kb/k1'
    const root = renderAdminRoot()
    const kbApi = mockKbApi({
      getKnowledgeBase: vi.fn().mockResolvedValue({
        kb_id: 'k1',
        name: 'Detail KB',
        status: 'active',
        document_count: 0,
      }),
      listDocuments: vi.fn().mockResolvedValue({
        documents: [],
        summary: {
          pending: 0, parsing: 0, embedding: 0, indexing: 0,
          completed: 0, failed: 0, deleting: 0,
        },
      }),
      listJobs: vi.fn().mockResolvedValue([]),
    })
    const app = initAdminApp({ root, kbApi })
    await app.ready
    expect(kbApi.getKnowledgeBase).toHaveBeenCalledWith('k1')
    expect(root.querySelector('h1')?.textContent).toBe('Detail KB')
    expect(root.textContent).toContain('重建索引')
    app.destroy()
  })

  it('toggles admin sidebar on narrow-screen button', async () => {
    window.location.hash = '#/'
    const root = renderAdminRoot()
    const kbApi = mockKbApi({
      listKnowledgeBases: vi.fn().mockResolvedValue([]),
    })
    const app = initAdminApp({ root, kbApi })
    await app.ready
    const sidebar = root.querySelector('[data-role="admin-sidebar"]')
    root.querySelector('[data-role="admin-sidebar-toggle"]').click()
    expect(sidebar.classList.contains('is-open')).toBe(true)
    root.querySelector('[data-role="admin-sidebar-backdrop"]').click()
    expect(sidebar.classList.contains('is-open')).toBe(false)
    app.destroy()
  })

})
