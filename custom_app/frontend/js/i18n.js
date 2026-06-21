/**
 * 轻量级 i18n —— 仅服务对话页 + 登录页。
 *
 * 使用方式（HTML 端）：
 *   <span data-i18n="login.title">中文兜底</span>
 *   <input data-i18n-attr="placeholder:composer.placeholder,title:composer.title">
 *
 * 使用方式（JS 端）：
 *   import { t, getLang, setLang } from './i18n.js'
 *   alert(t('login.failed'))
 *
 * 切换：setLang('en') → 刷新页面（避免运行时重渲染动态片段）。
 */

const STORAGE_KEY = 'ULTRARAG_LANG'
const DEFAULT_LANG = 'zh'
const SUPPORTED = new Set(['zh', 'en'])

const DICT = {
  zh: {
    'lang.name.zh': '中文',
    'lang.name.en': 'English',
    'lang.switch': '切换语言',
    'common.cancel': '取消',
    'common.confirm': '确认',
    'common.close': '关闭',
    'common.loading': '加载中…',
    'common.network_error': '网络错误：',

    // ── 登录页 ────────────────────────────────────────────────────
    'login.page_title': 'AGV 知识库助手 · 登录',
    'login.title': 'AGV 知识库助手',
    'login.subtitle': '请使用管理员分配的账号登录',
    'login.username': '用户名',
    'login.password': '密码',
    'login.submit': '登录',
    'login.submitting': '登录中…',
    'login.hint': '首次登录默认账号见管理员；登录后请尽快修改密码。',
    'login.empty_input': '请输入用户名和密码',
    'login.failed': '登录失败',

    // ── 对话页 ────────────────────────────────────────────────────
    'chat.page_title': 'AWP 知识库助手 · 对话',
    'chat.brand_title': 'AWP 知识库助手',
    'chat.brand_subtitle': '面向企业生产经营相关的私有问答系统',
    'chat.new_chat': '新聊天',
    'chat.search_chat': '搜索聊天',
    'chat.kb_label': '知识库',
    'chat.kb_placeholder': '请选择知识库',
    'chat.session_caption': '会话列表',
    'chat.session_empty': '暂无历史会话，点击「新建对话」开始。',
    'chat.greeting': '您好，请先选择知识库，然后输入 AGV 操作问题。',
    'chat.greeting_with_kb_hint': '您好！请选择知识库后开始提问。',
    'chat.source_placeholder': '引用来源将在回答完成后展示。',
    'chat.agent.quick': '智能体：快速问答',
    'chat.agent.agent': '智能体：智能推理',
    'chat.default_model': '默认模型',
    'chat.deep_reasoning': '深度思考',
    'chat.deep_reasoning_tip': '深度思考模式：多轮检索 + 推理链，适合跨文档/多步骤问题（耗时 ~2 倍）',
    'chat.composer_placeholder': '输入您的问题，Enter 发送，Shift+Enter 换行',
    'chat.send': '发送',
    'chat.admin_link': '管理后台',
    'chat.logout': '退出登录',
    'chat.sidebar_open': '打开侧栏',
    'chat.sidebar_toggle_aria': '打开导航菜单',
    'chat.kb_load_failed': '知识库加载失败：',
    'chat.assistant_label': '助手',
    'chat.model_chip_aria': '切换对话模型',
    'chat.model_chip_title': '切换对话模型',
    'chat.agent_select_aria': '智能体模式',
    'chat.char_count_aria': '已输入字数',
  },
  en: {
    'lang.name.zh': '中文',
    'lang.name.en': 'English',
    'lang.switch': 'Switch language',
    'common.cancel': 'Cancel',
    'common.confirm': 'Confirm',
    'common.close': 'Close',
    'common.loading': 'Loading…',
    'common.network_error': 'Network error: ',

    // ── Login ────────────────────────────────────────────────────
    'login.page_title': 'AGV Knowledge Assistant · Sign in',
    'login.title': 'AGV Knowledge Assistant',
    'login.subtitle': 'Please sign in with the account assigned by your admin.',
    'login.username': 'Username',
    'login.password': 'Password',
    'login.submit': 'Sign in',
    'login.submitting': 'Signing in…',
    'login.hint': 'Default credentials come from your admin; change your password after first sign-in.',
    'login.empty_input': 'Please enter username and password.',
    'login.failed': 'Sign-in failed',

    // ── Chat ─────────────────────────────────────────────────────
    'chat.page_title': 'AWP Knowledge Assistant · Chat',
    'chat.brand_title': 'AWP Knowledge Assistant',
    'chat.brand_subtitle': 'A private Q&A system for enterprise operations.',
    'chat.new_chat': 'New chat',
    'chat.search_chat': 'Search chats',
    'chat.kb_label': 'Knowledge base',
    'chat.kb_placeholder': 'Select a knowledge base',
    'chat.session_caption': 'Sessions',
    'chat.session_empty': 'No sessions yet. Click "New chat" to start.',
    'chat.greeting': 'Hi — please pick a knowledge base, then ask your AGV operations question.',
    'chat.greeting_with_kb_hint': 'Hi! Pick a knowledge base to start asking.',
    'chat.source_placeholder': 'Sources will appear here after the answer finishes.',
    'chat.agent.quick': 'Agent: Quick Q&A',
    'chat.agent.agent': 'Agent: Smart reasoning',
    'chat.default_model': 'Default model',
    'chat.deep_reasoning': 'Deep reasoning',
    'chat.deep_reasoning_tip': 'Deep reasoning: multi-round retrieval + reasoning chain. Best for cross-document or multi-step questions (~2x slower).',
    'chat.composer_placeholder': 'Type your question. Enter to send, Shift+Enter for newline.',
    'chat.send': 'Send',
    'chat.admin_link': 'Admin console',
    'chat.logout': 'Sign out',
    'chat.sidebar_open': 'Open sidebar',
    'chat.sidebar_toggle_aria': 'Open navigation menu',
    'chat.kb_load_failed': 'Failed to load knowledge bases: ',
    'chat.assistant_label': 'Assistant',
    'chat.model_chip_aria': 'Switch chat model',
    'chat.model_chip_title': 'Switch chat model',
    'chat.agent_select_aria': 'Agent mode',
    'chat.char_count_aria': 'Character count',
  },
}

function readStoredLang() {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY)
    if (v && SUPPORTED.has(v)) return v
  } catch {
    /* ignore */
  }
  return DEFAULT_LANG
}

let currentLang = readStoredLang()

export function getLang() {
  return currentLang
}

export function setLang(lang) {
  const next = SUPPORTED.has(lang) ? lang : DEFAULT_LANG
  try {
    window.localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* ignore */
  }
  currentLang = next
}

export function t(key, fallback = '') {
  const dict = DICT[currentLang] || DICT[DEFAULT_LANG]
  if (key in dict) return dict[key]
  // 兜底用默认语言；仍未命中给 fallback 或 key 本身
  if (currentLang !== DEFAULT_LANG && key in DICT[DEFAULT_LANG]) {
    return DICT[DEFAULT_LANG][key]
  }
  return fallback || key
}

/**
 * 遍历 root（默认 document）替换 data-i18n / data-i18n-attr 节点的文本。
 *
 * data-i18n="key"  → textContent
 * data-i18n-attr="attr1:key1,attr2:key2"  → 对应 attribute
 *
 * 不存在的 key 会保留原文本（充当中文兜底）。
 */
export function applyI18n(root = document) {
  if (!root || !root.querySelectorAll) return
  root.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n')
    if (!key) return
    const val = t(key, el.textContent)
    el.textContent = val
  })
  root.querySelectorAll('[data-i18n-attr]').forEach((el) => {
    const spec = el.getAttribute('data-i18n-attr') || ''
    spec.split(',').map((s) => s.trim()).filter(Boolean).forEach((pair) => {
      const [attr, key] = pair.split(':').map((s) => s.trim())
      if (!attr || !key) return
      const val = t(key, el.getAttribute(attr) || '')
      if (val) el.setAttribute(attr, val)
    })
  })
  // 同步 <html lang="..."> 给屏幕阅读器
  try {
    document.documentElement.setAttribute('lang', currentLang === 'en' ? 'en' : 'zh-CN')
  } catch {
    /* ignore */
  }
}

/**
 * 给 root 内的语言切换按钮挂事件 — 点击后切换并刷新页面。
 *
 * 期望 DOM：button[data-role="lang-switch"]，内部一个 [data-role="lang-name"] 显示当前语言名。
 */
export function bindLangSwitch(root = document) {
  const btn = root.querySelector('[data-role="lang-switch"]')
  if (!btn) return
  const nameEl = btn.querySelector('[data-role="lang-name"]')
  // 按钮显示「另一种语言」，提示用户点这里能切换到那个
  const other = currentLang === 'zh' ? 'en' : 'zh'
  if (nameEl) nameEl.textContent = t(`lang.name.${other}`)
  btn.setAttribute('aria-label', t('lang.switch'))
  btn.addEventListener('click', () => {
    setLang(other)
    window.location.reload()
  })
}
