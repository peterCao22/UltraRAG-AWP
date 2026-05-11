"""Phase 4.4 — general KB 端到端烟囱脚本（无需 Flask + 无需重型依赖）。

在临时目录里模拟一个 general KB 的完整入库前半段：
    准备 raw/*.md → 跑 parse_files 工厂 → 写 chunks.jsonl → 验证字段

不调用嵌入和 FAISS（那需要 GOOGLE_API_KEY），仅验证 Phase 4.1+4.2 的解析/路由层。

用法：
    .venv\\Scripts\\python.exe -m custom_app.scripts.verify_general_kb_smoke
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from custom_app.services.parsers import KB_TYPE_GENERAL, parse_files


SAMPLE_DOCS: dict[str, str] = {
    "故障手册.md": (
        "# 第3章 故障处理\n"
        "本章描述常见告警的处理流程。\n\n"
        "## 3.1 电池告警\n"
        "当电池电压低于 22V 时触发告警。\n\n"
        "### 3.1.1 低电量\n"
        "立即停止任务并切换备用电池。\n\n"
        "## 3.2 通信告警\n"
        "检查 WiFi 信号强度。\n"
    ),
    "维护指南.md": (
        "# 日常维护\n"
        "每周检查电池触点和导轮磨损。\n\n"
        "# 月度保养\n"
        "更换液压油，校准导航激光雷达。\n"
    ),
    "readme.txt": (
        "这是一个纯文本文件，没有 Markdown 标题。\n"
        "应该作为一个整体 chunk 处理。\n"
    ),
}


def main() -> int:
    print("=== Phase 4 general KB 烟囱测试 ===")
    print(f"将创建临时 KB，包含 {len(SAMPLE_DOCS)} 个样本文件")

    with tempfile.TemporaryDirectory(prefix="phase4_smoke_") as tmp:
        kb_root = Path(tmp) / "data" / "kb" / "smoke_general"
        raw_dir = kb_root / "raw"
        raw_dir.mkdir(parents=True)
        corpora_dir = kb_root / "corpora"
        corpora_dir.mkdir(parents=True)

        # 1. 准备样本文件
        print("\n[1/4] 准备样本文件...")
        for name, content in SAMPLE_DOCS.items():
            (raw_dir / name).write_text(content, encoding="utf-8")
            print(f"  - {name}")

        # 2. 跑 parser 工厂
        print("\n[2/4] 调用 parsers.parse_files()...")
        files = sorted(raw_dir.iterdir())
        chunks = parse_files(
            KB_TYPE_GENERAL,
            files,
            kb_root,
            kb_id="smoke_general",
        )
        print(f"  生成 {len(chunks)} 个 chunk")

        # 3. 写 chunks.jsonl
        print("\n[3/4] 写入 chunks.jsonl...")
        chunks_path = corpora_dir / "chunks.jsonl"
        chunks_path.write_text(
            "\n".join(
                json.dumps(c.to_jsonl_dict(), ensure_ascii=False) for c in chunks
            ),
            encoding="utf-8",
        )
        size_kb = chunks_path.stat().st_size / 1024
        print(f"  {chunks_path.name} ({size_kb:.2f} KB)")

        # 4. 字段断言 + 类型分布
        print("\n[4/4] 字段验证...")
        issues: list[str] = []
        parser_counter: dict[str, int] = {}
        source_counter: dict[str, int] = {}
        heading_path_count = 0

        for c in chunks:
            d = c.to_jsonl_dict()

            # 必填字段
            for f in ("id", "title", "contents", "doc", "kb_id",
                      "source_type", "parser", "structure"):
                if f not in d:
                    issues.append(f"{c.id}: missing field {f}")

            if d.get("kb_id") != "smoke_general":
                issues.append(f"{c.id}: kb_id not injected by factory")

            parser_counter[d["parser"]] = parser_counter.get(d["parser"], 0) + 1
            source_counter[d["source_type"]] = source_counter.get(d["source_type"], 0) + 1

            if d["structure"]["heading_path"]:
                heading_path_count += 1

        print(f"  parser 分布: {parser_counter}")
        print(f"  source_type 分布: {source_counter}")
        print(f"  含 heading_path 的 chunk: {heading_path_count}/{len(chunks)}")

        # 验证 heading_path 嵌入文本
        from custom_app.services.google_embedder import compose_doc_embedding_text

        sample_with_heading = next(
            (c for c in chunks if c.structure.heading_path), None
        )
        if sample_with_heading is not None:
            text = compose_doc_embedding_text(sample_with_heading.to_jsonl_dict())
            heading_str = " > ".join(sample_with_heading.structure.heading_path)
            if heading_str in text:
                print(f"  [OK] heading_path 前缀正确进入嵌入文本: {heading_str!r}")
            else:
                issues.append(f"heading_path 前缀缺失: expected {heading_str!r} in text")

        print("\n=== 总结 ===")
        summary = {
            "raw_files": len(SAMPLE_DOCS),
            "chunks_generated": len(chunks),
            "parser_counts": parser_counter,
            "source_type_counts": source_counter,
            "with_heading_path": heading_path_count,
            "schema_issues": len(issues),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        if issues:
            print(f"\n[WARN] {len(issues)} 个问题:")
            for issue in issues:
                print(f"  - {issue}")
            return 1

        print("\n[OK] Phase 4 general KB 烟囱测试通过")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
