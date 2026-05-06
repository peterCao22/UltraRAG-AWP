/**
 * 管理端知识库上传：在扩展名白名单之外，用 MIME 做二次校验（清单 P1）。
 * 部分浏览器对拖拽文件给出空 type 或 application/octet-stream，此时仍要求扩展名为 .pdf / .docx。
 */

/** 允许的文档 MIME（小写比较）。 */
export const KB_UPLOAD_ALLOWED_MIMES = new Set([
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
])

/**
 * 是否允许上传该文件（扩展名 + MIME 组合判断）。
 *
 * @param {File} file
 * @returns {boolean}
 */
export function isAllowedKbUploadFile(file) {
  if (!file || typeof file.name !== 'string') return false
  const name = file.name.toLowerCase()
  const extOk = name.endsWith('.pdf') || name.endsWith('.docx')
  if (!extOk) return false

  const mime = String(file.type || '').toLowerCase()
  if (!mime) return true
  if (KB_UPLOAD_ALLOWED_MIMES.has(mime)) return true
  if (mime === 'application/octet-stream' || mime === 'binary/octet-stream') return true
  return false
}
