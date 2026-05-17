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

# Phase 8.0 兜底滑窗切分参数（针对无 STEP、无 Heading 的结构松散文档）
SLIDING_WINDOW_THRESHOLD_CHARS = 800  # 整篇字符数 < 阈值 → 仍走 _full，单 chunk
SLIDING_WINDOW_SIZE_CHARS = 800  # 单 chunk 目标字符数
SLIDING_WINDOW_OVERLAP_CHARS = 100  # 相邻 chunk 重叠字符数（仅文本，不复制图片）


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


def _sliding_window_chunks(
    parts: List[str],
    imgs_per_part: List[List[str]],
    *,
    size: int = SLIDING_WINDOW_SIZE_CHARS,
    overlap: int = SLIDING_WINDOW_OVERLAP_CHARS,
) -> List[tuple[List[str], List[str]]]:
    """
    Phase 8.0 兜底滑窗切分：按段落边界 + 字符长度切分，不在段落中间截断。

    输入：
        parts: 段落文本数组（已 strip，可能为空字符串占位以与 imgs_per_part 对齐）
        imgs_per_part: 每段对应的图片相对路径列表（与 parts 等长）
        size: 单 chunk 目标字符数（默认 800）
        overlap: 相邻 chunk 重叠字符数（默认 100；仅文本，图片不重复）

    返回：
        [(chunk_text_lines, chunk_img_paths), ...]
        - chunk_text_lines: 该 chunk 的段落文本数组（不含 [IMG: ...] 占位，由 pack_chunk 注入）
        - chunk_img_paths: 该 chunk 归属的图片相对路径列表（去重在 pack_chunk 中处理）

    设计要点：
        - overlap 仅复制尾部段落文本到下个 buffer，不复制图片（避免同图召回两次）
        - 单段超过 size 时仍整段保留，不切碎（牺牲均匀性换语义完整）
        - parts 与 imgs_per_part 必须长度一致（调用方负责）
    """
    if len(parts) != len(imgs_per_part):
        raise ValueError(
            f"parts ({len(parts)}) and imgs_per_part ({len(imgs_per_part)}) must align"
        )

    out: List[tuple[List[str], List[str]]] = []
    buf_lines: List[str] = []
    buf_imgs: List[str] = []
    buf_len = 0

    for line, line_imgs in zip(parts, imgs_per_part):
        line_len = len(line)
        # 触发 flush 的条件：buffer 非空，且加上当前 part 后超出目标尺寸
        if buf_lines and buf_len + line_len > size:
            out.append((buf_lines[:], buf_imgs[:]))
            # 计算 overlap 尾部段落（只复制文本，不复制图片）
            # 规则：尾段单段长度必须 ≤ overlap 才会被复制；超大段不进入 tail，避免重复
            tail: List[str] = []
            tail_len = 0
            for prev in reversed(buf_lines):
                if len(prev) > overlap:
                    break  # 单段就比 overlap 还长 → 不复制，避免重复
                if tail_len + len(prev) > overlap and tail:
                    break
                tail.insert(0, prev)
                tail_len += len(prev)
            buf_lines = tail
            buf_imgs = []
            buf_len = tail_len
        buf_lines.append(line)
        buf_imgs.extend(line_imgs)
        buf_len += line_len

    if buf_lines or buf_imgs:
        out.append((buf_lines, buf_imgs))

    return out


_IMG_PLACEHOLDER_RE = re.compile(r"^\[IMG:\s*([^\]]+)\]$")


def _split_intro_for_windows(
    intro_lines: List[str], intro_imgs: List[str]
) -> tuple[List[str], List[List[str]]]:
    """
    Phase 8.0 辅助：把 intro_lines（已混入 [IMG: path] 占位行）拆成滑窗输入：
        parts: 纯文本段落数组（去掉占位行）
        imgs_per_part: 每段对应的图片路径数组（占位行的图片归到**前一段**）

    若占位行出现在所有文本段之前，则归到第一段（如果有）；否则归到一个空 part。
    intro_imgs 仅作健壮性检查：返回的所有图片路径之和应等于 intro_imgs。
    """
    parts: List[str] = []
    imgs_per_part: List[List[str]] = []
    pending_imgs: List[str] = []

    for line in intro_lines:
        m = _IMG_PLACEHOLDER_RE.match(line.strip()) if line else None
        if m:
            pending_imgs.append(m.group(1).strip())
            continue
        # 普通文本段：消耗 pending_imgs（归到上一段；若无上一段，则与本段并列）
        if pending_imgs and parts:
            imgs_per_part[-1].extend(pending_imgs)
            pending_imgs = []
        parts.append(line)
        imgs_per_part.append([])
        if pending_imgs:
            # 首段之前的孤儿图片，挂在第一段上
            imgs_per_part[-1].extend(pending_imgs)
            pending_imgs = []

    # 尾部还有未消化的图片 → 挂到最后一段；无段则新建空 part
    if pending_imgs:
        if parts:
            imgs_per_part[-1].extend(pending_imgs)
        else:
            parts.append("")
            imgs_per_part.append(pending_imgs)

    # 健壮性：若 intro_imgs 提供，所有归属图片应是它的子集（顺序可能不同）
    if intro_imgs:
        flat = [p for ips in imgs_per_part for p in ips]
        # 不做严格相等校验（intro_imgs 已去重逻辑由 pack_chunk 负责）
        _ = flat  # 仅作为隐式契约保留
    return parts, imgs_per_part


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
        # 有 heading 切分时，最后一节用 section_N；纯无标题文档需按字符阈值路由
        if heading_chunk_idx[0] > 0:
            heading_chunk_idx[0] += 1
            final_id = f"{doc_stem}_section_{heading_chunk_idx[0]}"
            pack_chunk(final_id, h_run or doc_title_fallback, intro_lines, intro_imgs)
        else:
            # Phase 8.0 兜底路径：无 STEP 无 Heading 文档
            # - 短文档（< SLIDING_WINDOW_THRESHOLD_CHARS）→ 沿用 `_intro` 单 chunk（向后兼容现有 KB）
            # - 长文档（≥ 阈值）→ 滑窗切多块 `_window_N`
            total_chars = sum(len(ln) for ln in intro_lines)
            if total_chars >= SLIDING_WINDOW_THRESHOLD_CHARS:
                parts, imgs_per_part = _split_intro_for_windows(intro_lines, intro_imgs)
                windows = _sliding_window_chunks(parts, imgs_per_part)
                for idx, (chunk_lines, chunk_imgs) in enumerate(windows, start=1):
                    non_empty = [ln for ln in chunk_lines if ln]
                    pack_chunk(
                        f"{doc_stem}_window_{idx}",
                        h_run or doc_title_fallback,
                        non_empty,
                        chunk_imgs,
                    )
            else:
                pack_chunk(
                    f"{doc_stem}_intro",
                    h_run or doc_title_fallback,
                    intro_lines,
                    intro_imgs,
                )
    else:
        pack_chunk(
            f"{doc_stem}_step_{cur_step}",
            f"{h_run or doc_title_fallback} | STEP {cur_step}",
            cur_lines,
            cur_imgs,
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
