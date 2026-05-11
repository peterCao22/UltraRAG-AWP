# Phase 3: 前端对话界面优化 (UI Optimization)

> 更新日期: 2026-05-08

## 改动概览

参照 WeKnora 的对话界面设计，对 UltraRAG `custom_app` 的聊天问答区进行全面优化，
主要目标是：**白色背景 + 居中列布局 + 无轮廓助手消息 + 紧凑型输入区**。

---

## 一、WeKnora vs UltraRAG 对比

| 维度 | WeKnora | UltraRAG（优化前） | 优化后 |
|------|---------|-------------------|--------|
| **工作区背景** | 白色 | 灰色 `#f5f5f5` | 白色 `#ffffff` |
| **消息排列** | 居中列，宽屏 | 左对齐，`max-width: 72%` | `align-items: center`，`max-width: 1050px` |
| **助手消息** | 无边框无轮廓，纯文本 | 1px 边框 + 圆角切角 + 阴影 | 无阴影、无边框、无圆角 |
| **用户消息** | 绿色气泡，右对齐 | 蓝色气泡，`max-width: 72%` | 绿色 `#10b981`，`max-width: min(600px, 70%)` |
| **发信人标签** | 不显示 | "我"/"助手"小字 | `display: none` |
| **输入区上方** | 无线 | 1px `border-top` | `border-top: none` |
| **滚动条** | 隐藏 | 显示 | `::-webkit-scrollbar: 0` + `scrollbar-width: none` |
| **正文排版** | 紧凑，行高适中 | 默认浏览器样式 | `font-size: 15px`，`line-height: 1.75` |

---

## 二、涉及文件

| 文件 | 作用 | 状态 |
|------|------|------|
| `frontend/style.css` | 主样式表，所有视觉改动 | 修改 |
| `frontend/main.js` | 推理步骤 summary 增加轮数显示 | 修改 |
| `frontend/components/sourcePanel.js` | 引用来源按钮文案简化 | 修改 |
| `frontend/__tests__/sourcePanel.test.js` | 测试适配新文案 | 修改 |
| `frontend/__tests__/main.test.js` | 测试适配新文案 | 修改 |

---

## 三、详细改动

### 1. `style.css` — 整体布局

**工作区背景改为白色**
```css
.chat-workspace {
  align-items: center;    /* 居中列 */
  background: var(--color-bg-card); /* 白色 */
}
```

**消息列表居中排列**
```css
.message-list {
  width: 100%;             /* 必须撑满工作区，否则会随内容收缩 */
  background: var(--color-bg-card);
  display: flex;
  flex-direction: column;
  align-items: center;   /* 子元素居中 */
  padding: 48px ...;     /* 上下留白加宽 */
}

/* 隐藏滚动条 */
.message-list::-webkit-scrollbar { width: 0; background: transparent; }
.message-list { scrollbar-width: none; }
```

### 2. `style.css` — 消息气泡

**基础消息**
```css
.message {
  flex: 0 0 auto;           /* 禁止 flex column 压缩消息高度，避免图片回答与后续消息重叠 */
  width: 100%;
  max-width: 1050px;         /* 800px → 1050px，更开阔 */
  margin-bottom: var(--space-xl); /* 间距加大 */
}
```

**助手消息（无轮廓）**
```css
.message.ai {
  box-shadow: none;          /* 去掉阴影 */
  border-radius: 0;          /* 去掉圆角 */
  font-size: 15px;
  line-height: 1.75;
}
```

**用户消息（绿色气泡）**
```css
.message.user {
  max-width: 1050px;         /* 透明行容器，与助手内容列同宽 */
  align-self: center;
  background: transparent;
}

.message.user [data-role="message-content"] {
  width: fit-content;        /* 短问题按内容展开，避免中文被过早换行 */
  max-width: min(720px, 82%);
  margin-left: auto;         /* 在内容列内右对齐，而不是贴到页面最右侧 */
  background: #10b981;       /* 绿色气泡 */
  font-size: 15px;
  line-height: 1.6;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-lg);
}
```

**发信人标签隐藏**
```css
.message > strong { display: none; }
```

### 3. `style.css` — Markdown 正文排版（新增）

```css
.message.ai [data-role="message-content"] {
  font-size: 15px;
  line-height: 1.75;
}

.message.ai [data-role="message-content"] p { margin: 0.5em 0; }
.message.ai [data-role="message-content"] code {
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  background: var(--color-bg-base);
  font-size: 0.88em;
  font-family: var(--font-mono);
}
.message.ai [data-role="message-content"] pre {
  background: #282c34;
}
.message.ai [data-role="message-content"] pre code {
  color: #abb2bf;
  background: none;
}
.message.ai [data-role="message-content"] blockquote {
  border-left: 3px solid var(--color-primary);
  background: var(--color-primary-light);
}
.message.ai [data-role="message-content"] th,
.message.ai [data-role="message-content"] td {
  border: 1px solid var(--color-border);
}
```

### 4. `style.css` — 输入区

```css
.composer {
  max-width: 1050px;        /* 和消息列对齐 */
  border-top: none;          /* 去掉上方分割线 */
  padding: var(--space-md) var(--space-xl) ...;
}

.composer-input {
  border: none;              /* 去掉边框 */
  border-radius: var(--radius-lg);
  font-size: 14px;
  line-height: 1.6;
}

/* 字符计数 */
[data-role="char-count"] {
  font-size: var(--text-xs);
  color: var(--color-text-disabled) !important;
}

/* 发送按钮 */
.send-button {
  border-radius: var(--radius-lg);
  font-weight: 500;
}
.send-button:hover {
  box-shadow: 0 2px 8px rgba(22, 119, 255, 0.25);
}
```

### 5. `style.css` — 引用来源

```css
.source-placeholder {
  opacity: 0.75;            /* 更轻 */
  border-left: none;         /* 去掉左侧蓝条 */
}

.source-panel-toggle {
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-size: var(--text-xs);
  font-weight: 500;
  /* 去掉边框，改为浅蓝背景标签 */
}

.source-card {
  padding: var(--space-xs) var(--space-sm);  /* 更紧凑 */
  border-left: 3px solid var(--color-primary);
}

.source-card__title {
  font-size: var(--text-xs);  /* 12px */
}

.source-card__excerpt {
  font-size: var(--text-xs);
  -webkit-line-clamp: 2;      /* 3行 → 2行 */
}
```

### 6. `style.css` — 推理步骤

```css
.reasoning-steps {
  /* 收起时不带边框，展开时才有 */
}
.reasoning-steps[open] {
  border: 1px solid var(--color-border);
}
.reasoning-steps__summary {
  font-weight: 500;
  color: var(--color-text-secondary);
}
.reasoning-steps__summary:hover {
  color: var(--color-text-primary);
}

/* 推理轮次标签去掉 text-transform: uppercase（中文场景不需要） */
.reasoning-round__label {
  /* 移除了 text-transform: uppercase */
}

/* tool call 显示为内联标签 */
.reasoning-steps__tool-call {
  display: inline-block;
}
```

### 7. `style.css` — 移动端适配

```css
@media (max-width: 767px) {
  .chat-workspace {
    align-items: stretch;    /* 全宽 */
  }
  .message-list {
    align-items: stretch;
  }
  .message {
    max-width: 100%;
  }
  .message.user {
    max-width: 100%;
  }
  .composer {
    max-width: 100%;
    padding: var(--space-sm) var(--space-md) ...;
  }
}
```

### 8. `main.js` — 推理步骤显示轮数

```javascript
function finish() {
  // ...
  sum.textContent = `推理步骤 · ${roundCount} 轮`
  details.open = false
}
```

### 9. `sourcePanel.js` — 文案简化

```javascript
// 之前: "📄 引用来源（2 处）▾"
// 之后: "引用来源（2）"
toggle.textContent = `引用来源（${n}）`
// 展开时: "引用来源（2）收起"
```

### 10. 测试适配

| 测试文件 | 改动 |
|---------|------|
| `__tests__/sourcePanel.test.js` | `"引用来源（2 处）"` → `"引用来源（2）"`，`"▾"` → `"收起"` |
| `__tests__/main.test.js` | `"引用来源（1 处）"` → `"引用来源（1）"` |

---

## 四、验证

```bash
npm test
# Test Files  17 passed (17)
# Tests       154 passed (154)
```

所有前端测试通过，无回归。
