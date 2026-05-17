"""Phase 8.1 评测集 JSONL IO。

写入约定：
    - 每行一条 EvalItem.to_dict() 的 JSON
    - utf-8 编码、ensure_ascii=False（保留中文）
    - 末尾换行，便于 git diff
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

from .schema import EvalItem, validate_kb_homogeneous, validate_unique_ids


def iter_eval_items(path: Path) -> Iterator[EvalItem]:
    """惰性读取 JSONL 评测集。空行跳过，解析失败抛 ValueError 并带行号。"""
    if not path.exists():
        raise FileNotFoundError(f"eval dataset not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            s = raw.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno} invalid JSON: {e}") from e
            try:
                yield EvalItem.from_dict(row)
            except ValueError as e:
                raise ValueError(f"{path}:{lineno} {e}") from e


def load_eval_dataset(
    path: Path,
    *,
    expected_kb_id: str | None = None,
) -> list[EvalItem]:
    """加载评测集并做整体校验。"""
    items = list(iter_eval_items(path))
    if not items:
        raise ValueError(f"{path}: dataset is empty")

    dups = validate_unique_ids(items)
    if dups:
        raise ValueError(f"{path}: duplicate ids: {dups[:5]}")

    kbs = validate_kb_homogeneous(items)
    if len(kbs) != 1:
        raise ValueError(f"{path}: dataset spans multiple kb_id values: {sorted(kbs)}")

    if expected_kb_id is not None:
        only_kb = next(iter(kbs))
        if only_kb != expected_kb_id:
            raise ValueError(
                f"{path}: expected kb_id={expected_kb_id!r}, got {only_kb!r}"
            )

    return items


def write_eval_dataset(items: Iterable[EvalItem], path: Path) -> int:
    """写 JSONL；返回写入行数。父目录自动创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
            n += 1
    return n
