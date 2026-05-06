# Phase 3 Vendor Dependencies

All files in this directory are checked in so the AGV H5 frontend can run in an intranet environment without CDN access.

| File | Version | Source | SHA256 |
| --- | --- | --- | --- |
| `vue.global.prod.js` | 3.4.38 | `https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js` | `B50EEEFE35D41636BB96C92B40F1DF0B4FB7914E07B3C625B1EC15E9748767B9` |

对话页 `index.html` / 管理页 `admin.html` 当前**不**加载 Vue（`main.js` / `admin.js` 为原生 DOM）；本文件仍保留于 `vendor/`，供遗留 `frontend/js/main.js` 等示例或后续 Phase 使用。
| `marked.min.js` | 9.1.6 | `https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js` | `6002AF63485B043FA60DDABA1B34363B98D2A8B2C63B607004F3A2405A8A053A` |
| `DOMPurify.min.js` | 3.1.6 | `https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js` | `C0845096A7C4A6741F362AC506C94C1C7D27DC603BCC1BF64A587F76F2DBE3A1` |
