import { applyI18n, bindLangSwitch, t } from './i18n.js'

console.log('[login.js] script loaded')

function onReady(fn) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fn, { once: true })
  } else {
    fn()
  }
}

onReady(() => {
  // i18n 必须最先跑，否则按钮文案在 doLogin 里取就是英文里没找到
  applyI18n(document)
  bindLangSwitch(document)

  const form = document.getElementById('login-form')
  const errBox = document.getElementById('login-error')
  const btn = document.getElementById('login-btn')
  const usernameEl = document.getElementById('username')
  const passwordEl = document.getElementById('password')

  if (!form || !errBox || !btn || !usernameEl || !passwordEl) {
    console.error('[login.js] missing nodes', {
      form: !!form, errBox: !!errBox, btn: !!btn,
      usernameEl: !!usernameEl, passwordEl: !!passwordEl,
    })
    return
  }
  console.log('[login.js] DOM ready')

  fetch('/api/auth/me', { credentials: 'same-origin' })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (data && data.data) {
        console.log('[login.js] already authed, jump /')
        window.location.href = '/'
      }
    })
    .catch((e) => console.warn('[login.js] me probe failed:', e))

  async function doLogin(ev) {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault()
    if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation()
    console.log('[login.js] doLogin start')

    errBox.style.display = 'none'
    btn.disabled = true
    btn.textContent = t('login.submitting')
    try {
      const username = usernameEl.value.trim()
      const password = passwordEl.value
      if (!username || !password) {
        errBox.textContent = t('login.empty_input')
        errBox.style.display = 'block'
        return
      }
      console.log('[login.js] POST /api/auth/login')
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      console.log('[login.js] status:', resp.status)
      const payload = await resp.json().catch(() => ({}))
      console.log('[login.js] body:', payload)
      if (resp.ok && payload && payload.data) {
        console.log('[login.js] ok, jump /')
        window.location.href = '/'
        return
      }
      errBox.textContent = (payload && payload.error) || t('login.failed')
      errBox.style.display = 'block'
    } catch (e) {
      console.error('[login.js] fetch error:', e)
      errBox.textContent = t('common.network_error') + (e && e.message ? e.message : '')
      errBox.style.display = 'block'
    } finally {
      btn.disabled = false
      btn.textContent = t('login.submit')
    }
    return false
  }

  form.addEventListener('submit', doLogin)
  btn.addEventListener('click', doLogin)
  ;[usernameEl, passwordEl].forEach((el) => {
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doLogin(e)
    })
  })

  console.log('[login.js] handlers bound')
})
