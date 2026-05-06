import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createIngestJob,
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  getJobProgress,
  getKnowledgeBase,
  getStoredAdminToken,
  listDocuments,
  listJobs,
  listKnowledgeBases,
  normalizeKnowledgeBase,
  uploadKbDocuments,
} from '../services/kbApi.js'

describe('normalizeKnowledgeBase', () => {
  it('keeps UI fields and removes internal server paths', () => {
    const kb = normalizeKnowledgeBase({
      kb_id: 'agv_demo',
      name: 'AGV Demo',
      status: 'active',
      data_path: 'data/kb/agv_demo',
      index_path: 'data/kb/agv_demo/index/index.index',
      embedding_path: 'data/kb/agv_demo/embedding/embedding.npy',
      created_at: '2026-04-01T00:00:00Z',
    })

    expect(kb).toEqual({
      kb_id: 'agv_demo',
      id: 'agv_demo',
      name: 'AGV Demo',
      status: 'active',
      description: '',
      created_at: '2026-04-01T00:00:00Z',
      updated_at: '',
      last_indexed_at: '',
      document_count: 0,
    })
    expect(kb).not.toHaveProperty('data_path')
    expect(kb).not.toHaveProperty('index_path')
    expect(kb).not.toHaveProperty('embedding_path')
  })
})

describe('listKnowledgeBases', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('fetches /api/kb and keeps active, ready, and indexed knowledge bases', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: [
        { kb_id: 'active_kb', name: 'Active KB', status: 'active' },
        { kb_id: 'ready_kb', name: 'Ready KB', status: 'ready' },
        { kb_id: 'indexed_kb', name: 'Indexed KB', status: 'indexed' },
        { kb_id: 'pending_kb', name: 'Pending KB', status: 'pending' },
      ],
    })))
    vi.stubGlobal('fetch', fetchMock)

    const result = await listKnowledgeBases()

    expect(fetchMock).toHaveBeenCalledWith('/api/kb', expect.objectContaining({
      headers: { Accept: 'application/json' },
    }))
    expect(result.map((kb) => kb.kb_id)).toEqual(['active_kb', 'ready_kb', 'indexed_kb'])
  })

  it('throws backend error messages', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ error: 'unauthorized' }),
      { status: 401 },
    )))

    await expect(listKnowledgeBases()).rejects.toThrow('unauthorized')
  })

  it('falls back to HTTP status when error response is not JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not json', { status: 500 })))

    await expect(listKnowledgeBases()).rejects.toThrow('HTTP 500')
  })

  it('lists all non-chat-filtered rows when purpose is admin', async () => {
    const payload = JSON.stringify({
      data: [
        { kb_id: 'a', name: 'A', status: 'active' },
        { kb_id: 'p', name: 'P', status: 'pending' },
      ],
    })
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(payload)))
    vi.stubGlobal('fetch', fetchMock)

    const chat = await listKnowledgeBases({ purpose: 'chat' })
    const admin = await listKnowledgeBases({ purpose: 'admin' })

    expect(chat.map((k) => k.kb_id)).toEqual(['a'])
    expect(admin.map((k) => k.kb_id)).toEqual(['a', 'p'])
  })
})

describe('kbApi admin mutations', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('createKnowledgeBase posts JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: { kb_id: 'new_kb', status: 'active' },
    })))
    vi.stubGlobal('fetch', fetchMock)

    await createKnowledgeBase(
      { kb_id: 'new_kb', name: 'New', description: '' },
      { adminToken: 'tok' },
    )

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/kb',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          'Content-Type': 'application/json',
          'X-Admin-Token': 'tok',
        }),
      }),
    )
  })

  it('deleteKnowledgeBase sends DELETE', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: { deleted: true },
    })))
    vi.stubGlobal('fetch', fetchMock)

    await deleteKnowledgeBase('kb1', { adminToken: 't' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/kb/kb1',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('deleteDocument passes doc_id query', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: { deleted: true },
    })))
    vi.stubGlobal('fetch', fetchMock)

    await deleteDocument('kb1', 'kb1:file.docx', {})

    const [url] = fetchMock.mock.calls[0]
    expect(url).toContain('doc_id=')
    expect(decodeURIComponent(url)).toContain('kb1:file.docx')
  })

  it('getKnowledgeBase fetches detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: { kb_id: 'x', name: 'X', status: 'active', document_count: 3 },
    }))))

    const kb = await getKnowledgeBase('x')
    expect(kb.document_count).toBe(3)
  })

  it('listDocuments maps rows', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: [{ doc_id: 'kb:f', file_name: 'f.docx', kb_id: 'kb', file_type: 'docx', file_path: '/p', channel: 'web', status: 'uploaded', error_message: '', created_at: '', updated_at: '' }],
    }))))

    const docs = await listDocuments('kb')
    expect(docs[0].file_name).toBe('f.docx')
  })

  it('createIngestJob posts async body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: { job_id: 'job_1', status: 'pending' },
    })))
    vi.stubGlobal('fetch', fetchMock)

    await createIngestJob('kb1', { force_reindex: true, async: true })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/kb/kb1/ingest',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ force_reindex: true, async: true }),
      }),
    )
  })

  it('listJobs and getJobProgress hit correct paths', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: { status: 'running', stage: 'parse' },
      })))
    vi.stubGlobal('fetch', fetchMock)

    await listJobs('kb1')
    await getJobProgress('kb1', 'job_1')

    expect(fetchMock.mock.calls[0][0]).toContain('/api/kb/kb1/jobs')
    expect(fetchMock.mock.calls[1][0]).toContain('/api/kb/kb1/jobs/job_1/progress')
  })

  it('listJobs returns empty array when data is not an array', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: null }))))
    await expect(listJobs('kb1')).resolves.toEqual([])
  })

  it('uploadKbDocuments uses XHR and FormData', async () => {
    const xhrClass = vi.fn().mockImplementation(function MockXHR() {
      this.upload = { onprogress: null }
      this.status = 200
      this.responseText = JSON.stringify({ data: { uploaded: 1, files: ['a.docx'] } })
      this.open = vi.fn()
      this.setRequestHeader = vi.fn()
      this.send = vi.fn(() => {
        if (this.onload) this.onload()
      })
    })
    vi.stubGlobal('XMLHttpRequest', xhrClass)

    const file = new File(['x'], 'a.docx', { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
    const data = await uploadKbDocuments('kb1', [file], { adminToken: 'tok' })

    expect(data.uploaded).toBe(1)
    const xhr = xhrClass.mock.results[0].value
    expect(xhr.open).toHaveBeenCalledWith(
      'POST',
      '/api/kb/kb1/documents/upload',
    )
    expect(xhr.setRequestHeader).toHaveBeenCalledWith('X-Admin-Token', 'tok')
  })

  it('uploadKbDocuments skips token header when no token', async () => {
    window.sessionStorage.removeItem('ultrarag_admin_token')
    window.localStorage.removeItem('ultrarag_admin_token')
    const xhrClass = vi.fn().mockImplementation(function MockXHR() {
      this.upload = { onprogress: null }
      this.status = 200
      this.responseText = JSON.stringify({ data: { uploaded: 0, files: [] } })
      this.open = vi.fn()
      this.setRequestHeader = vi.fn()
      this.send = vi.fn(() => {
        if (this.onload) this.onload()
      })
    })
    vi.stubGlobal('XMLHttpRequest', xhrClass)
    const file = new File(['x'], 'a.docx')
    await uploadKbDocuments('kb1', [file], {})
    const xhr = xhrClass.mock.results[0].value
    const tokenHeaders = xhr.setRequestHeader.mock.calls.filter((c) => c[0] === 'X-Admin-Token')
    expect(tokenHeaders).toHaveLength(0)
  })

  it('uploadKbDocuments invokes onProgress when length is computable', async () => {
    let ratio = 0
    const xhrClass = vi.fn().mockImplementation(function MockXHR() {
      this.upload = { onprogress: null }
      this.status = 200
      this.responseText = JSON.stringify({ data: { uploaded: 1, files: ['a.docx'] } })
      this.open = vi.fn()
      this.setRequestHeader = vi.fn()
      this.send = vi.fn(() => {
        if (this.upload.onprogress) {
          this.upload.onprogress({ lengthComputable: true, loaded: 2, total: 8 })
        }
        if (this.onload) this.onload()
      })
    })
    vi.stubGlobal('XMLHttpRequest', xhrClass)
    const file = new File(['x'], 'a.docx')
    await uploadKbDocuments('kb1', [file], { onProgress: (p) => { ratio = p } })
    expect(ratio).toBeCloseTo(0.25)
  })

  it('uploadKbDocuments uses HTTP status when response is not JSON', async () => {
    const xhrClass = vi.fn().mockImplementation(function MockXHR() {
      this.upload = { onprogress: null }
      this.status = 502
      this.responseText = '<html>err</html>'
      this.open = vi.fn()
      this.setRequestHeader = vi.fn()
      this.send = vi.fn(() => {
        if (this.onload) this.onload()
      })
    })
    vi.stubGlobal('XMLHttpRequest', xhrClass)
    const file = new File(['x'], 'a.docx')
    await expect(uploadKbDocuments('kb1', [file])).rejects.toThrow('HTTP 502')
  })

  it('uploadKbDocuments rejects oversized file', async () => {
    const big = new File([new Uint8Array(50 * 1024 * 1024 + 1)], 'huge.docx')
    await expect(uploadKbDocuments('kb1', [big])).rejects.toThrow('50MB')
  })

  it('uploadKbDocuments rejects HTTP error body from XHR', async () => {
    const xhrClass = vi.fn().mockImplementation(function MockXHR() {
      this.upload = { onprogress: null }
      this.status = 500
      this.responseText = JSON.stringify({ error: 'srv' })
      this.open = vi.fn()
      this.setRequestHeader = vi.fn()
      this.send = vi.fn(() => {
        if (this.onload) this.onload()
      })
    })
    vi.stubGlobal('XMLHttpRequest', xhrClass)
    const file = new File(['x'], 'a.docx')
    await expect(uploadKbDocuments('kb1', [file])).rejects.toThrow('srv')
  })

  it('uploadKbDocuments rejects on XHR network error', async () => {
    const xhrClass = vi.fn().mockImplementation(function MockXHR() {
      this.upload = { onprogress: null }
      this.open = vi.fn()
      this.setRequestHeader = vi.fn()
      this.send = vi.fn(() => {
        if (this.onerror) this.onerror()
      })
    })
    vi.stubGlobal('XMLHttpRequest', xhrClass)
    const file = new File(['x'], 'a.docx')
    await expect(uploadKbDocuments('kb1', [file])).rejects.toThrow('网络错误')
  })

  it('deleteKnowledgeBase with hard=true adds query', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {} })))
    vi.stubGlobal('fetch', fetchMock)
    await deleteKnowledgeBase('k', { hard: true })
    expect(fetchMock.mock.calls[0][0]).toContain('hard=true')
  })

  it('getKnowledgeBase with includeArchived adds query', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      data: { kb_id: 'x', name: 'X', status: 'archived' },
    })))
    vi.stubGlobal('fetch', fetchMock)
    await getKnowledgeBase('x', { includeArchived: true })
    expect(fetchMock.mock.calls[0][0]).toContain('include_archived=true')
  })
})

describe('getStoredAdminToken', () => {
  it('returns empty string when sessionStorage throws', () => {
    const orig = window.sessionStorage.getItem
    window.sessionStorage.getItem = () => {
      throw new Error('blocked')
    }
    expect(getStoredAdminToken()).toBe('')
    window.sessionStorage.getItem = orig
  })

  it('prefers sessionStorage then falls back to localStorage', () => {
    window.sessionStorage.setItem('ultrarag_admin_token', 'sess')
    window.localStorage.setItem('ultrarag_admin_token', 'loc')
    expect(getStoredAdminToken()).toBe('sess')
    window.sessionStorage.removeItem('ultrarag_admin_token')
    expect(getStoredAdminToken()).toBe('loc')
    window.localStorage.removeItem('ultrarag_admin_token')
  })
})
