"""Phase 4.4 — SOP DOCX 解析回归脚本（无需启动 Flask）。

对真实 SOP KB 的 raw 目录跑 docx_parser，输出：
    - 每个文档的 chunk 数量
    - chunk 类型分布（intro / step / section）
    - heading_path 命中率
    - 与 Phase 3 chunks.jsonl（如存在）的 ID 集合对比

用法：
    .venv\\Scripts\\python.exe -m custom_app.scripts.verify_sop_regression --kb agv_demo

判定：
    - chunk ID 集合应与现有 chunks.jsonl 100% 一致（Phase 4 不改动 SOP 分块逻辑）
    - 所有 chunk 都应有 source_type / parser / structure 三个新字段
    - STEP chunk 应有 step_number > 0
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from custom_app.services.docx_parser import parse_directory


def _diff_ids(current: set[str], reference: set[str]) -> tuple[set[str], set[str]]:
    """返回 (新增的 ID, 缺失的 ID)。"""
    return current - reference, reference - current


def _count_kinds(chunks: list[dict]) -> Counter:
    """统计 chunk 类型：intro / step / section / other。"""
    counter: Counter = Counter()
    for c in chunks:
        cid = c.get("id", "")
        if "_step_" in cid:
            counter["step"] += 1
        elif cid.endswith("_intro"):
            counter["intro"] += 1
        elif "_section_" in cid:
            counter["section"] += 1
        else:
            counter["other"] += 1
    return counter


def _check_new_schema(chunks: list[dict]) -> list[str]:
    """检查所有 chunk 是否有新 schema 字段，返回问题清单。"""
    issues: list[str] = []
    for c in chunks:
        cid = c.get("id", "<no-id>")
        if c.get("source_type") != "sop_docx":
            issues.append(f"{cid}: source_type != 'sop_docx' (got {c.get('source_type')!r})")
        if c.get("parser") != "docx_parser":
            issues.append(f"{cid}: parser != 'docx_parser' (got {c.get('parser')!r})")
        struct = c.get("structure")
        if not isinstance(struct, dict):
            issues.append(f"{cid}: structure missing or not a dict")
            continue
        for key in ("heading_path", "heading_level", "step_number", "page_idx"):
            if key not in struct:
                issues.append(f"{cid}: structure.{key} missing")
        # STEP chunk 必须有 step_number > 0
        if "_step_" in cid:
            sn = struct.get("step_number")
            if not (isinstance(sn, int) and sn > 0):
                issues.append(f"{cid}: STEP chunk should have step_number > 0, got {sn!r}")
        else:
            # 非 STEP chunk step_number 必须为 None
            if struct.get("step_number") is not None:
                issues.append(f"{cid}: non-STEP chunk should have step_number=None")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 SOP DOCX 解析回归")
    parser.add_argument("--kb", required=True, help="KB id（如 agv_demo / ifs_docs）")
    parser.add_argument(
        "--reference",
        default="",
        help="参考 chunks.jsonl 路径（默认: data/kb/<kb>/corpora/chunks.jsonl）",
    )
    args = parser.parse_args()

    kb_root = Path(f"data/kb/{args.kb}")
    raw_dir = kb_root / "raw"
    if not raw_dir.exists():
        print(f"ERROR: raw dir missing: {raw_dir}", file=sys.stderr)
        return 2
    docx_files = sorted(raw_dir.glob("*.docx"))
    if not docx_files:
        print(f"ERROR: no .docx under {raw_dir}", file=sys.stderr)
        return 2

    print(f"=== Phase 4 SOP 回归 — kb={args.kb} ===")
    print(f"raw dir: {raw_dir}")
    print(f"docx files: {len(docx_files)}")
    for fp in docx_files:
        print(f"  - {fp.name}")

    # 解析
    print("\n[1/3] 跑 docx_parser.parse_directory()...")
    chunks = parse_directory(raw_dir, kb_root)
    print(f"  生成 chunks: {len(chunks)}")

    # 类型分布
    kinds = _count_kinds(chunks)
    print(f"  chunk 类型分布: {dict(kinds)}")

    # heading_path 命中
    with_heading = sum(
        1 for c in chunks if c.get("structure", {}).get("heading_path")
    )
    print(f"  含 heading_path 的 chunk: {with_heading}/{len(chunks)}")

    # 新 schema 字段检查
    print("\n[2/3] 新 schema 字段检查...")
    issues = _check_new_schema(chunks)
    if issues:
        print(f"  发现 {len(issues)} 个问题：")
        for issue in issues[:20]:
            print(f"    - {issue}")
        if len(issues) > 20:
            print(f"    ... 还有 {len(issues) - 20} 项")
    else:
        print("  [OK] 所有 chunk 字段完整")

    # 与参考 chunks.jsonl 对比（如存在）
    ref_path = Path(args.reference) if args.reference else kb_root / "corpora" / "chunks.jsonl"
    print(f"\n[3/3] 与 {ref_path} 对比...")
    diff_status = "skipped (no reference)"
    if ref_path.exists():
        ref_chunks = [
            json.loads(line)
            for line in ref_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        current_ids = {c.get("id", "") for c in chunks}
        ref_ids = {c.get("id", "") for c in ref_chunks}
        added, missing = _diff_ids(current_ids, ref_ids)
        print(f"  参考 chunks: {len(ref_chunks)}")
        print(f"  当前 chunks: {len(chunks)}")
        if not added and not missing:
            print("  [OK] chunk ID 集合 100% 一致（零回归）")
            diff_status = "OK"
        else:
            print(f"  [WARN] 新增 ID: {len(added)}")
            for cid in sorted(added)[:10]:
                print(f"    + {cid}")
            print(f"  [WARN] 缺失 ID: {len(missing)}")
            for cid in sorted(missing)[:10]:
                print(f"    - {cid}")
            diff_status = "DIFF"
    else:
        print("  (跳过：参考文件不存在)")

    # 总结
    print("\n=== 总结 ===")
    summary = {
        "kb": args.kb,
        "docx_count": len(docx_files),
        "chunk_count": len(chunks),
        "kinds": dict(kinds),
        "with_heading_path": with_heading,
        "schema_issues": len(issues),
        "diff_vs_reference": diff_status,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 退出码
    if issues:
        print("\n[WARN] schema 字段问题：FAIL")
        return 1
    if diff_status == "DIFF":
        print("\n[WARN] chunks 集合与参考不一致：可能需要重新嵌入索引")
        return 1
    print("\n[OK] Phase 4 SOP 回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
