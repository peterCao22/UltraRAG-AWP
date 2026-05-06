# Phase 3 — Figma 结构说明与页面清单

> 用途：在 Figma 中建项目时的**目录级蓝图**，并与 `01_前端技术架构.md`、`03_页面功能设计.md` 对齐。  
> 约定：内网 **桌面 + 手机** 均需覆盖。桌面画板 **1440×900**（可另加 1280×800）；**手机**至少各增 **390×844**（对话 `CH-mob`、管理 `AD-mob`），与 `03` §0.4 一致。

---

## 一、Figma 文件级结构（建议单文件）

在 Figma 中新建 **1 个 Design file**，名称示例：`AGV-RAG-Phase3-H5`。

### 1.1 Pages（Figma 左侧「页面」Tab 命名）

| Page 名称 | 放什么 | 说明 |
|-----------|--------|------|
| `00 · README` | 1 个说明 Frame | 项目背景、画板尺寸、命名规则、与路由对照表截图 |
| `01 · Tokens` | Color / Type / Space | 从 `02_界面设计规范.md` 抄 CSS 变量为色板与文字样式 |
| `02 · Components` | 组件 Frame | 按钮、输入框、卡片、徽章、Modal 壳、Toast、表格行 |
| `03 · Chat` | 对话流程全部画板 | 对应 `/`、`/chat` |
| `04 · Admin` | 管理后台全部画板 | 对应 `/admin` 及详情子路由 |
| `05 · Login` | 登录壳 | 对应 `/login`（Phase 4 前静态提示） |
| `06 · States` | 跨页面状态 | 全局加载、断网、空列表、403 等（可只放关键屏） |

不必一次画满；**先 `03 · Chat` + `04 · Admin` 各 1～2 个主画板**即可开工对齐。

### 1.2 Frame 命名规范（画板 / 大区块）

格式：`{域}-{两位序号}-{简述}-{状态}`

| 域前缀 | 含义 |
|--------|------|
| `CH` | Chat 对话 |
| `AD` | Admin 管理 |
| `LG` | Login 登录壳 |
| `GL` | Global 全局（Toast、断网条等可单独 Frame） |

**状态**示例：`默认`、`空态`、`加载`、`错误`、`流式输出中`、`索引中`。

示例：`CH-01-对话主页-默认`、`AD-03-知识库详情-索引中`。

### 1.3 单个画板内的层级（建议用 Auto layout，便于和前端 flex 对应）

**对话页（Nous 式，已定稿）**：根节点为 **horizontal**（非「顶栏 + 通栏」）。

```
Screen-CH（1440×900，row，fill）
├── Sidebar（w=260～280，column）
└── Workspace（column，fill）
    ├── MessageList（fill，scroll）
    └── Composer（固定高度或 hug，border-top）
```

**管理后台**：根节点为 **vertical**，顶栏 + 下方横向 `Main`：

```
Screen-AD（column，fill）
├── Header（h=56）
└── Main（row，fill）
    ├── Sidebar（w≈220～240）
    └── Content（fill，scroll）
```

---

## 二、页面清单（整体 IA）

以下为 **「用户能到达的界面」** 清单；路由列与 `03_页面功能设计.md` 一致，实现阶段可与 Flask 路由微调（如 hash 路由 `#/admin`）。

| 序号 | 用户可见名称 | URL / 入口 | Figma Page | 主 Frame 命名示例 | Sprint |
|------|--------------|------------|------------|-------------------|--------|
| P01 | 对话主页 | `/` 或 `/chat`（SPA 同壳） | `03 · Chat` | `CH-01-对话主页-默认` | 1 |
| P02 | 对话主页 · 无知识库 | 同上（数据空） | `03 · Chat` | `CH-01-对话主页-空态` | 1 |
| P03 | 对话主页 · 流式输出中 | 同上 | `03 · Chat` | `CH-01-对话主页-流式输出中` | 1 |
| P04 | 对话主页 · 来源已展开 | 同上 | `03 · Chat` | `CH-02-对话-来源展开` | 2 |
| P05 | 图片 Lightbox | 覆盖层（无独立 URL） | `03 · Chat` | `CH-03-图片放大-覆盖层` | 2 |
| P06 | 管理后台 · 知识库列表 | `/admin` | `04 · Admin` | `AD-01-知识库列表-默认` | 3 |
| P07 | 管理后台 · 列表空态 | `/admin` | `04 · Admin` | `AD-01-知识库列表-空态` | 3 |
| P08 | 新建知识库 · Modal | 自列表页打开 | `04 · Admin` | `AD-10-新建知识库-Modal` | 3 |
| P09 | 知识库详情 · 文档列表 | `/admin/kb/<id>`（前端路由） | `04 · Admin` | `AD-02-知识库详情-默认` | 3 |
| P10 | 知识库详情 · 上传中/索引中 | 同上 | `04 · Admin` | `AD-02-知识库详情-索引中` | 3 |
| P11 | 删除确认 · Modal | 覆盖层 | `04 · Admin` | `AD-11-删除确认-Modal` | 3 |
| P12 | 登录壳（内网免登录提示） | `/login` | `05 · Login` | `LG-01-登录壳-占位` | 4 |
| P13 | 全局 Toast / 断网提示 | 任意页叠加 | `06 · States` | `GL-01-Toast-成功` 等 | 4 |
| P14 | 对话主页（手机） | 同 P01，390 宽 | `03 · Chat` | `CH-mob-01-抽屉关` / `CH-mob-02-抽屉开` | 1～4 |
| P15 | 管理列表（手机） | 同 P06，390 宽 | `04 · Admin` | `AD-mob-01-列表` | 3～4 |

**说明：**

- **P05、P08、P11** 在 Figma 中可与父画板 **同一 Page**，用「连线原型」或单独 Frame 表达覆盖层即可。
- 若只做最小集：**P01、P06、P09、P08** 四个主画板 + **P11** 即可覆盖主路径；**手机自适应需求**下建议再加 **P14**（可仅「抽屉关」一帧 + 标注）与 **P15**。

---

## 三、各主屏的 Figma 内部结构（区块树）

### 3.1 P01 对话主页 `CH-01-对话主页-默认`（Nous 式，已定稿）

与 `03_页面功能设计.md` §1 一致；参考信息架构：[Nous Chat](https://chat.nousresearch.com/)。

```
CH-01（Screen，1440×900，row）
├── Sidebar（w=260～280，column）
│   ├── Brand
│   ├── Btn「新建对话」
│   ├── Select「知识库」
│   ├── Frame「会话列表」（可选，Sprint 1 可仅占位）
│   └── Link「管理后台」
└── Workspace（column，fill）
    ├── MessageList（fill，scroll，column）
    │   ├── Message-User（右对齐气泡）
    │   └── Message-AI（左对齐气泡 + 折叠「引用来源」条）
    └── Composer（column，底部）
        ├── 可选：状态行（如「检索中…」）
        └── Row：Textarea | 发送
```

**元素映射（类 Nous → 本系统）**

| 常见位置 | 本系统内容 |
|----------|------------|
| 侧栏品牌 | AGV 知识库助手 |
| 侧栏「新对话」 | **新建对话** |
| 侧栏「模型」位 | **知识库** 下拉（`GET /api/kb`） |
| 侧栏会话列表 | **可选**；多会话后续迭代 |
| 主区 | 消息流 + **引用来源**（RAG 必保留） |
| 底栏 | Composer（多行输入 + 发送） |
| 侧栏底 | **管理后台** 入口 |

### 3.2 P06 管理列表 `AD-01-知识库列表-默认`

```
AD-01（Screen，column）
├── Header（row，h=56）
│   ├── 标题「管理后台」
│   └── 链接「返回对话」
└── Main（row，fill）
    ├── Sidebar（可选，w=240）
    │   └── 导航项：知识库管理 / 系统状态（占位）
    └── Content（column，fill，padding）
        ├── Toolbar（row）：标题 + 「新建知识库」主按钮
        └── KbCardList（scroll，column，gap）
            └── KbCard（重复）
                ├── 标题行 + 状态 Badge
                ├── 元信息：类型、文档数、最近索引时间
                └── 操作：详情 | 删除
```

### 3.3 P09 知识库详情 `AD-02-知识库详情-默认`

```
AD-02（Screen，column）
├── Header（同 AD-01，可增加面包屑：管理 / 当前 KB 名）
└── Main（column，fill，padding）
    ├── 概要条（KB 名称、状态、触发「重建索引」）
    ├── 文档表格或卡片列表（文件名、状态、上传时间）
    ├── 上传区（拖拽 + 点击，进度条占位）
    └── 任务进度条 / 阶段文案（parse / embed / index）
```

### 3.4 P08 / P11 Modal（通用壳）

```
Modal-Overlay（全屏半透明）
└── Modal-Card（居中，max-w=480）
    ├── Title + 关闭
    ├── Body（表单或警告文案）
    └── Footer（取消 | 主操作）
```

---

## 四、前端目录与 Figma 的对应关系（整体结构）

目标形态见 `01_前端技术架构.md`；下表便于从 **画板 ID** 找到将来要动的 **文件**。

| Figma 域 | 主要实现载体（目标） | 说明 |
|----------|----------------------|------|
| CH-* | `index.html` + `main.js`（或拆 `components/ChatMessage.js` 等） | 单页应用壳，路由可用 hash：`#/chat` |
| AD-* | `admin.html` + `admin.js` | 列表与详情可用 hash：`#/admin`、`#/admin/kb/:id` |
| LG-* | `login.html` | 静态壳，少逻辑 |
| GL-*（Toast 等） | `components/Toast.js` + 全局样式 | 多页复用时抽公共 JS/CSS |
| Tokens / Components | `style.css` + Figma `02 · Components` | 先 CSS 变量，组件样式双向对齐 |

**当前仓库**若仍为「单 `index.html` + hash 切 chat/admin」，可视为 **过渡结构**；与 Figma 的 **信息架构一致即可**，文件拆分可在 Sprint 1 末按上表收敛。

---

## 五、你在 Figma 里「最少要画清楚」的整体结构

1. **对话页**：**横向** `Sidebar | Workspace`；Workspace 内再 **纵向** `MessageList（fill）` → `Composer（贴底）`。  
2. **管理页**：**纵向** `Header` → `Main（Sidebar | Content）`（与对话页骨架不同，勿混用）。  
3. **两条业务线**：`03 · Chat`、`04 · Admin` 各至少 **默认态** 1 张。  
4. **叠加层**：Modal、Lightbox、Toast 各 1 个小 Frame 即可。

按上述建好后，你对「整体结构」的把握应是：

- **对话**：侧栏选库 / 新建对话 / 进管理 → 主区消息列表 → 底栏输入 →（流式）→ 来源折叠/展开 → 图片放大。  
- **管理**：列表 → 新建（Modal）→ 详情 → 上传 → 索引进度 → 删除确认（Modal）。  
- **全局**：Token 与组件在 `01`/`02`；异常态在 `06 · States`。

---

## 六、与现有文档的索引

| 主题 | 文档 |
|------|------|
| 技术目录与组件拆分 | [01_前端技术架构.md](01_前端技术架构.md) |
| 颜色 / 字号 / 间距 Token | [02_界面设计规范.md](02_界面设计规范.md) |
| 交互细节与 ASCII 线框原文（含 Nous 式对话定稿） | [03_页面功能设计.md](03_页面功能设计.md) |
| 阶段里程碑 | [00_阶段计划.md](00_阶段计划.md) |

Figma 画板与 `03`/`07` 的 ASCII 不一致时，**以 `03` 为准**并同步更新画板。
