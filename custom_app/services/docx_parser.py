"""
Parse AGV SOP .docx files into chunks.jsonl for Phase 1 RAG.

Chunking:
  - Split on paragraph lines matching ``STEP <n>:`` (case-insensitive).
  - Content before the first STEP becomes one ``intro`` chunk.
  - Tables attach to the current intro or STEP section.
  - Embedded images export to ``<kb_root>/images/<doc_stem>/img_NNNN.ext``.
  - ``Heading 1`` / ``Heading 2`` / … paragraphs update the title prefix for
    following chunks (document title context).

Each JSONL row: id, title, contents, doc, images (list of paths relative to kb_root).
``contents`` may end with ``\\n[IMAGES]\\n`` + one relative path per line so that
FAISS retriever returns passages that still reference files. Strip this suffix
before embedding (see ``google_embedder.text_for_embedding``).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

STEP_RE = re.compile(r"^\s*STEP\s+(\d+)\s*:", re.IGNORECASE)

IMAGES_MARK = "\n[IMAGES]\n"


def _ensure_step_newlines(text: str) -> str:
    """
    Word often omits line breaks before ``STEP N:`` (e.g. ``BatterySTEP 1:`` or
    ``parts.STEP 5:``). Insert a newline so line-based STEP detection works.
    """
    if not text:
        return text
    return re.sub(
        r"([a-zA-Z0-9.);}\]])(\s*)(STEP\s+\d+\s*:)",
        r"\1\2\n\3",
        text,
        flags=re.IGNORECASE,
    )


def _split_paragraph_by_steps(text: str) -> List[tuple[Optional[int], str]]:
    """
    One Word paragraph may contain multiple STEP lines (soft line breaks).
    Returns ordered (step_num_or_none, segment_text) with non-empty segments.
    """
    if not (text or "").strip():
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pieces: List[tuple[Optional[int], List[str]]] = []
    cur_step: Optional[int] = None
    buf: List[str] = []
    for line in lines:
        m = STEP_RE.match(line)
        if m:
            if buf:
                joined = "\n".join(buf).strip()
                if joined:
                    pieces.append((cur_step, joined))
                buf = []
            cur_step = int(m.group(1))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        joined = "\n".join(buf).strip()
        if joined:
            pieces.append((cur_step, joined))
    return [(s, t) for s, t in pieces]


def _guess_image_ext(blob: bytes) -> str:
    if len(blob) >= 8 and blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(blob) >= 2 and blob[:2] == b"\xff\xd8":
        return "jpg"
    if len(blob) >= 6 and blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "bin"


def _extract_image_blobs(doc: DocumentObject) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for rel in doc.part.rels.values():
        try:
            if "image" not in rel.reltype:
                continue
            out[rel.rId] = rel.target_part.blob
        except Exception:
            continue
    return out


def _paragraph_blip_rids(p_element) -> List[str]:
    rids: List[str] = []
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    for blip in p_element.findall(f".//{{{ns}}}blip"):
        rid = blip.get(qn("r:embed"))
        if rid:
            rids.append(rid)
    return rids


def _blip_rids_in_run(w_r_element) -> List[str]:
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rids: List[str] = []
    for blip in w_r_element.findall(f".//{{{ns}}}blip"):
        rid = blip.get(qn("r:embed"))
        if rid:
            rids.append(rid)
    return rids


def _run_plain_text(w_r_element) -> str:
    ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    parts: List[str] = []
    for t in w_r_element.findall(f".//{{{ns_w}}}t"):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


def _paragraph_text_image_phases(
    p_element,
    blobs: Dict[str, bytes],
    img_dir: Path,
    doc_stem: str,
    counter_holder: List[int],
) -> List[tuple[str, List[str]]]:
    """
    Follow Word run order: each phase is (text_blob, image_paths_that_follow_this_text).
    When new text appears after images, flush (previous_text, collected_images).
    """
    ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    phases: List[tuple[str, List[str]]] = []
    text_buf: List[str] = []
    img_paths: List[str] = []

    def save_rid(rid: str) -> Optional[str]:
        blob = blobs.get(rid)
        if not blob:
            return None
        counter_holder[0] += 1
        ext = _guess_image_ext(blob)
        fname = f"img_{counter_holder[0]:04d}.{ext}"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / fname).write_bytes(blob)
        return f"images/{doc_stem}/{fname}"

    for child in p_element:
        if child.tag != qn("w:r"):
            continue
        rids = _blip_rids_in_run(child)
        txt = _run_plain_text(child)
        if txt:
            if text_buf and img_paths:
                phases.append(("".join(text_buf), img_paths[:]))
                text_buf, img_paths = [], []
            text_buf.append(txt)
        for rid in rids:
            pth = save_rid(rid)
            if pth:
                img_paths.append(pth)
    if text_buf or img_paths:
        phases.append(("".join(text_buf), img_paths[:]))
    return [(t.strip(), ims) for t, ims in phases if t.strip() or ims]


def _table_to_text(table: Table) -> str:
    lines: List[str] = []
    for row in table.rows:
        cells = [c.text.strip() for c in row.cells]
        cells = list(dict.fromkeys(cells))
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def _paragraph_heading_label(p: Paragraph) -> Optional[str]:
    """
    判断段落是否应作为「节标题」：
    1. Word 内置 Heading 样式（Heading 1/2/3 …）
    2. 段落所有有内容的 Run 均加粗 —— 用于 IFS 等非标准 Heading 的 FAQ 型文档
    """
    name = (p.style and p.style.name) or ""
    t = (p.text or "").strip()
    if not t:
        return None
    if name.startswith("Heading"):
        return t
    # 全加粗短段落（非代码块）视为节标题
    runs = p.runs
    if runs and all(r.bold for r in runs if r.text.strip()):
        # 排除明显是代码块的行（以 ``` 开头或全为命令/符号）
        if not t.startswith("```") and len(t) <= 120:
            return t
    return None


def parse_docx(docx_path: Path, kb_root: Path) -> List[Dict[str, Any]]:
    doc_stem = docx_path.stem
    img_dir = kb_root / "images" / doc_stem
    img_dir.mkdir(parents=True, exist_ok=True)

    doc = Document(str(docx_path))
    blobs = _extract_image_blobs(doc)

    doc_title_fallback = doc_stem.replace("_", " ")
    h_run: Optional[str] = None
    intro_lines: List[str] = []
    intro_imgs: List[str] = []
    cur_step: Optional[int] = None
    cur_lines: List[str] = []
    cur_imgs: List[str] = []
    chunks_out: List[Dict[str, Any]] = []
    img_counter = 0
    counter_holder = [0]

    def pack_chunk(cid: str, title: str, lines: List[str], imgs: List[str]) -> None:
        # lines 是有序的字符串混合：普通文本行 / "[IMG: <相对路径>]" 内联占位
        # imgs 是去重后的图片路径数组（用于 chunk["images"] 兼容字段）
        body = "\n".join(x for x in lines if x).strip()
        uimgs = list(dict.fromkeys(imgs))
        if not body and not uimgs:
            return
        # 兜底：如果 lines 里没有 [IMG: ...] 占位（旧代码路径或表格内图片），
        # 把缺失的图片路径追加到末尾，保证 LLM 仍能看到（向后兼容）
        present = set(re.findall(r"\[IMG:\s*([^\]]+)\]", body))
        missing = [p for p in uimgs if p not in present]
        if missing:
            tail = "\n".join(f"[IMG: {p}]" for p in missing)
            body = (body + "\n" + tail).strip()

        # Phase 4 新 schema 字段（向后兼容版本）：
        # - images 仍输出字符串数组：现有下游 (api/chat.py, services/tools/list_chunks.py)
        #   假定字符串路径，保留兼容；新 parser (mineru/docling) 可直接输出对象数组，
        #   Chunk.from_jsonl_dict 已能兼容两种格式
        # - heading_path：当前 docx_parser 只维护单层运行标题 h_run
        # - step_number：仅 STEP chunk 有值（intro/section chunk 为 None）
        is_step_chunk = cur_step is not None and cid == f"{doc_stem}_step_{cur_step}"
        step_number = cur_step if is_step_chunk else None
        heading_path = [h_run] if h_run else []

        chunks_out.append(
            {
                "id": cid,
                "title": title,
                "contents": body,
                "doc": doc_stem,
                "images": uimgs,
                "source_type": "sop_docx",
                "parser": "docx_parser",
                "structure": {
                    "heading_path": heading_path,
                    "heading_level": 1 if heading_path else 0,
                    "step_number": step_number,
                    "page_idx": None,
                },
                "tables": [],
                "vector_id": None,
            }
        )

    # 标题切分计数器：无 STEP 文档遇到新标题时用此 ID 生成独立 chunk
    heading_chunk_idx = [0]

    def flush_for_new_heading() -> None:
        """
        无 STEP 文档遇到新 Heading 时，将当前积累内容打包为独立 chunk。
        含 STEP 的文档不受影响（cur_step 不为 None 时直接返回）。
        """
        nonlocal intro_lines, intro_imgs
        if cur_step is not None:
            return  # STEP 文档：不按标题切分
        if not (intro_lines or intro_imgs):
            return  # 尚无积累内容，无需 flush
        heading_chunk_idx[0] += 1
        cid = f"{doc_stem}_section_{heading_chunk_idx[0]}"
        pack_chunk(cid, h_run or doc_title_fallback, intro_lines, intro_imgs)
        intro_lines, intro_imgs = [], []

    def flush_for_new_step(new_step: int) -> None:
        nonlocal cur_step, cur_lines, cur_imgs, intro_lines, intro_imgs
        if cur_step is None:
            pack_chunk(
                f"{doc_stem}_intro",
                h_run or doc_title_fallback,
                intro_lines,
                intro_imgs,
            )
            intro_lines, intro_imgs = [], []
        else:
            pack_chunk(
                f"{doc_stem}_step_{cur_step}",
                f"{h_run or doc_title_fallback} | STEP {cur_step}",
                cur_lines,
                cur_imgs,
            )
            cur_lines, cur_imgs = [], []
        cur_step = new_step

    for child in doc.element.body:
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            lab = _paragraph_heading_label(p)
            if lab:
                # 有 STEP 的文档：标题只更新运行标题，不切 chunk
                # 无 STEP 的文档（FAQ/汇编型）：先 flush 上一节内容，再切换标题
                flush_for_new_heading()
                h_run = lab
            counter_holder[0] = img_counter
            phases = _paragraph_text_image_phases(
                p._element, blobs, img_dir, doc_stem, counter_holder
            )
            img_counter = counter_holder[0]

            for phase_text, phase_imgs in phases:
                # 把图片同时加到 lines 数组（作为 [IMG: path] 占位行，保留位置）
                # 和 imgs 数组（用于 chunk["images"] 兼容字段）
                def _append_imgs(target_lines: List[str], target_imgs: List[str], paths: List[str]) -> None:
                    for p in paths:
                        target_lines.append(f"[IMG: {p}]")
                        target_imgs.append(p)

                pieces = _split_paragraph_by_steps(
                    _ensure_step_newlines(phase_text.strip())
                )
                if not pieces:
                    if phase_imgs:
                        if cur_step is None:
                            _append_imgs(intro_lines, intro_imgs, phase_imgs)
                        else:
                            _append_imgs(cur_lines, cur_imgs, phase_imgs)
                    continue
                n_pieces = len(pieces)
                for idx, (piece_step, segment) in enumerate(pieces):
                    piece_imgs = list(phase_imgs) if idx == n_pieces - 1 else []
                    if piece_step is None:
                        if cur_step is None:
                            intro_lines.append(segment)
                            _append_imgs(intro_lines, intro_imgs, piece_imgs)
                        else:
                            cur_lines.append(segment)
                            _append_imgs(cur_lines, cur_imgs, piece_imgs)
                        continue
                    if cur_step != piece_step:
                        if cur_step is None and not intro_lines and not intro_imgs:
                            cur_step = piece_step
                            cur_lines.append(segment)
                            _append_imgs(cur_lines, cur_imgs, piece_imgs)
                        elif cur_step is None:
                            flush_for_new_step(piece_step)
                            cur_lines.append(segment)
                            _append_imgs(cur_lines, cur_imgs, piece_imgs)
                        else:
                            flush_for_new_step(piece_step)
                            cur_lines.append(segment)
                            _append_imgs(cur_lines, cur_imgs, piece_imgs)
                    else:
                        cur_lines.append(segment)
                        _append_imgs(cur_lines, cur_imgs, piece_imgs)
        elif child.tag == qn("w:tbl"):
            tt = _table_to_text(Table(child, doc))
            if not tt:
                continue
            if cur_step is None:
                intro_lines.append(tt)
            else:
                cur_lines.append(tt)

    if cur_step is None:
        # 有 heading 切分时，最后一节用 section_N；纯无标题文档沿用 _intro
        if heading_chunk_idx[0] > 0:
            heading_chunk_idx[0] += 1
            final_id = f"{doc_stem}_section_{heading_chunk_idx[0]}"
        else:
            final_id = f"{doc_stem}_intro"
        pack_chunk(final_id, h_run or doc_title_fallback, intro_lines, intro_imgs)
    else:
        pack_chunk(
            f"{doc_stem}_step_{cur_step}",
            f"{h_run or doc_title_fallback} | STEP {cur_step}",
            cur_lines,
            cur_imgs,
        )

    # No STEP anywhere: keep a single chunk if pack_chunk produced nothing
    if not chunks_out:
        lines = []
        imgs: List[str] = []
        counter_holder[0] = 0
        h_run = None
        for child in doc.element.body:
            if child.tag == qn("w:p"):
                p = Paragraph(child, doc)
                lab = _paragraph_heading_label(p)
                if lab:
                    h_run = lab
                for phase_text, phase_imgs in _paragraph_text_image_phases(
                    p._element, blobs, img_dir, doc_stem, counter_holder
                ):
                    if phase_text.strip():
                        lines.append(phase_text.strip())
                    imgs.extend(phase_imgs)
            elif child.tag == qn("w:tbl"):
                tt = _table_to_text(Table(child, doc))
                if tt:
                    lines.append(tt)
        pack_chunk(
            f"{doc_stem}_full",
            h_run or doc_title_fallback,
            lines,
            imgs,
        )

    return chunks_out


def parse_directory(raw_dir: Path, kb_root: Path) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for docx in sorted(raw_dir.glob("*.docx")):
        chunks.extend(parse_docx(docx, kb_root))
    return chunks


def write_chunks_jsonl(chunks: List[Dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in chunks:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse SOP .docx files into chunks.jsonl")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/kb/agv_demo/raw"),
        help="Directory containing .docx files",
    )
    parser.add_argument(
        "--kb-root",
        type=Path,
        default=Path("data/kb/agv_demo"),
        help="Knowledge base root (images/, corpora/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSONL (default: <kb-root>/corpora/chunks.jsonl)",
    )
    args = parser.parse_args()
    out = args.output or (args.kb_root / "corpora" / "chunks.jsonl")
    chunks = parse_directory(args.input, args.kb_root)
    write_chunks_jsonl(chunks, out)
    print(f"Wrote {len(chunks)} chunks -> {out}")


if __name__ == "__main__":
    main()
