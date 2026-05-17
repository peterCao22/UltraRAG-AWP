# Phase 8.0 —— 兜底滑窗切分（结构松散文档保底）

> **状态**：✅ 已实施（2026-05-17）
> **草案讨论**：2026-05-16
> **前置**：无（Phase 5 已完成的存储栈即可）
> **借用**：❌ 不借 UltraRAG，纯 custom_app 内部小改
> **预计工时**：0.5-1 天
> **定位**：Phase 8.1 评测的**前置数据修复**，不是优化项

---

## 一、目标

修复 [`docx_parser.py`](../../custom_app/services/docx_parser.py) 兜底分支「整篇文档拍成单个 `_full` chunk」的问题，让**没有 STEP、也没有 Heading**的结构松散文档也能按合理粒度切分。

完成本阶段后，Phase 8.1 评测才能在所有 KB 上公平打分（避免"超长单 chunk 永远召不回"导致评测信号失真）。

---

## 二、为什么单独成档

| 维度 | 说明 |
|------|------|
| 不是优化 | 这是个**数据完整性 bug fix**，不需要评测验证收益——切不出 chunk 的文档本来就检索不到 |
| 不进 8.1 | 8.1 是评测体系搭建，8.0 是被评测对象（数据）的修复，先后顺序明确 |
| 不进 8.2 | 8.2 在 chunk 基础上加 contextual + BM25；如果 chunk 本身就是错的（一篇文档=1 chunk），8.2 改了也没用 |
| 工时极小 | 0.5-1 天，独立成档便于快速过审 + 验收 |

---

## 三、问题现状

### 3.1 当前切分规则（优先级）

[`docx_parser.py:209-425`](../../custom_app/services/docx_parser.py#L209) `parse_docx` 函数：

1. **优先**：文档含 `STEP N:` → 按 STEP 切（每 STEP 1 个 chunk）
2. **次选**：文档含 Heading 1/2/3 或"整段加粗短行" → 按 Heading 切（`_section_N`）
3. **兜底**：两者都没有 → 整篇拍成 1 个 `_full` chunk（[L397-423](../../custom_app/services/docx_parser.py#L397-L423)）

### 3.2 兜底路径的问题

WeKnora 等长度切分系统能切出多块；UltraRAG custom_app 在结构松散文档上只切出 1 块。**影响范围**：

| 文档类型 | 影响 |
|---------|------|
| FAQ 汇编（无 STEP、无 Heading） | 整篇 1 块，无法按问题召回单条 |
| 长篇连续叙述（培训手册散文段） | 整篇 1 块，超长 chunk embedding 信息稀释 |
| 历史导入的旧文档 | 同上 |
| 用户上传的非标准 SOP | 同上 |

### 3.3 已知现状

`agv_demo` / `ifs_docs` 这种规范 SOP 用 STEP/Heading 切得很好；问题主要出在**未来上传的非标准文档**上。本阶段属于**预防性加固**。

---

## 四、设计方案

### 4.1 触发条件

修改兜底分支 [L396-423](../../custom_app/services/docx_parser.py#L396-L423)：

- **保留**：如果 STEP/Heading 路径已切出 chunks，**不动**（向后兼容现有 KB）
- **新增**：如果走到 `_full` 兜底路径，**且** 整篇字符数超过阈值 → 改走滑窗切分

```python
SLIDING_WINDOW_THRESHOLD_CHARS = 800   # 不到这个字数，保持单 _full chunk
SLIDING_WINDOW_SIZE_CHARS      = 800   # 单 chunk 目标字数
SLIDING_WINDOW_OVERLAP_CHARS   = 100   # 相邻 chunk 重叠字数
```

### 4.2 滑窗策略

按**段落边界 + 字符长度**切，不是按 token 切（避免 tokenizer 依赖）：

1. 收集所有段落（已 strip）和表格文本到 `parts: list[str]`
2. 逐 part 累加到 buffer；buffer 长度 ≥ `SLIDING_WINDOW_SIZE_CHARS` 时 flush 成一个 chunk
3. flush 时保留最后 `SLIDING_WINDOW_OVERLAP_CHARS` 字到下一个 buffer 开头
4. 不切分单个段落内部（避免句子截断）

伪代码：

```python
def _sliding_window_chunks(parts: list[str], imgs_per_part: list[list[str]]) -> list[tuple[list[str], list[str]]]:
    """返回 [(chunk_lines, chunk_imgs), ...]"""
    out = []
    buf_lines, buf_imgs, buf_len = [], [], 0
    for line, line_imgs in zip(parts, imgs_per_part):
        if buf_len + len(line) > SLIDING_WINDOW_SIZE_CHARS and buf_lines:
            out.append((buf_lines[:], buf_imgs[:]))
            # 保留尾部 overlap
            tail, tail_len = [], 0
            for prev in reversed(buf_lines):
                if tail_len + len(prev) > SLIDING_WINDOW_OVERLAP_CHARS:
                    break
                tail.insert(0, prev)
                tail_len += len(prev)
            buf_lines, buf_imgs, buf_len = tail, [], tail_len
        buf_lines.append(line)
        buf_imgs.extend(line_imgs)
        buf_len += len(line)
    if buf_lines:
        out.append((buf_lines, buf_imgs))
    return out
```

### 4.3 Chunk ID 命名

兜底滑窗的 chunk_id 用 `<doc_stem>_window_N`，区分于：
- `<doc_stem>_step_N`（STEP 切分）
- `<doc_stem>_section_N`（Heading 切分）
- `<doc_stem>_full`（**单 chunk** 短文档兜底，保留向后兼容）

### 4.4 图片归属

按段落顺序累加，flush 时**当前 buffer 内的所有图片**归该 chunk。`overlap` 部分的段落如果带图，图片**只归原 chunk，不重复**进下一个 chunk（避免同图重复检索）。

### 4.5 改动文件

| 文件 | 改动 |
|------|------|
| [`custom_app/services/docx_parser.py`](../../custom_app/services/docx_parser.py) | 拆出 `_sliding_window_chunks` 函数；改造兜底分支 [L396-423](../../custom_app/services/docx_parser.py#L396-L423) |
| `tests/test_docx_parser_sliding.py` | **新增**：4-5 个 case |
| [`docs/Phase8/README.md`](./README.md) | 加 8.0 行（顺序工作） |

**不动**的：
- STEP 切分逻辑（[L51-78](../../custom_app/services/docx_parser.py#L51-L78)）
- Heading 切分逻辑（[L188-206](../../custom_app/services/docx_parser.py#L188-L206)）
- 图片抽取（[L91-176](../../custom_app/services/docx_parser.py#L91-L176)）
- Chunk schema（不加字段）

---

## 五、任务拆分

| 子任务 | 工时 | 复杂度 | 验收 |
|--------|------|--------|------|
| 8.0.1 抽 `_sliding_window_chunks` 纯函数 | 1h | LOW | 5 个单测 case 通过 |
| 8.0.2 改造兜底分支，按字符阈值路由 | 1h | LOW | 单测：短文档走 `_full`，长文档走 `_window_N` |
| 8.0.3 表格归属测试（表格在哪段就属哪个 window） | 0.5h | LOW | 1 个单测 |
| 8.0.4 图片归属测试（无重复入下个 window） | 0.5h | LOW | 1 个单测 |
| 8.0.5 跑一遍 ingest 验证（造一份 FAQ 文档） | 0.5h | LOW | chunks.jsonl 多个 `_window_N` |
| **合计** | **3.5h（约半天）** | | |

---

## 六、关键设计抉择

| 议题 | 抉择 | 理由 |
|------|------|------|
| 触发条件 | 字符数 ≥ 800 才滑窗，否则保持 `_full` | 短文档没必要切；800 字是 Gemini embedding 的合理粒度下限 |
| 切分单位 | 字符数（不是 token） | 不依赖 tokenizer；中文环境近似换算 1 char ≈ 1 token，误差可接受 |
| 切分边界 | 段落整体（不在段落中间截断） | 保留语义完整；牺牲一点窗口大小均匀性 |
| Overlap | 100 字 | 行业经验：8-15% 重叠率；过大冗余，过小信息断裂 |
| chunk_id 命名 | `_window_N` 独立于现有命名 | 便于后续区分召回来源（是结构化切还是兜底切） |
| 是否回填旧 KB | **不回填** | 现有 KB 都是结构化文档，不触发兜底；本期纯预防 |
| 阈值是否可配置 | **暂不配置** | 写死常量；如未来需调，再迁 yaml |

---

## 七、关键风险

| 等级 | 风险 | 缓解 |
|------|------|------|
| 🟢 LOW | 单段落本身就超过 800 字 → 该 chunk 偏大 | 不切单段落，保持语义；超大段落极少见，可接受 |
| 🟢 LOW | overlap 段落带图 → 图片在两个 chunk 都引用 | 明确实现：overlap 段落只复制文本，不复制图片 |
| 🟢 LOW | 表格被切到不同 window | 已有 `_table_to_text` 把表压成单行，按整行加入 buffer，不会被切断 |

---

## 八、待讨论问题

1. **阈值 800/800/100 是否合理**？参考 Anthropic / LangChain 默认值（500-1000 字），我倾向 800；你有偏好吗？
2. **是否做"按句子切分"备选**？需要中文分句库（如 `pkuseg` 或简单正则），增加依赖。当前按段落切已经够稳，不引入
3. **超大文档（>10 万字）是否限制 chunk 总数**？目前没限制；如有需要可加 `MAX_CHUNKS_PER_DOC = 200` 兜底
4. **是否在 admin 分块视图标注切分来源**？（STEP / Heading / Window / Full）—— 这是**未来 admin 优化的事**，本期不做

---

## 九、验收清单

- [ ] `_sliding_window_chunks` 纯函数单测通过（5 case）
- [ ] 兜底分支按字符阈值路由（短文档不变，长文档切多块）
- [ ] 表格归属正确
- [ ] 图片不重复入相邻 window
- [ ] 现有 `agv_demo` / `ifs_docs` 重跑 ingest，**chunks.jsonl 内容不变**（向后兼容）
- [ ] 造一份 1500+ 字 FAQ 文档跑通，切出 2-3 个 `_window_N` chunk
- [ ] [`docs/Phase8/README.md`](./README.md) 子阶段列表更新

---

> 本阶段是 Phase 8.1 评测的**数据前置**。讨论确认后即可实施，半天落地。

---

## 十、实施记录（2026-05-17）

### 落地差异

| 计划点 | 实际实施 | 原因 |
|---|---|---|
| 短文档兜底命名 `_full` | **保留 `_intro`** | 落地前发现现有 `agv_demo` / `ifs_docs` 把"无 STEP 无 Heading 的短文档"已命名为 `_intro`（5 个），改为 `_full` 会破坏 §九"chunks.jsonl 不变"的验收。保留 `_intro` 命名；`_window_N` 命名仅用于新增的长文档分支。 |
| 兜底分支位置 | **finalize 段，非死代码 `if not chunks_out` 分支** | 现有代码 `_full` 分支仅在主循环全空时触发（死代码）。真正接管"无 STEP 无 Heading"文档的是 finalize 段对 `intro_lines` 的处理。改造改在那里，并清理了原死代码分支。 |
| `_sliding_window_chunks` 超大单段处理 | 单段长度 > overlap → 不进入 tail（避免被复制到下一 chunk） | PLAN §四.2 伪代码未覆盖该 case；测试 `test_oversized_single_part_kept_whole` 暴露问题后修复。 |

### 改动文件

- [`custom_app/services/docx_parser.py`](../../custom_app/services/docx_parser.py)
  - 新增常量 `SLIDING_WINDOW_THRESHOLD_CHARS / SIZE_CHARS / OVERLAP_CHARS`（800/800/100）
  - 新增 `_sliding_window_chunks(parts, imgs_per_part, *, size, overlap)`
  - 新增 `_split_intro_for_windows(intro_lines, intro_imgs)`（把含 `[IMG:]` 占位的扁平 lines 拆回 parts + imgs_per_part）
  - 改造 finalize 段：无 STEP 无 Heading 文档按字符阈值路由 `_intro` / `_window_N`
  - 删除原死代码兜底块 `if not chunks_out`（功能由 finalize 段接管）
- [`tests/test_docx_parser_sliding.py`](../../tests/test_docx_parser_sliding.py) **新增**：9 个 case
  - 纯函数 6 case：短输入、多段分块、overlap 不复制图片、超大单段保留、参数对齐校验、表格整行不切断
  - 集成 3 case：短文档保持 `_intro`、长文档切多块 `_window_N`、阈值常量自洽

### 验收结果

- ✅ `pytest tests/test_docx_parser_sliding.py` 9/9 通过
- ✅ `pytest tests/test_docx_parser_schema.py tests/test_hotfix_inline_image_position.py` 既有 docx_parser 套件全绿
- ✅ 重跑 `agv_demo` / `ifs_docs`：56 + 16 = 72 个旧 chunk 全部存在且未变为 `_window_N`；唯一新增的 `E-Stop SOP_1_intro` 来自源目录中新增的 docx 文件（与改动无关）；`E-Stop SOP_intro` 内容微变同样来自源 docx 改动
- ✅ 现有 KB 均未进入滑窗分支（所有文档要么是结构化 STEP/Heading 文档，要么 < 800 字阈值）—— Phase 8.0 是预防性加固，对现网零影响

### 阈值待 8.1 评测后调整

`800 / 800 / 100` 是 PLAN 建议默认值。Phase 8.1 评测体系上线后，若发现：
- 长 FAQ 文档召回率偏低 → 调小 `SIZE_CHARS`（如 600）
- 上下文截断频繁 → 调大 `OVERLAP_CHARS`（如 150-200）
- 短文档不切但评测分数差 → 调低 `THRESHOLD_CHARS`（如 600）

调整时直接改 [`docx_parser.py`](../../custom_app/services/docx_parser.py) 顶部常量；未来若需运行时可调，再迁 `parameter.yaml`。
