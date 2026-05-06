# T2 — DOCX 解析与入库

> 核心定制开发。这是与 UltraRAG 现有能力差异最大的部分：需要在入库阶段将**图片与段落精确绑定**，供检索时携带图片元数据。

---

## 现有 UltraRAG 的 DOCX 处理能力与缺口

| 功能 | UltraRAG 现有 | Phase 1 补充 |
|------|--------------|-------------|
| 段落文本抽取 | ✅ `_read_docx_text()` | 需改为**带段落序号**输出 |
| 表格文本抽取 | ✅ `cell1 \| cell2 \| ...` | 需记录**表格所在段落位置** |
| 嵌入图片导出 | ❌ 完全不做 | **需自定义**：导出 + 绑定 para_idx |
| chunk 含图片路径 | ❌ | **需自定义**：chunk 元数据含 `images[]` |

---

## DOCX 内部结构与图片绑定原理

DOCX 本质是 ZIP，`word/document.xml` 是正文。结构如下：

```xml
<w:body>
  <w:p>  <!-- 段落 para_idx=0 -->
    <w:r><w:t>第一步：确认 AGV 已停至换电位</w:t></w:r>
  </w:p>
  <w:p>  <!-- 段落 para_idx=1，含内嵌图片 -->
    <w:r>
      <w:drawing>
        <wp:inline>
          <a:blip r:embed="rId5"/>  <!-- 图片关系 ID -->
        </wp:inline>
      </w:drawing>
    </w:r>
  </w:p>
  <w:tbl>  <!-- 表格，在段落序列中占位 -->
    <w:tr><w:tc>...</w:tc></w:tr>
  </w:tbl>
</w:body>
```

**绑定规则**：遍历 `w:body` 的直接子元素，按顺序编号（无论是 `<w:p>` 还是 `<w:tbl>`）。每遇到含 `<w:drawing>` 的段落，提取图片并记录其 `para_idx`。分块时 chunk 覆盖段落范围 `[start, end)`，范围内的图 `para_idx` 都归属该 chunk。

> **实现说明**：Phase 1 的 `docx_parser.py` 在段落级采用下文的 **Run 顺序 + STEP 规范化**，与上表「按 para_idx 绑定」的设计目标一致，但具体算法以代码为准。

---

## Run 顺序与 STEP 粘连规范化

### 1. 段落内 `w:r`（Run）顺序

Word 中**文字与图片的先后顺序**由多个 `w:r` 在 `w:p` 中的 XML 顺序决定，不能只看合并后的 `paragraph.text`。

`docx_parser.py` 对每个 `w:p` 按 **Run 出现顺序**扫描：

- 连续的文字 Run 拼成一段 **text buffer**；
- 遇到**绘图** Run 时，把此前尚未「归档」的文字与随后收集到的图片打成一组 **phase**：`(文字, 紧随其后的图片路径列表)`；
- 若**再次出现文字** Run，且此前已有图片，则先 **flush** 上一 phase，再开始新文字。

典型版式「STEP 1 说明 + Figure 行 → 插图 →（换段）STEP 2」下，插图会挂在 **STEP 1 所在 phase** 的末尾，而不会错误归到 STEP 2。用户加的 **`Figure N` 单独成段**有助于人读与核对；解析仍以 **Run 顺序**为准，题注行主要便于人工对照。

### 2. `STEP N:` 行切分与同段多 STEP

同一 `w:p` 内若用**软换行**写了多行，只要某行以 `STEP <数字>:` 开头（忽略大小写），即视为新 STEP 的起点，在**该 phase 的文字**上切分为多个片段，分别进入对应 STEP 的 chunk。

### 3. STEP 与上文「粘」在一起（无换行）

Word 中常见 **`BatterySTEP 1:`**、**`parts.STEP 5:`** 等（`STEP` 前没有换行），按行匹配会失败。实现在分块前调用 **`_ensure_step_newlines`**：在「字母 / 数字 / `.)]}]` 等结尾符」与 `STEP \d+:` 之间**自动插入换行**，再按行做 STEP 切分。

**建议**：在文档里仍尽量用**显式换行**分隔每个 STEP，减少歧义；规范化规则是兜底，不是排版规范。

---

## 输出 JSONL 格式设计

### `raw_paragraphs.jsonl`（解析输出，每行一个段落/表格）

```json
{
  "id": "BatteryChangeSequenceSOP_p042",
  "doc":  "BatteryChangeSequenceSOP",
  "para_idx": 42,
  "type": "paragraph",
  "title": "3.2 换电操作步骤",
  "contents": "确认 AGV 已到达换电位，车体前方 LED 指示灯为绿色常亮。",
  "images": ["images/BatteryChangeSequenceSOP/p042_img0.png"]
}
```

```json
{
  "id": "BatteryChangeSequenceSOP_t003",
  "doc":  "BatteryChangeSequenceSOP",
  "para_idx": 56,
  "type": "table",
  "title": "电池规格参数",
  "contents": "参数 | 数值\n额定电压 | 48V\n额定容量 | 100Ah\n充电截止电压 | 54.6V",
  "images": []
}
```

### `chunks.jsonl`（分块后，供 UltraRAG 向量化）

```json
{
  "id": "BatteryChangeSequenceSOP_c007",
  "title": "3.2 换电操作步骤",
  "contents": "确认 AGV 已到达换电位，车体前方 LED 指示灯为绿色常亮。\n抬起换电机构，缓慢插入电池导槽...",
  "doc": "BatteryChangeSequenceSOP",
  "para_range": [42, 48],
  "images": [
    "images/BatteryChangeSequenceSOP/p042_img0.png",
    "images/BatteryChangeSequenceSOP/p045_img0.png"
  ]
}
```

> `images` 路径相对于 `data/kb/<kb_id>/` 目录；`rag_runner.py` 读取时拼完整路径再转 base64。

---

## `docx_parser.py` 实现逻辑（伪代码）

```python
# custom_app/services/docx_parser.py

class DocxParser:
    """
    输入: DOCX 文件路径、kb_id
    输出:
      data/kb/<kb_id>/images/<doc_stem>/<idx>.png  (导出图片)
      data/kb/<kb_id>/corpora/raw_paragraphs.jsonl  (段落/表格元数据)
    """

    def parse(self, docx_path, kb_id, output_dir):
        doc = Document(docx_path)
        doc_stem = Path(docx_path).stem

        # 导出图片目录
        img_dir = output_dir / "images" / doc_stem
        img_dir.mkdir(parents=True, exist_ok=True)

        # 建立 rId → 图片字节 的映射（从 doc.part.rels 读取）
        image_parts = self._extract_image_parts(doc)

        # 遍历 body 直接子元素（段落和表格都有 para_idx）
        rows = []
        para_idx = 0
        current_heading = ""

        for child in doc.element.body:
            tag = child.tag.split("}")[-1]  # 去命名空间

            if tag == "p":
                paragraph = Paragraph(child, doc)
                text = paragraph.text.strip()

                # 更新当前标题
                if paragraph.style.name.startswith("Heading"):
                    current_heading = text

                # 提取该段落中的内嵌图片
                img_paths = self._export_paragraph_images(
                    child, image_parts, img_dir, doc_stem, para_idx
                )

                if text or img_paths:
                    rows.append({
                        "id":       f"{doc_stem}_p{para_idx:04d}",
                        "doc":      doc_stem,
                        "para_idx": para_idx,
                        "type":     "paragraph",
                        "title":    current_heading,
                        "contents": text,
                        "images":   img_paths,   # 相对于 kb_id 目录的路径
                    })

            elif tag == "tbl":
                table = Table(child, doc)
                table_text = self._table_to_text(table)

                rows.append({
                    "id":       f"{doc_stem}_t{para_idx:04d}",
                    "doc":      doc_stem,
                    "para_idx": para_idx,
                    "type":     "table",
                    "title":    current_heading,
                    "contents": table_text,
                    "images":   [],
                })

            para_idx += 1

        return rows

    def _export_paragraph_images(self, p_elem, image_parts, img_dir, doc_stem, para_idx):
        """
        找到 <w:drawing> → <a:blip r:embed="rId?"> → image_parts[rId]
        保存为 PNG，返回相对路径列表
        """
        paths = []
        NS = {
            "w":  "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "a":  "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r":  "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        blips = p_elem.findall(".//a:blip", NS)
        for i, blip in enumerate(blips):
            r_id = blip.get(f"{{{NS['r']}}}embed")
            if r_id and r_id in image_parts:
                img_bytes = image_parts[r_id]
                filename  = f"p{para_idx:04d}_img{i}.png"
                save_path = img_dir / filename
                save_path.write_bytes(img_bytes)
                # 返回相对于 kb_id 目录的路径
                rel_path  = f"images/{doc_stem}/{filename}"
                paths.append(rel_path)
        return paths

    def _table_to_text(self, table):
        """
        将 Word 表格转换为 Markdown 表格文本
        第一行作为表头，后续行数据，用换行分隔
        """
        lines = []
        for i, row in enumerate(table.rows):
            cells = [c.text.strip() for c in row.cells]
            # 去除重复单元格（合并单元格会重复）
            cells = list(dict.fromkeys(cells))
            lines.append(" | ".join(cells))
            if i == 0:
                lines.append(" | ".join(["---"] * len(cells)))
        return "\n".join(lines)

    def _extract_image_parts(self, doc):
        """从 doc.part.rels 建立 rId → bytes 映射"""
        result = {}
        for rId, rel in doc.part.rels.items():
            if "image" in rel.reltype:
                try:
                    result[rId] = rel.target_part.blob
                except Exception:
                    pass
        return result
```

---

## 章节标题自动识别

DOCX 中标题通常用 Heading 1/2/3 样式，也有用**加粗段落**作为标题的情况。解析时按以下优先级识别：

1. `paragraph.style.name` 为 `Heading 1/2/3` → 直接用文本
2. 段落全文字体加粗、长度 < 50 字 → 作为小节标题
3. 否则继承上一个标题

---

## 分块策略（Phase 1 实际实现）

**以 `STEP N:` 为主边界**：在 intro（首个 `STEP` 之前）与各个 `STEP` 之间切 chunk；表格与当前 intro 或当前 `STEP` 合并。图片按上文 **Run 顺序**归入对应 phase，再写入该 chunk 的 `images` 与 `contents` 末尾的 `[IMAGES]` 块。

不使用 UltraRAG 自带的 `corpus.chunk_documents`（会丢失 `images` 等扩展字段）。输出 `chunks.jsonl` 后直接进入 **Google embed + FAISS index**（见 `03_向量索引构建.md`）。

---

## 命令行使用方式

在项目根目录、已激活 `ultrarag` conda 环境后执行：

```powershell
python -m custom_app.services.docx_parser `
  --input  data/kb/agv_demo/raw `
  --kb-root data/kb/agv_demo
```

默认写出：`data/kb/agv_demo/corpora/chunks.jsonl`，图片目录：`data/kb/agv_demo/images/<doc_stem>/`。

指定输出文件：

```powershell
python -m custom_app.services.docx_parser --input data/kb/agv_demo/raw --kb-root data/kb/agv_demo --output data/kb/agv_demo/corpora/chunks.jsonl
```

---

## 验收标准（T2）

1. `chunks.jsonl` 行数合理（样本文档预估 50-200 条）。  
2. 每条 chunk 的 `images` 字段仅包含**该 chunk 段落范围内**的图片，不会混入其他段落的图。  
3. 表格行被正确转为 Markdown 格式文本，包含在 `contents` 里。  
4. 图片文件能在 `data/kb/agv_demo/images/` 下找到，格式为 PNG，可正常打开。  
5. 人工抽查 3-5 条 chunk，`title` 字段与文档章节标题一致。
