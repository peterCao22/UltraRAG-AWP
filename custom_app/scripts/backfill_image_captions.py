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

    def _apply_result(ci: int, ii: int, path: str, result) -> bool:
        """把 describe_image 结果合并到 chunks[ci].images[ii]，返回是否 failed。"""
        original_item = chunks[ci].get("images", [])[ii]
        if isinstance(original_item, str):
            new_item: dict[str, Any] = {"path": original_item}
        elif isinstance(original_item, dict):
            new_item = dict(original_item)
        else:
            new_item = {"path": path}

        new_item["caption_zh"] = result.caption_zh
        new_item["caption_en"] = result.caption_en
        new_item["entities"] = list(result.entities)
        if result.failed:
            new_item["_describe_failed"] = True
            new_item["_describe_reason"] = result.reason or ""
            if result.raw_text:
                new_item["_describe_raw"] = result.raw_text[:500]
        else:
            # 清除可能的旧失败标记
            new_item.pop("_describe_failed", None)
            new_item.pop("_describe_reason", None)
            new_item.pop("_describe_raw", None)

        chunks[ci]["images"][ii] = new_item
        return result.failed

    # ── Pass 1：正常顺序跑 ───────────────────────────────────────
    print(f"\n[Pass 1] 顺序跑 {len(jobs)} 张图片...")
    n_ok = 0
    failed_jobs: list[tuple[int, int, str]] = []  # 失败的图，pass 2 重试用
    total_ms = 0
    for k, (ci, ii, path) in enumerate(jobs, start=1):
        chunk_ctx = _build_chunk_context(chunks[ci])
        result = describe_image(path, chunk_context=chunk_ctx, kb_root=kb_root)
        total_ms += result.ms
        if _apply_result(ci, ii, path, result):
            failed_jobs.append((ci, ii, path))
        else:
            n_ok += 1
        # 每张写完立即增量写文件（防止脚本崩溃丢全部成果）
        _write_chunks(chunks, chunks_path)
        if k % 5 == 0 or k == len(jobs):
            print(
                f"  [{k}/{len(jobs)}] ok={n_ok} fail={len(failed_jobs)} "
                f"avg_ms={int(total_ms / k)}"
            )

    # ── Pass 2：等待 + 慢节流重试失败的（同模型）─────────────────
    pass2_recovered = 0
    pass2_still_failed: list[tuple[int, int, str]] = []
    if failed_jobs:
        cool_down_sec = 30
        per_image_sleep_sec = 3
        print(
            f"\n[Pass 2] {len(failed_jobs)} 张失败，冷却 {cool_down_sec}s "
            f"让 Gemini 节流缓存退出，然后慢节流重试..."
        )
        time.sleep(cool_down_sec)
        for k, (ci, ii, path) in enumerate(failed_jobs, start=1):
            chunk_ctx = _build_chunk_context(chunks[ci])
            result = describe_image(path, chunk_context=chunk_ctx, kb_root=kb_root)
            recovered = not result.failed
            _apply_result(ci, ii, path, result)
            if recovered:
                pass2_recovered += 1
                n_ok += 1
            else:
                pass2_still_failed.append((ci, ii, path))
            print(
                f"  [{k}/{len(failed_jobs)}] {'OK' if recovered else 'STILL FAIL'} "
                f"{path}"
            )
            _write_chunks(chunks, chunks_path)
            if k < len(failed_jobs):
                time.sleep(per_image_sleep_sec)

    # ── Pass 3：仍失败的用 fallback model（更稳定的 gemini-2.5-pro）──
    pass3_recovered = 0
    if pass2_still_failed:
        import os as _os
        fallback_model = (
            _os.environ.get("ULTRARAG_IMAGE_DESCRIBE_FALLBACK_MODEL")
            or "gemini-2.5-pro"
        ).strip()
        print(
            f"\n[Pass 3] {len(pass2_still_failed)} 张仍失败，"
            f"切换到 fallback model={fallback_model} 重试..."
        )
        for k, (ci, ii, path) in enumerate(pass2_still_failed, start=1):
            chunk_ctx = _build_chunk_context(chunks[ci])
            result = describe_image(
                path, chunk_context=chunk_ctx, kb_root=kb_root,
                model=fallback_model,
            )
            recovered = not result.failed
            _apply_result(ci, ii, path, result)
            if recovered:
                pass3_recovered += 1
                n_ok += 1
            print(
                f"  [{k}/{len(pass2_still_failed)}] {'OK' if recovered else 'STILL FAIL'} "
                f"{path}"
            )
            _write_chunks(chunks, chunks_path)
            if k < len(pass2_still_failed):
                time.sleep(2)

    final_fail = len(failed_jobs) - pass2_recovered - pass3_recovered
    print(f"\n已写回 {chunks_path}")
    print(
        f"成功: {n_ok}（含 Pass 2 救回 {pass2_recovered}，"
        f"Pass 3 fallback 救回 {pass3_recovered}），失败: {final_fail}"
    )
    if final_fail:
        print(
            f"\n[WARN] {final_fail} 张仍失败的图片已标 _describe_failed=true"
        )
        print("       可以再跑一次本脚本（幂等），失败的会再重试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
