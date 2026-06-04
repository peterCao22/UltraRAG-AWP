"""Phase 9.1 一次性回填脚本：给现有 chunks.jsonl 的所有图片生成 VLM caption。

设计：
    - 扫描指定 KB 的 chunks.jsonl
    - 对每个 chunk 的 images，把字符串路径升级为含 caption_zh/caption_en/entities 的 dict
    - **幂等**：images[i] 已是 dict 且含非空 caption_zh 时跳过
    - 失败不阻塞整体：单图失败仍写入，但 caption_zh="" 并标记 reason
    - dry-run：扫描 + 估算调用次数，不真的调 Gemini
    - 备份：写入前生成 chunks.jsonl.bak.<ts>

用法：
    # dry-run，看会调多少次 + 估算成本
    python -m custom_app.scripts.backfill_image_captions --kb agv_demo --dry-run

    # 真实执行
    python -m custom_app.scripts.backfill_image_captions --kb agv_demo

    # 仅处理第一个 N 张（试运行）
    python -m custom_app.scripts.backfill_image_captions --kb agv_demo --limit 5

退出码：
    0  成功 / dry-run 完成
    1  内部错误（文件缺失 / 写入失败）
    2  使用错误
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# 必须最早 load_dotenv，因为脚本不经过 Flask app.py 入口，
# 否则 ULTRARAG_DB_BACKEND / GOOGLE_API_KEY 等环境变量都读不到
from dotenv import load_dotenv
load_dotenv()

_logger = logging.getLogger(__name__)


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"chunks file not found: {path}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_chunks(chunks: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in chunks:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _needs_caption(item: Any) -> bool:
    """判断 images[i] 是否需要回填。

    - str 路径 → 是
    - dict 且 caption_zh 为空 → 是
    - dict 且 caption_zh 非空 → 否（已回填过，幂等跳过）
    """
    if isinstance(item, str):
        return True
    if isinstance(item, dict):
        return not str(item.get("caption_zh") or "").strip()
    return False


def _build_chunk_context(chunk: dict[str, Any], max_chars: int = 1200) -> str:
    """同 chunk 的标题 + contents 文本作为 VLM 上下文。"""
    parts: list[str] = []
    title = str(chunk.get("title") or "").strip()
    if title:
        parts.append(f"[Title] {title}")
    contents = str(chunk.get("contents") or "").strip()
    if contents:
        parts.append(contents)
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


def _collect_image_jobs(
    chunks: list[dict[str, Any]],
) -> list[tuple[int, int, str]]:
    """枚举所有需要回填的图片：返回 [(chunk_idx, image_idx, path), ...]。"""
    jobs: list[tuple[int, int, str]] = []
    for ci, chunk in enumerate(chunks):
        images = chunk.get("images") or []
        if not isinstance(images, list):
            continue
        for ii, item in enumerate(images):
            if not _needs_caption(item):
                continue
            if isinstance(item, str):
                path = item
            elif isinstance(item, dict):
                path = str(item.get("path") or "").strip()
            else:
                continue
            if not path:
                continue
            jobs.append((ci, ii, path))
    return jobs


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="KB ID（对应 data/kb/<kb> 目录）")
    p.add_argument(
        "--chunks-path", type=Path, default=None,
        help="自定义 chunks.jsonl 路径（默认 data/kb/<kb>/corpora/chunks.jsonl）",
    )
    p.add_argument(
        "--kb-root", type=Path, default=None,
        help="KB 根目录，用于解析图片相对路径（默认 data/kb/<kb>）",
    )
    p.add_argument("--dry-run", action="store_true", help="只扫描不调用")
    p.add_argument(
        "--limit", type=int, default=0,
        help="仅处理前 N 张图（试运行；0 = 全部）",
    )
    args = p.parse_args(argv)

    chunks_path = args.chunks_path or Path(f"data/kb/{args.kb}/corpora/chunks.jsonl")
    kb_root = args.kb_root or Path(f"data/kb/{args.kb}")

    try:
        chunks = _load_chunks(chunks_path)
    except FileNotFoundError as e:
        _logger.error("%s", e)
        return 1

    print(f"=== Phase 9.1 图片 caption 回填 — kb={args.kb} ===")
    print(f"chunks file: {chunks_path}")
    print(f"kb root: {kb_root}")
    print(f"chunks total: {len(chunks)}")

    jobs = _collect_image_jobs(chunks)
    print(f"images needing caption: {len(jobs)}")

    if args.limit > 0:
        jobs = jobs[: args.limit]
        print(f"--limit={args.limit} → 实际处理 {len(jobs)} 张")

    if not jobs:
        print("\n所有图片均已有 caption（或无图）。无需操作。")
        return 0

    if args.dry_run:
        # 简单估算：~$0.002 / 张（含图像输入 + 双语 caption + 实体输出）
        est_cost = len(jobs) * 0.002
        print(f"\n[dry-run] 不执行。预估 Gemini 成本 ~${est_cost:.3f}")
        print("去掉 --dry-run 即真实执行（会备份原文件）。")
        return 0

    # 备份
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = chunks_path.with_suffix(f".jsonl.bak.{ts}")
    shutil.copy2(chunks_path, backup_path)
    print(f"\n已备份 → {backup_path}")

    # 延迟 import：避免 dry-run 也要装 jinja2
    from custom_app.services.parsers.image_describer import describe_image

    print(f"\n开始回填 {len(jobs)} 张图片...")
    n_ok = 0
    n_fail = 0
    total_ms = 0
    for k, (ci, ii, path) in enumerate(jobs, start=1):
        chunk_ctx = _build_chunk_context(chunks[ci])
        result = describe_image(
            path, chunk_context=chunk_ctx, kb_root=kb_root,
        )
        total_ms += result.ms
        # 升级 images[ii]：字符串 → dict
        original_item = chunks[ci].get("images", [])[ii]
        if isinstance(original_item, str):
            new_item: dict[str, Any] = {"path": original_item}
        elif isinstance(original_item, dict):
            new_item = dict(original_item)
        else:
            new_item = {"path": path}

        new_item["caption_zh"] = result.caption_zh
        new_item["caption_en"] = result.caption_en
        new_item["entities"] = result.entities
        if result.failed:
            new_item["_describe_failed"] = True
            new_item["_describe_reason"] = result.reason or ""
            # 保留 raw_text 用于排查（成功时不存，避免 jsonl 膨胀）
            if result.raw_text:
                new_item["_describe_raw"] = result.raw_text[:500]
            n_fail += 1
        else:
            # 清除可能的旧失败标记
            new_item.pop("_describe_failed", None)
            new_item.pop("_describe_reason", None)
            n_ok += 1

        chunks[ci]["images"][ii] = new_item

        # 进度
        if k % 5 == 0 or k == len(jobs):
            print(
                f"  [{k}/{len(jobs)}] ok={n_ok} fail={n_fail} "
                f"avg_ms={int(total_ms / k)}"
            )

    # 写回
    _write_chunks(chunks, chunks_path)
    print(f"\n已写回 {chunks_path}")
    print(f"成功: {n_ok}，失败: {n_fail}，总耗时: {total_ms / 1000:.1f}s")
    if n_fail:
        print(f"\n[WARN] {n_fail} 张失败的图片仍写入了 _describe_failed=true，")
        print("       你可以稍后重跑本脚本（幂等），失败的会重试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
