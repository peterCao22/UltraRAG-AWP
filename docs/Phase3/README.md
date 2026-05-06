# Phase 3 — H5 前端开发

> 状态：规划中 · 日期：2026-04-09  
> 依赖：Phase 1（核心 RAG 流程）已完成，Phase 2（知识库管理 API）已完成

## 阶段目标

构建一套运行于**内网浏览器**的 **响应式 H5**（PC 与手机），无需 Node/npm 构建环境，对接后端已有的以下 API：

| API 模块 | 接口路径 | 状态 |
|---------|---------|------|
| 对话 | `POST /api/chat`（SSE 流） | Phase 1 已实现 |
| 知识库管理 | `GET/POST/DELETE /api/kb/*` | Phase 2 已实现 |
| 用户认证 | `POST /api/user/login` | Phase 4 预留 |

## 核心交付物

| 文件 | 说明 |
|------|------|
| `custom_app/frontend/index.html` | 对话主页（Nous 式：侧栏含知识库选择器 + 主区对话） |
| `custom_app/frontend/admin.html` | 管理后台（知识库 + 文档管理） |
| `custom_app/frontend/login.html` | 登录页（Phase 4 接入前为空壳） |
| `custom_app/frontend/main.js` | Vue3 应用逻辑（CDN 模式） |
| `custom_app/frontend/style.css` | 全局样式（基于 CSS 变量） |

## 文档目录

| 文档 | 内容 |
|------|------|
| [00_阶段计划.md](00_阶段计划.md) | 阶段拆分、里程碑、验收标准 |
| [01_前端技术架构.md](01_前端技术架构.md) | 技术选型、架构设计、目录结构 |
| [02_界面设计规范.md](02_界面设计规范.md) | 设计系统、色彩、组件规范 |
| [03_页面功能设计.md](03_页面功能设计.md) | 各页面交互与线框（对话页为 Nous 式已定稿） |
| [04_API对接设计.md](04_API对接设计.md) | 前端与后端 API 的对接规范 |
| [05_开发任务清单.md](05_开发任务清单.md) | 可执行任务拆解（带优先级） |
| [06_对齐WeKnora界面及智能推理.md](06_对齐WeKnora界面及智能推理.md) | **Phase 3+ 大任务**：会话列表、智能推理对齐，及**快速问答回复慢（Phase P）**排查与 SLA；实施前需确认 gate |
| [07_Figma结构与页面清单.md](07_Figma结构与页面清单.md) | Figma 页面/画板结构、页面清单、与前端目录对照 |
| [../WeKnora智能推理与UltraRAG移植指南.md](../WeKnora智能推理与UltraRAG移植指南.md) | WeKnora「智能推理」机制说明与向 UltraRAG 移植的分层路线（设计参考，不含代码变更） |
