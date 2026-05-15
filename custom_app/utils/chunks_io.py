"""Phase 6.2: chunks.jsonl 的细粒度读写工具。

为什么需要：单文件重建 / 单文件删除时只能动这一文档的行，不能整文件重建。

注意 schema：chunk 字典里没有 `doc_id`，只有 `doc`（= doc_stem，文件名去扩展）。
本模块统一按 doc_stem 匹配。doc_id 形如 "<kb_id>:<file_name>"，调用方负责把
doc_id → file_name → stem 的转换；本模块只认 doc_stem。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


def iter_chunks(chunks_path: Path) -> Iterable[dict]:
    """流式读 chunks.jsonl。空文件/不存在视为空。"""
    if not chunks_path.exists():
        return
    with chunks_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def collect_chunk_ids_for_doc(chunks_path: Path, doc_stem: str) -> list[str]:
    """列出该 doc 在 chunks.jsonl 中的所有 chunk id（删向量库时用）。"""
    out: list[str] = []
    for row in iter_chunks(chunks_path):
        if row.get("doc") == doc_stem:
            cid = row.get("id")
            if cid:
                out.append(str(cid))
    return out


def remove_doc_from_chunks(chunks_path: Path, doc_stem: str) -> int:
    """过滤掉该 doc 的所有行，原子覆写。返回删除的 chunk 数。

    实现：写 tmp 再 rename，避免半成品。
    """
    if not chunks_path.exists():
        return 0
    tmp = chunks_path.with_suffix(chunks_path.suffix + ".tmp")
    removed = 0
    with chunks_path.open("r", encoding="utf-8") as src, tmp.open(
        "w", encoding="utf-8"
    ) as dst:
        for raw in src:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                # 坏行直接丢——chunks.jsonl 是派生数据，下次 ingest 会重新生成
                continue
            if row.get("doc") == doc_stem:
                removed += 1
                continue
            dst.write(raw if raw.endswith("\n") else raw + "\n")
    os.replace(tmp, chunks_path)
    return removed


def append_chunks(chunks_path: Path, new_rows: list[dict]) -> int:
    """追加写若干 chunk 行。返回追加的条数。

    单文件重建场景：先 remove_doc_from_chunks(doc_stem)，再 append_chunks(new_rows)。
    """
    if not new_rows:
        return 0
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    with chunks_path.open("a", encoding="utf-8") as f:
        for row in new_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(new_rows)


def doc_id_to_stem(doc_id: str) -> str:
    """`<kb_id>:<file_name.ext>` → `<file_name>`（去扩展名）。

    与 _register_documents / _attribute_chunk_counts 的命名规则保持一致。
    """
    file_name = doc_id.split(":", 1)[1] if ":" in doc_id else doc_id
    return Path(file_name).stem
