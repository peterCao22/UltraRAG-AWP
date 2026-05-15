/**
 * 管理端知识库上传：按 KB 类型动态判断允许的扩展名和 MIME。
 *
 * sop_docx：仅 .docx / .pdf（历史行为保持不变）
 * general ：.docx / .pdf / .md / .markdown / .txt / .png / .jpg / .jpeg / .bmp / .tiff / .tif
 *
 * 部分浏览器对拖拽文件给出空 type 或 application/octet-stream，此时仅凭扩展名判断。
 */

/** SOP 类型允许的 MIME。 */
const SOP_ALLOWED_MIMES = new Set([
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
])

/** General 类型额外允许的 MIME（图片 / 文本）。 */
const GENERAL_EXTRA_MIMES = new Set([
  'text/markdown',
  'text/plain',
  'text/x-markdown',
  'image/png',
  'image/jpeg',
  'image/bmp',
  'image/tiff',
])

/** General 类型允许的扩展名集合。 */
const GENERAL_ALLOWED_EXTS = new Set([
  '.pdf', '.docx',
  '.md', '.markdown', '.txt',
  '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif',
])

/** SOP 类型允许的扩展名集合。 */
const SOP_ALLOWED_EXTS = new Set(['.pdf', '.docx'])

/**
 * 返回指定 KB 类型的上传文件 accept 字符串（用于 input[accept]）。
 *
 * @param {'sop_docx'|'general'} kbType
 * @returns {string}
 */
export function getAcceptAttr(kbType) {
  if (kbType === 'general') {
    return '.docx,.pdf,.md,.markdown,.txt,.png,.jpg,.jpeg,.bmp,.tiff,.tif'
  }
  return '.docx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document'
}

/**
 * 返回指定 KB 类型的上传提示文字（短格式，用于 dropzone）。
 *
 * @param {'sop_docx'|'general'} kbType
 * @returns {string}
 */
export function getUploadHint(kbType) {
  if (kbType === 'general') {
    return '.pdf / .docx / .md / .txt / 图片（.png .jpg .tiff）'
  }
  return '.docx / .pdf'
}

/**
 * 是否允许上传该文件（扩展名 + MIME 组合判断）。
 *
 * @param {File} file
 * @param {'sop_docx'|'general'} [kbType='sop_docx']
 * @returns {boolean}
 */
export function isAllowedKbUploadFile(file, kbType = 'sop_docx') {
  if (!file || typeof file.name !== 'string') return false
  const name = file.name.toLowerCase()
  const ext = name.slice(name.lastIndexOf('.'))

  const allowedExts = kbType === 'general' ? GENERAL_ALLOWED_EXTS : SOP_ALLOWED_EXTS
  if (!allowedExts.has(ext)) return false

  const mime = String(file.type || '').toLowerCase()
  if (!mime) return true
  if (mime === 'application/octet-stream' || mime === 'binary/octet-stream') return true
  if (SOP_ALLOWED_MIMES.has(mime)) return true
  if (kbType === 'general' && GENERAL_EXTRA_MIMES.has(mime)) return true
  return false
}
