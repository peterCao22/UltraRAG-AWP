/**
 * 管理后台入口：知识库列表 / 详情（文档表 + 上传 + 索引进度）、hash 路由。
 */
import { buildAgentToolsPanel } from './components/agentToolsPanel.js'
import { openConfirmModal } from './components/confirmModal.js'
import { createStatusBadge } from './components/statusBadge.js'
import { Toast } from './components/toast.js'
import { sanitizeHtml } from './utils/sanitizeHtml.js'
import { isAllowedKbUploadFile, getAcceptAttr, getUploadHint } from './utils/uploadGuards.js'
import {
  batchDocumentStatus,
  createIngestJob,
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  getAgentConfig,
  getJobProgress,
  getKnowledgeBase,
  listDocumentChunks,
  listDocuments,
  listJobs,
  listKnowledgeBases,
  retryDocument,
  updateAgentConfig,
  uploadKbDocuments,
} from './services/kbApi.js'

const defaultKbApi = {
  batchDocumentStatus,
  listKnowledgeBases,
  createKnowledgeBase,
  createIngestJob,
  deleteDocument,
  deleteKnowledgeBase,
  getAgentConfig,
  getJobProgress,
  getKnowledgeBase,
  listDocumentChunks,
  listDocuments,
  listJobs,
  retryDocument,
  updateAgentConfig,
  uploadKbDocuments,
}

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024
const POLL_MS = 3000
// Phase 6.1：文档状态轮询频率（WeKnora 用 1500ms，我们用 2000ms 折中）
const DOC_POLL_MS = 2000

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/"/g, '&quot;')
}

// ── Phase 6.1 helpers ──────────────────────────────────────────────────────

const PROCESSING_DOC_STATUSES = new Set(['pending', 'parsing', 'embedding', 'indexing', 'deleting'])

function isProcessingStatus(status) {
  return PROCESSING_DOC_STATUSES.has(String(status || '').toLowerCase())
}

function formatDocSummary(total, summary) {
  const s = summary || {}
  return (
    `共 ${total} 个文档 · ` +
    `${s.completed || 0} 已完成 · ` +
    `${s.parsing || 0} 解析中 · ` +
    `${s.embedding || 0} 嵌入中 · ` +
    `${s.indexing || 0} 索引中 · ` +
    `${s.pending || 0} 待处理 · ` +
    `${s.failed || 0} 失败 · ` +
    `${s.deleting || 0} 删除中`
  )
}

function formatDocTime(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function parseRoute() {
  const raw = (window.location.hash || '#/').replace(/^#/, '')
  if (raw === '/' || raw === '') return { view: 'list' }
  if (raw === '/status') return { view: 'status' }
  const m = raw.match(/^\/kb\/([^/]+)\/?$/)
  if (m) return { view: 'detail', kbId: decodeURIComponent(m[1]) }
  return { view: 'list' }
}

function buildAdminListSkeleton() {
  const wrap = document.createElement('div')
  wrap.className = 'admin-skeleton-list'
  wrap.setAttribute('aria-busy', 'true')
  wrap.setAttribute('aria-label', '加载知识库列表')
  for (let i = 0; i < 3; i += 1) {
    const card = document.createElement('div')
    card.className = 'skeleton-card'
    const a = document.createElement('div')
    a.className = 'skeleton-line skeleton-line--lg'
    const b = document.createElement('div')
    b.className = 'skeleton-line'
    const c = document.createElement('div')
    c.className = 'skeleton-line skeleton-line--short'
    card.append(a, b, c)
    wrap.append(card)
  }
  return wrap
}

function buildAdminDetailSkeleton() {
  const root = document.createElement('div')
  root.className = 'admin-detail-skeleton'
  root.setAttribute('aria-busy', 'true')
  root.setAttribute('aria-label', '加载知识库详情')
  const top = document.createElement('div')
  top.className = 'skeleton-line skeleton-line--xl skeleton-line--narrow'
  const sub = document.createElement('div')
  sub.className = 'skeleton-line skeleton-line--md'
  const actions = document.createElement('div')
  actions.className = 'admin-detail-skeleton-actions'
  const b1 = document.createElement('div')
  b1.className = 'skeleton-line skeleton-line--btn'
  const b2 = document.createElement('div')
  b2.className = 'skeleton-line skeleton-line--btn'
  actions.append(b1, b2)
  const table = document.createElement('div')
  table.className = 'admin-table-skeleton'
  for (let r = 0; r < 5; r += 1) {
    const row = document.createElement('div')
    row.className = 'skeleton-table-row'
    for (let c = 0; c < 4; c += 1) {
      const cell = document.createElement('div')
      cell.className = 'skeleton-line skeleton-line--sm'
      row.append(cell)
    }
    table.append(row)
  }
  root.append(top, sub, actions, table)
  return root
}

function setNavActive(root, view) {
  root.querySelectorAll('[data-nav]').forEach((a) => {
    const v = a.getAttribute('data-nav')
    a.classList.toggle('is-active', v === view || (view === 'detail' && v === 'kb'))
  })
}

function setAdminSidebarOpen(root, open) {
  const sidebar = root.querySelector('[data-role="admin-sidebar"]')
  const backdrop = root.querySelector('[data-role="admin-sidebar-backdrop"]')
  sidebar?.classList.toggle('is-open', Boolean(open))
  if (backdrop) {
    backdrop.classList.toggle('is-active', Boolean(open))
    backdrop.setAttribute('aria-hidden', open ? 'false' : 'true')
  }
}

/**
 * 初始化管理后台（路由、列表、详情、窄屏侧栏）。
 *
 * 参数：
 *   options.root — 挂载根节点，`[data-page="admin"]`
 *
 * 返回：
 *   `{ ready, destroy }` — `ready` 为首次路由渲染完成的 Promise
 */
export function initAdminApp({
  root = document.querySelector('[data-page="admin"]'),
  kbApi = defaultKbApi,
} = {}) {
  if (!root) return null

  const api = kbApi
  const outlet = root.querySelector('[data-role="admin-outlet"]')
  const titleEl = root.querySelector('[data-role="admin-title"]')
  if (!outlet) return null

  let pollTimer = null
  let activeKbId = null
  let activeJobId = null
  // Phase 6.1：文档级状态轮询独立计时器
  let docPollTimer = null
  let docPollKbId = null
  let docPollIds = []

  function clearPoll() {
    if (pollTimer) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
    activeJobId = null
  }

  function clearDocPoll() {
    if (docPollTimer) {
      window.clearInterval(docPollTimer)
      docPollTimer = null
    }
    docPollKbId = null
    docPollIds = []
  }

  function showFlash(message, kind = 'error') {
    Toast.show(message, kind === 'success' ? 'success' : 'error')
  }

  async function renderList() {
    clearPoll()
    clearDocPoll()
    activeKbId = null
    setNavActive(root, 'list')
    if (titleEl) titleEl.textContent = '知识库管理'

    outlet.replaceChildren(buildAdminListSkeleton())
    try {
      const items = await api.listKnowledgeBases({ purpose: 'admin' })
      const wrap = document.createElement('div')
      wrap.className = 'admin-list-view'

      const toolbar = document.createElement('div')
      toolbar.className = 'admin-toolbar'
      const btnNew = document.createElement('button')
      btnNew.type = 'button'
      btnNew.className = 'primary-action'
      btnNew.textContent = '+ 新建知识库'
      btnNew.addEventListener('click', () => openNewKbModal())
      toolbar.append(btnNew)
      wrap.append(toolbar)

      if (!items.length) {
        const empty = document.createElement('div')
        empty.className = 'admin-empty'
        const icon = document.createElement('div')
        icon.className = 'admin-empty-icon'
        icon.setAttribute('aria-hidden', 'true')
        icon.textContent = '📚'
        const h2 = document.createElement('h2')
        h2.className = 'admin-empty-title'
        h2.textContent = '还没有知识库'
        const p = document.createElement('p')
        p.className = 'muted'
        p.textContent = '创建后可上传文档并建立向量索引，供对话页选用。'
        const btnEmpty = document.createElement('button')
        btnEmpty.type = 'button'
        btnEmpty.className = 'primary-action'
        btnEmpty.textContent = '新建第一个知识库'
        btnEmpty.addEventListener('click', () => openNewKbModal())
        empty.append(icon, h2, p, btnEmpty)
        wrap.append(empty)
        outlet.replaceChildren(wrap)
        return
      }

      const grid = document.createElement('section')
      grid.className = 'card-grid'
      grid.setAttribute('aria-label', '知识库卡片列表')

      for (const kb of items) {
        const card = document.createElement('article')
        card.className = 'card admin-kb-card'

        const h2 = document.createElement('h2')
        h2.textContent = kb.name
        card.append(h2)

        const meta = document.createElement('p')
        meta.className = 'muted admin-kb-meta'
        meta.append(`标识：${kb.kb_id} · 文档 ${kb.document_count ?? 0} · `)
        meta.append(createStatusBadge(kb.status))
        card.append(meta)

        const row = document.createElement('div')
        row.className = 'admin-card-actions'

        const btnDetail = document.createElement('a')
        btnDetail.className = 'admin-primary-link'
        btnDetail.href = `#/kb/${encodeURIComponent(kb.kb_id)}`
        btnDetail.textContent = '详情'
        btnDetail.addEventListener('click', () => setAdminSidebarOpen(root, false))

        const btnDel = document.createElement('button')
        btnDel.type = 'button'
        btnDel.className = 'button-danger'
        btnDel.textContent = '删除'
        btnDel.addEventListener('click', async () => {
          const ok = await openConfirmModal({
            message: `确定删除知识库「${kb.name}」？（默认软删归档）`,
            confirmLabel: '删除',
          })
          if (!ok) return
          try {
            await api.deleteKnowledgeBase(kb.kb_id, { hard: false })
            await renderList()
          } catch (e) {
            showFlash(e.message || String(e))
          }
        })

        row.append(btnDetail, btnDel)
        card.append(row)
        grid.append(card)
      }

      wrap.append(grid)
      outlet.replaceChildren(wrap)
    } catch (e) {
      Toast.show(`列表加载失败：${e.message || String(e)}`, 'error')
      outlet.innerHTML = sanitizeHtml(`<p class="admin-error">加载失败：${escapeHtml(e.message || String(e))}</p>`)
    }
  }

  function openNewKbModal() {
    const overlay = document.createElement('div')
    overlay.className = 'modal-overlay'

    const card = document.createElement('div')
    card.className = 'modal-card modal-card--wide'
    // 模态框 HTML 来自应用自身代码（非用户输入），直接赋 innerHTML 避免
    // DOMPurify SANITIZE_DOM 干扰表单 name/pattern 等属性。
    card.innerHTML = `
        <h2 class="modal-title">新建知识库</h2>
        <form class="admin-form" data-role="new-kb-form">
          <label>知识库 ID（英文/数字/下划线）<span class="required">*</span>
          <input name="kb_id" class="field" required autocomplete="off" placeholder="例如 my_kb_01" />
          </label>
        <label>显示名称 <span class="required">*</span>
          <input name="display_name" class="field" required maxlength="120" placeholder="展示给用户的名称" />
        </label>
        <label>知识库类型 <span class="required">*</span>
          <select name="kb_type" class="field" required>
            <option value="sop_docx">SOP 知识库（DOCX，业务定制分块）</option>
            <option value="general">通用知识库（PDF / 图片 / DOCX / MD）</option>
          </select>
          <small class="muted">类型创建后无法修改。SOP 用于带 STEP 编号的标准作业流程；通用支持多种格式。</small>
        </label>
        <label>描述（可选）
          <textarea name="description" class="field" rows="2" maxlength="500" placeholder="用途说明"></textarea>
        </label>
        <div class="modal-actions">
          <button type="button" class="button-secondary" data-role="cancel">取消</button>
          <button type="submit" class="button-primary">创建</button>
        </div>
      </form>
    `

    overlay.append(card)
    document.body.append(overlay)

    const form = card.querySelector('[data-role="new-kb-form"]')
    card.querySelector('[data-role="cancel"]').addEventListener('click', () => overlay.remove())
    // 新建表单易误触：仅允许「取消」或提交成功后关闭，点击遮罩不关闭。

    form.addEventListener('submit', async (e) => {
      e.preventDefault()
      const fd = new FormData(form)
      const kb_id = String(fd.get('kb_id') || '').trim()
      const name = String(fd.get('display_name') || '').trim()
      const type = String(fd.get('kb_type') || 'sop_docx').trim() || 'sop_docx'
      const description = String(fd.get('description') || '').trim()
      if (!/^[A-Za-z0-9_-]{2,64}$/.test(kb_id)) {
        showFlash('知识库 ID 只能包含英文、数字、下划线或横线，长度 2-64。')
        return
      }
      try {
        await api.createKnowledgeBase({ kb_id, name, type, description })
        overlay.remove()
        window.location.hash = `#/kb/${encodeURIComponent(kb_id)}`
      } catch (err) {
        showFlash(err.message || String(err))
      }
    })
  }

  function startJobPoll(kbId, jobId) {
    clearPoll()
    activeKbId = kbId
    activeJobId = jobId
    pollTimer = window.setInterval(async () => {
      if (!activeJobId || activeKbId !== kbId) return
      try {
        const prog = await api.getJobProgress(kbId, activeJobId)
        const st = String(prog.status || '')
        const bar = outlet.querySelector('[data-role="index-progress"]')
        if (bar) {
          bar.textContent = `索引任务：${st} · 阶段 ${prog.stage || '-'}`
        }
        if (st === 'success' || st === 'failed' || st === 'cancelled') {
          clearPoll()
          if (st === 'failed' && prog.error) showFlash(prog.error)
          await renderDetail(kbId)
        }
      } catch {
        /* 轮询失败时忽略单次错误 */
      }
    }, POLL_MS)
  }

  async function renderDetail(kbId) {
    if (activeKbId !== kbId) {
      clearPoll()
      clearDocPoll()
    } else {
      // 同一 KB 重渲染时也需要重置 doc 轮询，新一轮会按文档状态再启动
      clearDocPoll()
    }
    activeKbId = kbId
    setNavActive(root, 'detail')
    if (titleEl) titleEl.textContent = '知识库详情'

    outlet.replaceChildren(buildAdminDetailSkeleton())
    try {
      const [kb, docBundle, jobs] = await Promise.all([
        api.getKnowledgeBase(kbId),
        api.listDocuments(kbId),
        api.listJobs(kbId, { limit: 20 }),
      ])
      const docs = docBundle.documents
      const docSummary = docBundle.summary

      let chunkHint = '—'
      const lastOk = jobs.find((j) => j.job_type === 'ingest' && j.status === 'success')
      if (lastOk?.result && typeof lastOk.result.chunk_count === 'number') {
        chunkHint = String(lastOk.result.chunk_count)
      }

      const wrap = document.createElement('div')
      wrap.className = 'admin-detail-view'

      const head = document.createElement('header')
      head.className = 'admin-detail-head'

      const back = document.createElement('a')
      back.href = '#/'
      back.className = 'admin-back-link'
      back.textContent = '← 返回列表'
      back.addEventListener('click', () => setAdminSidebarOpen(root, false))

      const h1 = document.createElement('h1')
      h1.textContent = kb.name

      const sub = document.createElement('p')
      sub.className = 'muted admin-detail-sub'
      sub.append('标识 ', document.createTextNode(kb.kb_id), ' · ')
      sub.append(createStatusBadge(kb.status))
      const kbType = kb.type === 'general' ? '通用' : 'SOP'
      sub.append(document.createTextNode(
        ` · 类型 ${kbType}（创建后不可改） · 文档 ${kb.document_count ?? docs.length} · 块 ${chunkHint}`
      ))

      head.append(back, h1, sub)

      const actions = document.createElement('div')
      actions.className = 'admin-detail-actions'

      const btnReindex = document.createElement('button')
      btnReindex.type = 'button'
      btnReindex.className = 'button-secondary'
      btnReindex.textContent = '重建索引'
      btnReindex.addEventListener('click', async () => {
        const ok = await openConfirmModal({
          message: '将触发异步入库任务（解析 DOCX → 向量 → FAISS）。确定继续？',
          confirmLabel: '开始重建',
        })
        if (!ok) return
        try {
          await api.createIngestJob(kbId, { force_reindex: true, async: true })
          showFlash('已提交索引任务', 'success')
          await renderDetail(kbId)
        } catch (e) {
          showFlash(e.message || String(e))
        }
      })

      const btnDelKb = document.createElement('button')
      btnDelKb.type = 'button'
      btnDelKb.className = 'button-danger'
      btnDelKb.textContent = '删除知识库'
      btnDelKb.addEventListener('click', async () => {
        const ok = await openConfirmModal({
          message: `确定删除「${kb.name}」？默认归档，数据目录保留。`,
          confirmLabel: '删除',
        })
        if (!ok) return
        try {
          await api.deleteKnowledgeBase(kbId, { hard: false })
          window.location.hash = '#/'
        } catch (e) {
          showFlash(e.message || String(e))
        }
      })

      actions.append(btnReindex, btnDelKb)

      const progress = document.createElement('p')
      progress.className = 'muted'
      progress.dataset.role = 'index-progress'
      progress.textContent = '索引任务：—'

      const running = jobs.find((j) => j.job_type === 'ingest' && (j.status === 'pending' || j.status === 'running'))
      if (running) {
        progress.textContent = `索引任务：${running.status} · ${running.summary || ''}`
        startJobPoll(kbId, running.job_id)
      }

      wrap.append(head, actions, progress)

      const uploadSection = document.createElement('section')
      uploadSection.className = 'admin-upload-section'
      const uploadTitle = document.createElement('h2')
      uploadTitle.textContent = '上传文档'
      uploadSection.append(uploadTitle)

      const zone = document.createElement('div')
      zone.className = 'admin-dropzone'
      const uploadHint = getUploadHint(kb.type)
      const hintP = document.createElement('p')
      const pickBtn = document.createElement('button')
      pickBtn.type = 'button'
      pickBtn.className = 'linklike'
      pickBtn.dataset.role = 'pick-files'
      pickBtn.textContent = '选择文件'
      hintP.append(`拖拽 ${uploadHint} 到此处，或`, pickBtn)
      const sizeP = document.createElement('p')
      sizeP.className = 'muted'
      sizeP.textContent = '单文件最大 50MB'
      zone.append(hintP, sizeP)
      const input = document.createElement('input')
      input.type = 'file'
      input.multiple = true
      input.accept = getAcceptAttr(kb.type)
      input.className = 'visually-hidden'
      input.dataset.role = 'file-input'

      const bar = document.createElement('div')
      bar.className = 'admin-progress-bar'
      bar.hidden = true
      const barInner = document.createElement('div')
      barInner.className = 'admin-progress-bar__inner'
      bar.append(barInner)

      async function handleFiles(fileList) {
        const files = Array.from(fileList || []).filter(Boolean)
        if (!files.length) return
        for (const f of files) {
          if (!isAllowedKbUploadFile(f, kb.type)) {
            showFlash(
              `不支持的文件类型：${f.name}。允许 ${uploadHint}（当前 MIME：${f.type || '未知'}）。`,
            )
            return
          }
          if (f.size > MAX_UPLOAD_BYTES) {
            showFlash(`文件超过 50MB：${f.name}`)
            return
          }
        }
        bar.hidden = false
        barInner.style.width = '0%'
        try {
          await api.uploadKbDocuments(kbId, files, {
            onProgress: (p) => {
              barInner.style.width = `${Math.round(p * 100)}%`
            },
          })
          showFlash('上传完成', 'success')
          await renderDetail(kbId)
        } catch (e) {
          showFlash(e.message || String(e))
        } finally {
          bar.hidden = true
        }
      }

      pickBtn.addEventListener('click', () => input.click())
      input.addEventListener('change', () => {
        handleFiles(input.files)
        input.value = ''
      })
      zone.addEventListener('dragover', (e) => {
        e.preventDefault()
        zone.classList.add('is-dragover')
      })
      zone.addEventListener('dragleave', () => zone.classList.remove('is-dragover'))
      zone.addEventListener('drop', (e) => {
        e.preventDefault()
        zone.classList.remove('is-dragover')
        handleFiles(e.dataTransfer?.files)
      })

      uploadSection.append(zone, input, bar)
      wrap.append(uploadSection)

      // Phase 6.1: 文档列表（卡片 + 状态徽章 + 汇总条 + 轮询）
      const docSection = document.createElement('section')
      docSection.className = 'admin-docs-section'
      const docTitle = document.createElement('h2')
      docTitle.textContent = '文档列表'
      docSection.append(docTitle)

      const summaryBar = document.createElement('p')
      summaryBar.className = 'admin-docs-summary muted'
      summaryBar.dataset.role = 'docs-summary'
      summaryBar.textContent = formatDocSummary(docs.length, docSummary)
      docSection.append(summaryBar)

      const docList = document.createElement('div')
      docList.className = 'admin-docs-list'
      docList.dataset.role = 'docs-list'
      docSection.append(docList)

      if (!docs.length) {
        const emptyBox = document.createElement('div')
        emptyBox.className = 'admin-empty-inline'
        const t = document.createElement('strong')
        t.textContent = '暂无文档'
        const hint = document.createElement('p')
        hint.className = 'muted'
        hint.textContent = `将 ${uploadHint} 拖到上方区域，或点击「选择文件」。单文件不超过 50MB。`
        emptyBox.append(t, hint)
        docList.append(emptyBox)
      } else {
        for (const d of docs) {
          docList.append(buildDocRow(kbId, d))
        }
      }

      wrap.append(docSection)

      // 启动文档级状态轮询（如果有在途文档）
      const pendingDocIds = docs
        .filter((d) => isProcessingStatus(d.status))
        .map((d) => d.doc_id)
      if (pendingDocIds.length > 0) {
        startDocStatusPoll(kbId, pendingDocIds)
      }

      // ── Agent 工具配置面板（侧重智能推理 KB；quick KB 也可见仅作占位）──
      try {
        const cfg = await api.getAgentConfig(kbId)
        const { root: panel } = buildAgentToolsPanel({
          allTools: cfg?.all_tools || [],
          enabledTools: cfg?.enabled_tools || [],
          onSave: async (tools) => {
            try {
              await api.updateAgentConfig(kbId, tools)
              Toast.show('已保存工具配置', 'success')
            } catch (e) {
              Toast.show(`保存失败：${e.message || String(e)}`, 'error')
              throw e
            }
          },
        })
        wrap.append(panel)
      } catch (e) {
        // 工具配置加载失败不影响详情页主功能
        const warn = document.createElement('p')
        warn.className = 'muted admin-agent-tools__error'
        warn.textContent = `智能推理工具配置加载失败：${e.message || String(e)}`
        wrap.append(warn)
      }

      outlet.replaceChildren(wrap)
    } catch (e) {
      Toast.show(`详情加载失败：${e.message || String(e)}`, 'error')
      outlet.innerHTML = sanitizeHtml(
        `<p class="admin-error">加载失败：${escapeHtml(e.message || String(e))}</p><p><a href="#/">返回列表</a></p>`,
      )
    }
  }

  function renderStatus() {
    clearPoll()
    clearDocPoll()
    activeKbId = null
    setNavActive(root, 'status')
    if (titleEl) titleEl.textContent = '系统状态'
    outlet.innerHTML = sanitizeHtml(`
      <section>
        <h1>系统状态</h1>
        <p class="muted">Sprint 3 占位：后续可接入健康检查、磁盘与任务队列概览。</p>
        <p><a href="#/">返回知识库管理</a></p>
      </section>`)
  }

  async function handleRoute() {
    const route = parseRoute()
    if (route.view === 'list') await renderList()
    else if (route.view === 'status') renderStatus()
    else if (route.view === 'detail' && route.kbId) await renderDetail(route.kbId)
    else await renderList()
  }

  const toggle = root.querySelector('[data-role="admin-sidebar-toggle"]')
  const backdrop = root.querySelector('[data-role="admin-sidebar-backdrop"]')
  toggle?.addEventListener('click', () => {
    const sidebar = root.querySelector('[data-role="admin-sidebar"]')
    const next = !sidebar?.classList.contains('is-open')
    setAdminSidebarOpen(root, next)
  })
  backdrop?.addEventListener('click', () => setAdminSidebarOpen(root, false))
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return
    const sidebar = root.querySelector('[data-role="admin-sidebar"]')
    if (sidebar?.classList.contains('is-open')) setAdminSidebarOpen(root, false)
  })

  outlet.addEventListener('click', (e) => {
    const a = e.target.closest('a[href^="#/"]')
    if (a) setAdminSidebarOpen(root, false)
  })

  window.addEventListener('hashchange', () => {
    void handleRoute()
  })

  const ready = handleRoute().then(() => {
    root.dataset.ready = 'true'
  })

  // ── Phase 6.1：文档卡片 + 详情面板 + 轮询 ──────────────────────────────

  function buildDocRow(kbId, d) {
    const row = document.createElement('div')
    row.className = 'admin-doc-row'
    row.dataset.docId = d.doc_id
    row.dataset.status = d.status

    const main = document.createElement('div')
    main.className = 'admin-doc-row__main'
    const name = document.createElement('div')
    name.className = 'admin-doc-row__name'
    name.textContent = d.file_name
    main.append(name)
    const meta = document.createElement('div')
    meta.className = 'admin-doc-row__meta muted'
    const metaBits = [d.file_type || '?', `上传 ${formatDocTime(d.created_at) || '—'}`]
    if (d.status === 'completed' && d.chunk_count) {
      metaBits.push(`${d.chunk_count} 分块`)
    }
    if (d.status === 'completed' && d.processed_at) {
      metaBits.push(`完成 ${formatDocTime(d.processed_at)}`)
    }
    meta.textContent = metaBits.filter(Boolean).join(' · ')
    main.append(meta)
    row.append(main)

    const right = document.createElement('div')
    right.className = 'admin-doc-row__right'
    const badge = createStatusBadge(d.status, {
      title: d.status === 'failed' ? d.error_message || '失败' : '',
    })
    badge.dataset.role = 'doc-status-badge'
    right.append(badge)

    // 操作区
    const ops = document.createElement('div')
    ops.className = 'admin-doc-row__ops'

    if (d.status === 'failed') {
      const errSpan = document.createElement('span')
      errSpan.className = 'admin-doc-row__err'
      errSpan.title = d.error_message || ''
      errSpan.textContent = '错误详情'
      errSpan.addEventListener('click', (e) => {
        e.stopPropagation()
        openFailedDetailModal(kbId, d)
      })
      ops.append(errSpan)
      const retryBtn = document.createElement('button')
      retryBtn.type = 'button'
      retryBtn.className = 'button-secondary button-small'
      retryBtn.textContent = '重试'
      retryBtn.addEventListener('click', async (e) => {
        e.stopPropagation()
        try {
          await api.retryDocument(kbId, d.doc_id)
          showFlash('已重新提交', 'success')
          await renderDetail(kbId)
        } catch (err) {
          showFlash(err.message || String(err))
        }
      })
      ops.append(retryBtn)
    } else if (d.status === 'completed') {
      const viewBtn = document.createElement('button')
      viewBtn.type = 'button'
      viewBtn.className = 'button-secondary button-small'
      viewBtn.textContent = '查看分块'
      viewBtn.addEventListener('click', (e) => {
        e.stopPropagation()
        openDocDetailModal(kbId, d)
      })
      ops.append(viewBtn)
    }

    const del = document.createElement('button')
    del.type = 'button'
    del.className = 'button-danger button-small'
    del.textContent = '删除'
    del.addEventListener('click', async (e) => {
      e.stopPropagation()
      const ok = await openConfirmModal({ message: `删除文档「${d.file_name}」？` })
      if (!ok) return
      try {
        await api.deleteDocument(kbId, d.doc_id)
        await renderDetail(kbId)
      } catch (err) {
        showFlash(err.message || String(err))
      }
    })
    ops.append(del)
    right.append(ops)
    row.append(right)

    // completed 行点空白处也能打开详情
    if (d.status === 'completed') {
      row.classList.add('admin-doc-row--clickable')
      row.addEventListener('click', () => openDocDetailModal(kbId, d))
    }
    return row
  }

  function startDocStatusPoll(kbId, initialDocIds) {
    clearDocPoll()
    docPollKbId = kbId
    docPollIds = Array.from(new Set(initialDocIds || []))
    if (!docPollIds.length) return
    docPollTimer = window.setInterval(async () => {
      if (docPollKbId !== kbId || activeKbId !== kbId) {
        clearDocPoll()
        return
      }
      if (!docPollIds.length) {
        clearDocPoll()
        return
      }
      try {
        const updates = await api.batchDocumentStatus(kbId, docPollIds)
        applyDocStatusUpdates(updates)
        // 把已经离开"在途"的 doc 从轮询列表中剔除；若全部完成则停止
        const remaining = updates
          .filter((u) => isProcessingStatus(u.status))
          .map((u) => u.doc_id)
        // 如果某个 id 没出现在返回里（被删除/不存在），也从轮询中剔除
        const returned = new Set(updates.map((u) => u.doc_id))
        docPollIds = docPollIds.filter((id) => returned.has(id) && remaining.includes(id))
        if (docPollIds.length === 0) {
          clearDocPoll()
          // 全部离开在途态：刷新一次详情拿到完整 chunk_count + summary
          await renderDetail(kbId)
        }
      } catch {
        /* 单次轮询失败不中断 */
      }
    }, DOC_POLL_MS)
  }

  function applyDocStatusUpdates(updates) {
    const list = outlet.querySelector('[data-role="docs-list"]')
    if (!list) return
    const summaryEl = outlet.querySelector('[data-role="docs-summary"]')
    const summaryCounts = {
      pending: 0, parsing: 0, embedding: 0, indexing: 0,
      completed: 0, failed: 0, deleting: 0,
    }
    let total = 0
    for (const update of updates) {
      const row = list.querySelector(`[data-doc-id="${cssEscape(update.doc_id)}"]`)
      if (!row) continue
      row.dataset.status = update.status
      const badge = row.querySelector('[data-role="doc-status-badge"]')
      if (badge) {
        const fresh = createStatusBadge(update.status, {
          title: update.status === 'failed' ? update.error_message || '失败' : '',
        })
        fresh.dataset.role = 'doc-status-badge'
        badge.replaceWith(fresh)
      }
    }
    // 重新统计 summary（看 list 内所有 row 的 data-status）
    for (const r of list.querySelectorAll('.admin-doc-row')) {
      total += 1
      const st = r.dataset.status
      if (st in summaryCounts) summaryCounts[st] += 1
    }
    if (summaryEl && total > 0) {
      summaryEl.textContent = formatDocSummary(total, summaryCounts)
    }
  }

  function cssEscape(s) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(s)
    return String(s).replace(/["\\]/g, '\\$&')
  }

  function openFailedDetailModal(kbId, doc) {
    const overlay = document.createElement('div')
    overlay.className = 'modal-overlay'
    const card = document.createElement('div')
    card.className = 'modal-card'
    const h = document.createElement('h2')
    h.className = 'modal-title'
    h.textContent = `失败详情 · ${doc.file_name}`
    const pre = document.createElement('pre')
    pre.className = 'admin-error-pre'
    pre.textContent = doc.error_message || '(无错误信息)'
    const actions = document.createElement('div')
    actions.className = 'modal-actions'
    const close = document.createElement('button')
    close.type = 'button'
    close.className = 'button-secondary'
    close.textContent = '关闭'
    close.addEventListener('click', () => overlay.remove())
    const retry = document.createElement('button')
    retry.type = 'button'
    retry.className = 'button-primary'
    retry.textContent = '重试'
    retry.addEventListener('click', async () => {
      try {
        await api.retryDocument(kbId, doc.doc_id)
        overlay.remove()
        showFlash('已重新提交', 'success')
        await renderDetail(kbId)
      } catch (e) {
        showFlash(e.message || String(e))
      }
    })
    actions.append(close, retry)
    card.append(h, pre, actions)
    overlay.append(card)
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.remove()
    })
    document.body.append(overlay)
  }

  async function openDocDetailModal(kbId, doc) {
    const overlay = document.createElement('div')
    overlay.className = 'modal-overlay'
    const card = document.createElement('div')
    card.className = 'modal-card modal-card--wide admin-doc-detail-card'

    const head = document.createElement('div')
    head.className = 'admin-doc-detail-head'
    const h = document.createElement('h2')
    h.className = 'modal-title'
    h.textContent = doc.file_name
    head.append(h)
    const sub = document.createElement('p')
    sub.className = 'muted'
    sub.textContent =
      `${doc.chunk_count || 0} 分块 · 完成 ${formatDocTime(doc.processed_at) || '—'}`
    head.append(sub)

    const tabs = document.createElement('div')
    tabs.className = 'admin-doc-tabs'
    const tabChunks = document.createElement('button')
    tabChunks.type = 'button'
    tabChunks.className = 'admin-doc-tab is-active'
    tabChunks.textContent = '分块（chunks）'
    const tabMerged = document.createElement('button')
    tabMerged.type = 'button'
    tabMerged.className = 'admin-doc-tab'
    tabMerged.textContent = '合并（merged）'
    const tabPreview = document.createElement('button')
    tabPreview.type = 'button'
    tabPreview.className = 'admin-doc-tab is-disabled'
    tabPreview.textContent = '预览（Coming soon）'
    tabPreview.disabled = true
    tabPreview.title = 'Phase 6.2+ 推出'
    tabs.append(tabChunks, tabMerged, tabPreview)
    head.append(tabs)

    const body = document.createElement('div')
    body.className = 'admin-doc-detail-body'
    body.textContent = '加载中…'

    const actions = document.createElement('div')
    actions.className = 'modal-actions'
    const close = document.createElement('button')
    close.type = 'button'
    close.className = 'button-secondary'
    close.textContent = '关闭'
    close.addEventListener('click', () => overlay.remove())
    actions.append(close)

    card.append(head, body, actions)
    overlay.append(card)
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.remove()
    })
    document.body.append(overlay)

    let chunks = []
    try {
      const res = await api.listDocumentChunks(kbId, doc.doc_id)
      chunks = res.chunks || []
    } catch (e) {
      body.textContent = `加载失败：${e.message || String(e)}`
      return
    }

    const renderChunks = () => {
      tabChunks.classList.add('is-active')
      tabMerged.classList.remove('is-active')
      body.innerHTML = ''
      if (!chunks.length) {
        const empty = document.createElement('p')
        empty.className = 'muted'
        empty.textContent = '该文档没有分块内容。'
        body.append(empty)
        return
      }
      chunks.forEach((c, idx) => {
        const block = document.createElement('section')
        block.className = 'admin-chunk-block'
        const meta = document.createElement('p')
        meta.className = 'admin-chunk-meta muted'
        meta.textContent = `分块 ${idx + 1} · ${c.char_count} 字符${c.image_count ? ` · ${c.image_count} 张图` : ''}`
        block.append(meta)
        if (c.title) {
          const t = document.createElement('h4')
          t.className = 'admin-chunk-title'
          t.textContent = c.title
          block.append(t)
        }
        const content = document.createElement('div')
        content.className = 'admin-chunk-content'
        content.innerHTML = renderMarkdownSafe(c.contents || '')
        block.append(content)
        body.append(block)
      })
    }
    const renderMerged = () => {
      tabMerged.classList.add('is-active')
      tabChunks.classList.remove('is-active')
      body.innerHTML = ''
      if (!chunks.length) {
        const empty = document.createElement('p')
        empty.className = 'muted'
        empty.textContent = '该文档没有分块内容。'
        body.append(empty)
        return
      }
      const merged = chunks
        .map((c) => (c.title ? `## ${c.title}\n\n` : '') + (c.contents || ''))
        .join('\n\n---\n\n')
      const wrapper = document.createElement('div')
      wrapper.className = 'admin-chunk-content'
      wrapper.innerHTML = renderMarkdownSafe(merged)
      body.append(wrapper)
    }
    tabChunks.addEventListener('click', renderChunks)
    tabMerged.addEventListener('click', renderMerged)
    renderChunks()
  }

  function renderMarkdownSafe(src) {
    try {
      if (typeof window.marked?.parse !== 'function') {
        return sanitizeHtml(`<pre>${escapeHtml(src)}</pre>`)
      }
      const html = window.marked.parse(src, { mangle: false, headerIds: false })
      return sanitizeHtml(html)
    } catch {
      return sanitizeHtml(`<pre>${escapeHtml(src)}</pre>`)
    }
  }

  return {
    ready,
    destroy() {
      clearPoll()
      clearDocPoll()
      window.removeEventListener('hashchange', handleRoute)
    },
  }
}

initAdminApp()
