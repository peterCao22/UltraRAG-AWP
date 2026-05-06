import { describe, expect, it } from 'vitest'

import { isAllowedKbUploadFile } from '../utils/uploadGuards.js'

describe('uploadGuards', () => {
  it('accepts pdf and docx with matching or empty MIME', () => {
    expect(isAllowedKbUploadFile({ name: 'a.pdf', type: 'application/pdf', size: 1 })).toBe(true)
    expect(
      isAllowedKbUploadFile({
        name: 'b.docx',
        type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        size: 1,
      }),
    ).toBe(true)
    expect(isAllowedKbUploadFile({ name: 'c.pdf', type: '', size: 1 })).toBe(true)
    expect(isAllowedKbUploadFile({ name: 'd.docx', type: 'application/octet-stream', size: 1 })).toBe(true)
  })

  it('rejects wrong extension or wrong MIME when type is explicit', () => {
    expect(isAllowedKbUploadFile({ name: 'x.txt', type: 'text/plain', size: 1 })).toBe(false)
    expect(isAllowedKbUploadFile({ name: 'x.pdf', type: 'text/plain', size: 1 })).toBe(false)
    expect(isAllowedKbUploadFile({ name: 'x.docx', type: 'image/png', size: 1 })).toBe(false)
  })
})
