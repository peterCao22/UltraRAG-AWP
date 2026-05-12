/**
 * 管理后台入口：知识库列表 / 详情（文档表 + 上传 + 索引进度）、hash 路由。
 */
import { buildAgentToolsPanel } from './components/agentToolsPanel.js'
import { openConfirmModal } from './components/confirmModal.js'
import { createStatusBadge } from './components/statusBadge.js'
import { Toast } from './components/toast.js'
import { sanitizeHtml } from './utils/sanitizeHtml.js'
import { isAllowedKbUploadFile } from './utils/uploadGuards.js'
import {
  createIngestJob,
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  getAgentConfig,
  getJobProgress,
  getKnowledgeBase,
  listDocuments,
  listJobs,
  listKnowledgeBases,
  updateAgentConfig,
  uploadKbDocuments,
} from './services/kbApi.js'

const defaultKbApi = {
  listKnowledgeBases,
  createKnowledgeBase,
  createIngestJob,
  deleteDocument,
  deleteKnowledgeBase,
  getAgentConfig,
  getJobProgress,
  getKnowledgeBase,
  listDocuments,
  listJobs,
  updateAgentConfig,
  uploadKbDocuments,
}

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024
const POLL_MS = 3000

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/"/g, '&quot;')
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

  function clearPoll() {
    if (pollTimer) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
    activeJobId = null
  }

  function showFlash(message, kind = 'error') {
    Toast.show(message, kind === 'success' ? 'success' : 'error')
  }

  async function renderList() {
    clearPoll()
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
    card.innerHTML = sanitizeHtml(`
      <h2 class="modal-title">新建知识库</h2>
      <form class="admin-form" data-role="new-kb-form">
        <label>知识库 ID（英文/数字/下划线）<span class="required">*</span>
          <input name="kb_id" class="field" required pattern="[a-zA-Z0-9_-]{2,64}" autocomplete="off" placeholder="例如 my_kb_01" />
        </label>
        <label>显示名称 <span class="required">*</span>
          <input name="name" class="field" required maxlength="120" placeholder="展示给用户的名称" />
        </label>
        <label>知识库类型 <span class="required">*</span>
          <select name="type" class="field" required>
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
    `)

    overlay.append(card)
    document.body.append(overlay)

    const form = card.querySelector('[data-role="new-kb-form"]')
    card.querySelector('[data-role="cancel"]').addEventListener('click', () => overlay.remove())
    // 新建表单易误触：仅允许「取消」或提交成功后关闭，点击遮罩不关闭。

    form.addEventListener('submit', async (e) => {
      e.preventDefault()
      const fd = new FormData(form)
      const kb_id = String(fd.get('kb_id') || '').trim()
      const name = String(fd.get('name') || '').trim()
      const type = String(fd.get('type') || 'sop_docx').trim() || 'sop_docx'
      const description = String(fd.get('description') || '').trim()
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
    if (activeKbId !== kbId) clearPoll()
    activeKbId = kbId
    setNavActive(root, 'detail')
    if (titleEl) titleEl.textContent = '知识库详情'

    outlet.replaceChildren(buildAdminDetailSkeleton())
    try {
      const [kb, docs, jobs] = await Promise.all([
        api.getKnowledgeBase(kbId),
        api.listDocuments(kbId, { limit: 200 }),
        api.listJobs(kbId, { limit: 20 }),
      ])

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
      zone.innerHTML = sanitizeHtml(
        '<p>拖拽 .docx / .pdf 到此处，或<button type="button" class="linklike" data-role="pick-files">选择文件</button></p><p class="muted">单文件最大 50MB</p>',
      )
      const input = document.createElement('input')
      input.type = 'file'
      input.multiple = true
      input.accept = '.docx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document'
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
          if (!isAllowedKbUploadFile(f)) {
            showFlash(
              `不支持的文件类型：${f.name}。仅允许 .pdf / .docx，且 MIME 须为 PDF 或 Word 文档（当前：${f.type || '未知'}）。`,
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

      zone.querySelector('[data-role="pick-files"]').addEventListener('click', () => input.click())
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

      const docTitle = document.createElement('h2')
      docTitle.textContent = '文档列表'
      wrap.append(docTitle)

      const tableWrap = document.createElement('div')
      tableWrap.className = 'admin-table-wrap'
      const table = document.createElement('table')
      table.className = 'admin-table'
      const thead = document.createElement('thead')
      const trHead = document.createElement('tr')
      for (const label of ['文件名', '类型', '状态', '上传时间', '']) {
        const th = document.createElement('th')
        th.textContent = label
        trHead.append(th)
      }
      thead.append(trHead)
      const tbody = document.createElement('tbody')
      table.append(thead, tbody)
      for (const d of docs) {
        const tr = document.createElement('tr')
        const tdName = document.createElement('td')
        tdName.textContent = d.file_name
        const tdType = document.createElement('td')
        tdType.textContent = d.file_type
        const tdSt = document.createElement('td')
        tdSt.append(createStatusBadge(d.status))
        const tdTime = document.createElement('td')
        tdTime.textContent = d.created_at || '—'
        const tdAct = document.createElement('td')
        const del = document.createElement('button')
        del.type = 'button'
        del.className = 'button-danger button-small'
        del.textContent = '删除'
        del.addEventListener('click', async () => {
          const ok = await openConfirmModal({ message: `删除文档「${d.file_name}」？` })
          if (!ok) return
          try {
            await api.deleteDocument(kbId, d.doc_id)
            await renderDetail(kbId)
          } catch (e) {
            showFlash(e.message || String(e))
          }
        })
        tdAct.append(del)
        tr.append(tdName, tdType, tdSt, tdTime, tdAct)
        tbody.append(tr)
      }
      if (!docs.length) {
        const tr = document.createElement('tr')
        const td = document.createElement('td')
        td.colSpan = 5
        const box = document.createElement('div')
        box.className = 'admin-empty-inline'
        const t = document.createElement('strong')
        t.textContent = '暂无文档'
        const hint = document.createElement('p')
        hint.className = 'muted'
        hint.textContent = '将 .docx 或 .pdf 拖到上方区域，或点击「选择文件」。单文件不超过 50MB。'
        box.append(t, hint)
        td.append(box)
        tr.append(td)
        tbody.append(tr)
      }
      tableWrap.append(table)
      wrap.append(tableWrap)

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

  return {
    ready,
    destroy() {
      clearPoll()
      window.removeEventListener('hashchange', handleRoute)
    },
  }
}

initAdminApp()
