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
import os
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

# Phase 11.2 细粒度切块（借鉴 WeKnora splitter）
# 现状：旧版 section 整段不切 → ifs_docs 单 chunk 高达 887 字，子项被埋
# 改造：所有 section/step/intro 内容 > CHUNK_TARGET_SIZE 时按递归分隔符二次切分
# 调参：可通过 env ULTRARAG_CHUNK_TARGET_SIZE / ULTRARAG_CHUNK_OVERLAP 覆盖
CHUNK_TARGET_SIZE = int(os.environ.get("ULTRARAG_CHUNK_TARGET_SIZE", "400"))
CHUNK_OVERLAP_SIZE = int(os.environ.get("ULTRARAG_CHUNK_OVERLAP", "80"))

# 递归分隔符（按优先级降级；每个分隔符切完不达 size 才用下一级）
# 中文优先句号 / 问号 / 感叹号 / 分号 / 逗号 / 顿号，最后兜底空格 / 字符
_SPLIT_SEPARATORS = ["\n\n", "\n", "。", "？", "！", "；", "，", "、", " "]

# 保护正则：以下模式整体保留（不被切碎），借鉴 WeKnora protected_regex
# 注意：模式越具体优先级越高
_PROTECTED_PATTERNS = [
    re.compile(r"\[IMG:\s*[^\]]+\]"),  # docx_parser 内联图片占位
    re.compile(r"!\[[^\]]*\]\([^)]+\)"),  # markdown 图片
    re.compile(r"\[[^\]]+\]\([^)]+\)"),  # markdown 链接
    re.compile(r"```[\s\S]*?```"),  # 代码块
    # markdown 表格行（含分隔行）
    re.compile(r"^[ ]*(?:\|[^|\n]*)+\|\s*$", re.MULTILINE),
]

# 旧常量名保留向后兼容（仅 _sliding_window_chunks 用，已废弃）
SLIDING_WINDOW_THRESHOLD_CHARS = CHUNK_TARGET_SIZE  # >= 阈值才切多 chunk
SLIDING_WINDOW_SIZE_CHARS = CHUNK_TARGET_SIZE
SLIDING_WINDOW_OVERLAP_CHARS = CHUNK_OVERLAP_SIZE


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


def _split_text_recursive(
    text: str,
    *,
    target_size: int = CHUNK_TARGET_SIZE,
    separators: Optional[List[str]] = None,
) -> List[str]:
    """递归分隔符切分（借鉴 WeKnora splitter._split + protected 保护）。

    策略：先扫描 _PROTECTED_PATTERNS（图片/表格/代码块）整段占位，再对普通文本
    按 separators 顺序降级切分；最后还原 protected 段。Protected 段超 target_size
    时仍整段保留（牺牲均匀性换语义完整，否则 markdown 表格切断会渲染错乱）。

    Args:
        text: 待切文本（可能含 [IMG: ...] 占位 / markdown / 表格）
        target_size: 单段目标字符数
        separators: 分隔符优先级序列（None 用默认 _SPLIT_SEPARATORS）

    Returns:
        splits: 按 size 切好的段落数组，拼接后 == text
    """
    if separators is None:
        separators = _SPLIT_SEPARATORS

    if not text:
        return []
    if len(text) <= target_size:
        return [text]

    # Step 1: 找出所有 protected 区间 [(start, end)]，按 start 升序、互不重叠
    spans: List[tuple[int, int]] = []
    for pat in _PROTECTED_PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    spans.sort(key=lambda x: (x[0], -x[1]))
    # 去重叠：后一个 span 如果在前一个内部就丢弃
    merged: List[tuple[int, int]] = []
    for s, e in spans:
        if merged and s < merged[-1][1]:
            continue
        merged.append((s, e))

    # Step 2: 把 text 切成 "普通段 + protected 段 + 普通段 + ..." 序列
    segments: List[tuple[str, bool]] = []  # (text_piece, is_protected)
    cursor = 0
    for s, e in merged:
        if cursor < s:
            segments.append((text[cursor:s], False))
        segments.append((text[s:e], True))
        cursor = e
    if cursor < len(text):
        segments.append((text[cursor:], False))

    # Step 3: 对每个 segment：protected 整段保留；普通段按分隔符递归切
    out: List[str] = []
    for piece, is_prot in segments:
        if is_prot or len(piece) <= target_size:
            out.append(piece)
        else:
            out.extend(_split_plain_text(piece, target_size=target_size, separators=separators))
    return out


def _split_plain_text(
    text: str,
    *,
    target_size: int,
    separators: List[str],
) -> List[str]:
    """对纯文本（无 protected）按 separators 顺序降级切分。"""
    if len(text) <= target_size:
        return [text] if text else []

    # 找第一个能把 text 切成 ≥2 段的分隔符
    chosen_sep = None
    for sep in separators:
        if sep and sep in text:
            chosen_sep = sep
            break

    if chosen_sep is None:
        # 所有分隔符都不在 text 里，硬切字符
        return [text[i : i + target_size] for i in range(0, len(text), target_size)]

    # 按 chosen_sep 切分，保留分隔符在尾部（便于拼接还原）
    parts = text.split(chosen_sep)
    splits_with_sep: List[str] = []
    for i, p in enumerate(parts):
        if i < len(parts) - 1:
            splits_with_sep.append(p + chosen_sep)
        elif p:
            splits_with_sep.append(p)

    # 对单段仍超 size 的，用下一级分隔符递归
    out: List[str] = []
    next_seps = separators[separators.index(chosen_sep) + 1:]
    for s in splits_with_sep:
        if len(s) <= target_size:
            out.append(s)
        else:
            out.extend(_split_plain_text(s, target_size=target_size, separators=next_seps))
    return out


def _split_lines_to_chunks(
    parts: List[str],
    imgs_per_part: List[List[str]],
    *,
    target_size: int = CHUNK_TARGET_SIZE,
    overlap: int = CHUNK_OVERLAP_SIZE,
) -> List[tuple[List[str], List[str]]]:
    """Phase 11.2 细粒度切块（替代旧 _sliding_window_chunks）。

    与旧版差异：
      - 旧版按段落硬切；新版用递归分隔符切到 target_size 以下，避免段落过大
      - target_size 从 800 → 400（默认）；overlap 100 → 80
      - protected_patterns 保护 [IMG: ...] / markdown 图链 / 表格行 / 代码块

    输入：
        parts: 段落文本数组（不含 [IMG] 占位；图片单独在 imgs_per_part）
        imgs_per_part: 每段对应的图片相对路径列表（与 parts 等长）
        target_size: 单 chunk 目标字符数
        overlap: 相邻 chunk 重叠字符数（仅文本，图片不重复以免召回两次）

    返回：
        [(chunk_text_lines, chunk_img_paths), ...]
        chunk_text_lines 中可包含按图片顺序穿插的 "[IMG: path]" 占位行
        chunk_img_paths 是去重后的图片相对路径列表
    """
    if len(parts) != len(imgs_per_part):
        raise ValueError(
            f"parts ({len(parts)}) and imgs_per_part ({len(imgs_per_part)}) must align"
        )

    # 先把 parts + imgs_per_part 还原成"段落 + 图片占位"交错的序列（一维 splits）
    # 这样可以让 _split_text_recursive 一次切完，图片占位作为 protected 模式保留
    flat_blocks: List[str] = []  # 文本块 or "[IMG: path]" 占位
    for p, ips in zip(parts, imgs_per_part):
        if p:
            flat_blocks.append(p)
        for img in ips:
            flat_blocks.append(f"[IMG: {img}]")

    if not flat_blocks:
        return []

    # 把 flat_blocks 用 "\n" 拼成大文本，递归切到 target_size 以下
    full_text = "\n".join(flat_blocks)
    splits = _split_text_recursive(full_text, target_size=target_size)

    # 把切好的 splits 合并成 chunks（带 overlap）
    out: List[tuple[List[str], List[str]]] = []
    buf_text = ""
    for s in splits:
        if buf_text and len(buf_text) + len(s) > target_size:
            # flush 当前 chunk
            out.append(_chunk_from_text(buf_text))
            # 计算 overlap：从 buf 末尾保留 ≤ overlap 字符（按句末切，不破句）
            tail = _take_overlap_tail(buf_text, overlap)
            buf_text = tail + s
        else:
            buf_text += s

    if buf_text:
        out.append(_chunk_from_text(buf_text))

    return out


def _chunk_from_text(text: str) -> tuple[List[str], List[str]]:
    """把切好的纯文本（含 [IMG: ...] 占位行）转回 (lines, imgs) 元组。

    lines: 按 "\n" 切分；保留 [IMG: ...] 行（pack_chunk 不会重复加）
    imgs: 从文本中抽取所有 [IMG: ...] 路径，去重保序
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    imgs: List[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\[IMG:\s*([^\]]+)\]", text):
        p = m.group(1).strip()
        if p and p not in seen:
            seen.add(p)
            imgs.append(p)
    return lines, imgs


def _take_overlap_tail(text: str, overlap: int) -> str:
    """从 text 末尾取约 overlap 字符的尾部，尽量在句末切（不破句）。

    优先在 [。！？\n] 后断；找不到就硬切。返回的尾部用于拼到下个 chunk 开头。
    Overlap 不复制图片占位（避免图片重复展示）。
    """
    if not text or overlap <= 0:
        return ""
    tail = text[-overlap:] if len(text) > overlap else text
    # 找尾部第一个句末标点之后的位置作为切点
    for sep in ("。", "！", "？", "\n"):
        idx = tail.find(sep)
        if idx >= 0 and idx < len(tail) - 1:
            tail = tail[idx + len(sep):]
            break
    # 去掉 overlap 里的 [IMG: ...] 占位（避免下个 chunk 重复显示同图）
    tail = re.sub(r"\[IMG:[^\]]+\]\n?", "", tail).strip()
    return tail + ("\n" if tail and not tail.endswith("\n") else "")


def _sliding_window_chunks(
    parts: List[str],
    imgs_per_part: List[List[str]],
    *,
    size: int = SLIDING_WINDOW_SIZE_CHARS,
    overlap: int = SLIDING_WINDOW_OVERLAP_CHARS,
) -> List[tuple[List[str], List[str]]]:
    """Phase 11.2 起：转调新的 _split_lines_to_chunks（递归分隔符 + protected）。

    旧实现按段落硬切、单段超 size 也整段保留 → ifs_docs 单 chunk 高达 887 字。
    新实现把同一段内的 ID-NN 子项也切到 target_size 以下，"计划人"等小子项
    才能独立成 chunk 被 LLM 精准引用。

    保留旧函数名，是因为 docx_parser 内部"无 STEP 无 Heading 兜底路径"
    仍用此函数；改造范围限定在它的内部实现。
    """
    return _split_lines_to_chunks(parts, imgs_per_part, target_size=size, overlap=overlap)


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

    def _emit_chunk(cid: str, title: str, body: str, uimgs: List[str]) -> None:
        """实际向 chunks_out 追加一条记录（不再二次切分）。"""
        if not body and not uimgs:
            return
        # 兜底：如果 body 没含 [IMG: ...] 占位（旧代码路径或表格内图片），
        # 把缺失的图片路径追加到末尾，保证 LLM 仍能看到（向后兼容）
        present = set(re.findall(r"\[IMG:\s*([^\]]+)\]", body))
        missing = [p for p in uimgs if p not in present]
        if missing:
            tail = "\n".join(f"[IMG: {p}]" for p in missing)
            body = (body + "\n" + tail).strip()

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

    def pack_chunk(cid: str, title: str, lines: List[str], imgs: List[str]) -> None:
        """Phase 11.2: 若 body 超过 CHUNK_TARGET_SIZE，按递归分隔符切成 _part_N。

        - body 中已包含按位置穿插的 [IMG: ...] 占位行（来自 lines）
        - body 较小时（≤ target_size）按旧行为生成单 chunk，cid 不加 _part_N
        - body 过大时调用 _split_lines_to_chunks 切多份；每份用 cid + "_part_{N}"
        """
        body = "\n".join(x for x in lines if x).strip()
        uimgs = list(dict.fromkeys(imgs))
        if not body and not uimgs:
            return

        # 短内容：维持原行为（不加 _part_N 后缀，保持评测集 chunk_id 不变的边界）
        if len(body) <= CHUNK_TARGET_SIZE:
            _emit_chunk(cid, title, body, uimgs)
            return

        # 长内容：用 _split_lines_to_chunks 二次切分
        # 输入：把 lines 重新拆分成 (parts, imgs_per_part) — 但 lines 已含 [IMG:]
        # 占位，所以这里走 _split_intro_for_windows 的逆向：分文本/图片即可
        parts, imgs_per_part = _split_intro_for_windows(lines, uimgs)
        windows = _split_lines_to_chunks(
            parts, imgs_per_part, target_size=CHUNK_TARGET_SIZE, overlap=CHUNK_OVERLAP_SIZE,
        )
        if len(windows) <= 1:
            # 切分没产生多段（特殊情况，比如全是 protected content）→ 仍走单 chunk
            _emit_chunk(cid, title, body, uimgs)
            return

        for idx, (sub_lines, sub_imgs) in enumerate(windows, start=1):
            sub_body = "\n".join(x for x in sub_lines if x).strip()
            if not sub_body and not sub_imgs:
                continue
            sub_cid = f"{cid}_part_{idx}"
            _emit_chunk(sub_cid, title, sub_body, sub_imgs)

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

    # 注入 prev_chunk_id / next_chunk_id（doc 内邻居链）
    # 单 doc 调用场景下也保证邻居字段齐全；parse_directory 末尾会再跑一次（幂等）
    link_neighbors_in_place(chunks_out)
    return chunks_out


def link_neighbors_in_place(chunks: List[Dict[str, Any]]) -> None:
    """为 chunks 列表注入 prev_chunk_id / next_chunk_id 字段（doc 内串链）。

    设计要点（对齐 WeKnora merge_expand.go 的邻居模型）：
        - 按 ``doc`` 字段分组，每组内按出现顺序两两相连
        - 首/尾 chunk 对应字段为空字符串（语义：无邻居）
        - 严格 doc 边界：跨 doc 不连接（防御性，避免 Layer 1 邻居扩展拉到无关 chunk）
        - 缺 doc 字段的 chunk 单独成组（自身视为孤立 doc）

    参数:
        chunks: 由 ``parse_docx`` 或 ``parse_directory`` 产出的 chunk 列表，原地修改。
    """
    if not chunks:
        return

    # 按 doc 分组保持出现顺序（dict 保序）
    by_doc: Dict[str, List[Dict[str, Any]]] = {}
    for ch in chunks:
        doc_key = str(ch.get("doc") or "")
        by_doc.setdefault(doc_key, []).append(ch)

    for group in by_doc.values():
        n = len(group)
        for idx, ch in enumerate(group):
            prev_id = str(group[idx - 1].get("id", "")) if idx > 0 else ""
            next_id = str(group[idx + 1].get("id", "")) if idx < n - 1 else ""
            ch["prev_chunk_id"] = prev_id
            ch["next_chunk_id"] = next_id


def parse_directory(raw_dir: Path, kb_root: Path) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for docx in sorted(raw_dir.glob("*.docx")):
        # 跳过 Word 临时锁文件（"~$xxx.docx"），它们是 Office 打开时生成的小文件
        if docx.name.startswith("~$"):
            continue
        chunks.extend(parse_docx(docx, kb_root))
    link_neighbors_in_place(chunks)
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
