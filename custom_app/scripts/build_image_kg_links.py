"""Phase 9.3.A 一次性脚本：把 Phase 9.1 图片数据建进 Neo4j KG。

数据流：
    chunks.jsonl 中每条 chunk 的 images[] 字段（dict 含 caption / entities）
        ↓
    1. 对每张含 caption 的图 upsert (:Image {kb_id, img_id, path, ...})
        - img_id = sha1(kb_id::path)[:12]，稳定可重跑（幂等）
        - 失败图（_describe_failed=True）跳过
    2. 对图的每个 entity_name，过滤通用词（出现 chunk_ids > 5 的实体跳过）
    3. 仅当 KG 已存在同名 (kb_id, name) Entity 时，建 (:Image)-[:MENTIONS]->(:Entity)

幂等性：
    - upsert 用 MERGE，重跑只更新 caption / chunk_id
    - link MERGE 关系，重跑不重复建边
    - --reset 选项：先删该 KB 的所有 Image 节点，再重建

用法：
    # dry-run，看会建多少节点 / 多少边 / 多少被过滤
    python -m custom_app.scripts.build_image_kg_links --kb agv_demo --dry-run

    # 实际写入
    python -m custom_app.scripts.build_image_kg_links --kb agv_demo
    python -m custom_app.scripts.build_image_kg_links --kb ifs_docs

    # 推倒重建
    python -m custom_app.scripts.build_image_kg_links --kb agv_demo --reset

env：
    ULTRARAG_NEO4J_URI / USER / PASSWORD / DATABASE
    ULTRARAG_IMAGE_KG_MAX_CHUNK_IDS_PER_ENTITY    默认 5（实体出现在多少
                                                   chunk 算"过于通用"，超过则
                                                   不允许图片连接，避免噪声）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_logger = logging.getLogger(__name__)

DEFAULT_MAX_CHUNK_IDS_PER_ENTITY = 5
# 包含匹配的图实体最小长度：太短（"AGV"、"ID"）容易和大量 KG 实体匹配，
# 反成噪声。中文 2 字符 / 英文 4 字符以上才参与包含匹配。
DEFAULT_MIN_IMG_ENTITY_LEN_FOR_FUZZY = 2


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


def _make_img_id(kb_id: str, path: str) -> str:
    """稳定 img_id：sha1(kb_id::path)[:12]。同图重跑得到同 id。"""
    h = hashlib.sha1(f"{kb_id}::{path}".encode("utf-8")).hexdigest()
    return h[:12]


def _normalize_for_match(s: str) -> str:
    """匹配前归一化：去空白 + 小写。

    保留中文不变；英文转小写让大小写差异（"AGV"/"agv"）不影响匹配。
    """
    return "".join(s.split()).lower()


def _resolve_entity_to_kg_names(
    img_entity: str,
    kg_entity_names: list[str],
    *,
    min_fuzzy_len: int = DEFAULT_MIN_IMG_ENTITY_LEN_FOR_FUZZY,
) -> list[str]:
    """把一个图实体扩展成可建 MENTIONS 的 KG 实体名列表。

    匹配规则（按优先级）：
        1. 完全匹配（归一化后相等）→ 返回该 KG 实体
        2. 包含匹配（图实体 ⊆ KG 实体 或 KG 实体 ⊆ 图实体）→ 全部返回
        3. 图实体过短（< min_fuzzy_len）跳过包含匹配，避免噪声

    去重 + 保序：同一个 KG 实体不会被返回两次。

    Args:
        img_entity:      图片实体名（VLM 抽取的，如"控制面板"、"AGV"）
        kg_entity_names: KG 中该 KB 的所有实体名列表
        min_fuzzy_len:   触发包含匹配的最小图实体长度（归一化后）

    Returns:
        匹配到的 KG 实体名列表（保留 KG 中的原始大小写 / 表达）；按出现
        顺序去重。
    """
    img_norm = _normalize_for_match(img_entity)
    if not img_norm:
        return []

    out: list[str] = []
    seen: set[str] = set()

    # 1. 完全匹配优先
    for kg_name in kg_entity_names:
        kg_norm = _normalize_for_match(kg_name)
        if kg_norm == img_norm and kg_name not in seen:
            out.append(kg_name)
            seen.add(kg_name)

    if out:
        return out

    # 2. 太短的图实体不参与包含匹配（避免"AGV"匹配所有含 AGV 的实体）
    if len(img_norm) < min_fuzzy_len:
        return []

    # 3. 包含匹配：双向
    for kg_name in kg_entity_names:
        if kg_name in seen:
            continue
        kg_norm = _normalize_for_match(kg_name)
        if not kg_norm:
            continue
        # 任一方向包含
        if img_norm in kg_norm or kg_norm in img_norm:
            out.append(kg_name)
            seen.add(kg_name)
    return out


def _load_chunks(chunks_path: Path) -> list[dict]:
    if not chunks_path.exists():
        raise FileNotFoundError(f"chunks file not found: {chunks_path}")
    return [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="KB ID（如 agv_demo / ifs_docs）")
    p.add_argument("--dry-run", action="store_true",
                  help="只扫描统计，不写 Neo4j")
    p.add_argument("--reset", action="store_true",
                  help="先删该 KB 所有 :Image 节点，再重建")
    args = p.parse_args(argv)

    chunks_path = Path(f"data/kb/{args.kb}/corpora/chunks.jsonl")
    try:
        chunks = _load_chunks(chunks_path)
    except FileNotFoundError as e:
        _logger.error("%s", e)
        return 1

    print(f"=== Phase 9.3.A image-KG build — kb={args.kb} ===")
    print(f"chunks file: {chunks_path}")
    print(f"chunks total: {len(chunks)}")

    # ── 第 1 步：扫所有有 caption 的图，构造 image_jobs ───────────────
    # image_jobs: [(img_id, path, doc, chunk_id, caption_zh, caption_en, entities[])]
    image_jobs: list[tuple] = []
    n_failed_skipped = 0
    n_no_caption_skipped = 0
    n_legacy_string_skipped = 0
    for c in chunks:
        chunk_id = str(c.get("id") or "")
        doc = str(c.get("doc") or "")
        for img in c.get("images") or []:
            if isinstance(img, str):
                n_legacy_string_skipped += 1
                continue
            if not isinstance(img, dict):
                continue
            if img.get("_describe_failed"):
                n_failed_skipped += 1
                continue
            path = str(img.get("path") or "").strip()
            caption_zh = str(img.get("caption_zh") or "").strip()
            caption_en = str(img.get("caption_en") or "").strip()
            if not path or (not caption_zh and not caption_en):
                n_no_caption_skipped += 1
                continue
            entities_raw = img.get("entities") or []
            entities = [str(e).strip() for e in entities_raw if str(e).strip()]
            img_id = _make_img_id(args.kb, path)
            image_jobs.append((
                img_id, path, doc, chunk_id, caption_zh, caption_en, entities,
            ))

    print(f"\nimages found:")
    print(f"  with caption (will build): {len(image_jobs)}")
    print(f"  failed (skip):             {n_failed_skipped}")
    print(f"  no caption / no path:      {n_no_caption_skipped}")
    print(f"  legacy string format:      {n_legacy_string_skipped}")

    if not image_jobs:
        print("\n无图可建。退出。")
        return 0

    # ── 第 2 步：连 Neo4j，加载实体过滤表 ──────────────────────────
    from custom_app.services.kgstore import build_kg_store
    store = build_kg_store()

    max_chunk_ids = _env_int(
        "ULTRARAG_IMAGE_KG_MAX_CHUNK_IDS_PER_ENTITY",
        DEFAULT_MAX_CHUNK_IDS_PER_ENTITY,
    )
    print(f"\nentity chunk_ids threshold: ≤ {max_chunk_ids} (大于此值的实体视为通用词，不建 MENTIONS)")

    entity_chunk_counts = store.list_entity_chunk_id_counts(args.kb)
    print(f"KG entities for kb={args.kb}: {len(entity_chunk_counts)}")
    if not entity_chunk_counts:
        print("\n[WARN] KG 没有该 KB 的实体，无法建 MENTIONS 关系。")
        print("       请先跑 ingest job 让 kg_extractor 生成实体。")
        # 仍允许只建 Image 节点（即使没 MENTIONS，9.3.B 也不会查到东西）
        # 但这种状态没价值，提示后退出
        return 1

    # ── 第 3 步：dry-run 统计 ──────────────────────────────────────
    # 使用包含匹配 resolver：1 个图实体 → N 个 KG 实体
    kg_entity_names_list = list(entity_chunk_counts.keys())
    eligible_links: int = 0
    skipped_entity_no_match = 0
    skipped_kg_entity_too_generic = 0
    total_entity_attempts = 0
    sample_links: list[str] = []
    # 在 dry-run 阶段先把每个 img-job 的"图实体 → KG 实体集合"算好缓存，
    # 真实写入段直接复用避免重算
    resolved_per_job: list[list[tuple[str, str, int]]] = []
    # 每个元素：[(img_entity, kg_entity_name, chunk_count), ...]
    for img_id, path, doc, chunk_id, zh, en, entities in image_jobs:
        rows: list[tuple[str, str, int]] = []
        for img_ent in entities:
            total_entity_attempts += 1
            kg_names = _resolve_entity_to_kg_names(img_ent, kg_entity_names_list)
            if not kg_names:
                skipped_entity_no_match += 1
                continue
            for kg_name in kg_names:
                cnt = entity_chunk_counts.get(kg_name, 0)
                if cnt > max_chunk_ids:
                    skipped_kg_entity_too_generic += 1
                    continue
                rows.append((img_ent, kg_name, cnt))
                eligible_links += 1
                if len(sample_links) < 10:
                    arrow = "=" if img_ent == kg_name else "≈"
                    sample_links.append(
                        f"  img({img_id} {path[:35]}) "
                        f"-[:MENTIONS]-> ({kg_name}) {arrow} "
                        f"img_ent='{img_ent}' [in {cnt} chunks]"
                    )
        resolved_per_job.append(rows)

    print(f"\nentity matching summary:")
    print(f"  total img entity attempts:        {total_entity_attempts}")
    print(f"  eligible MENTIONS (will build):   {eligible_links}")
    print(f"  skipped: no KG match (exact/fuzzy): {skipped_entity_no_match}")
    print(f"  skipped: KG entity too generic ( > {max_chunk_ids} chunks): {skipped_kg_entity_too_generic}")
    if sample_links:
        print(f"\nsample MENTIONS (前 10 条；'=' 完全匹配 / '≈' 包含匹配):")
        for s in sample_links:
            print(s)

    if args.dry_run:
        print("\n[dry-run] 不写 Neo4j。去掉 --dry-run 真实执行。")
        return 0

    # ── 第 4 步：reset（可选）────────────────────────────────────
    if args.reset:
        print(f"\n[reset] 清空 kb={args.kb} 的 Image 节点 + MENTIONS 关系...")
        n_removed = store.delete_images_for_kb(args.kb)
        print(f"  已删除 {n_removed} 个 Image 节点")

    # ── 第 5 步：写入 Image 节点 + MENTIONS 关系 ───────────────────
    # 直接复用 dry-run 段算好的 resolved_per_job，避免对每个图实体再算一次
    print(f"\n开始写入 {len(image_jobs)} 张图...")
    n_images_upserted = 0
    n_mentions_created = 0
    ts = _now_iso()
    for i, ((img_id, path, doc, chunk_id, zh, en, _ents), rows) in enumerate(
        zip(image_jobs, resolved_per_job), start=1,
    ):
        store.upsert_image_node(
            kb_id=args.kb, img_id=img_id, path=path, doc=doc,
            chunk_id=chunk_id, caption_zh=zh, caption_en=en,
            created_at=ts,
        )
        n_images_upserted += 1
        # rows 已经按"eligible MENTIONS（含包含匹配 + 通用词过滤）"准备好
        for _img_ent, kg_name, _cnt in rows:
            ok = store.link_image_to_entity(
                kb_id=args.kb, img_id=img_id, entity_name=kg_name,
                created_at=ts,
            )
            if ok:
                n_mentions_created += 1
        if i % 10 == 0 or i == len(image_jobs):
            print(f"  [{i}/{len(image_jobs)}] images={n_images_upserted} mentions={n_mentions_created}")

    # ── 第 6 步：最终统计 ──────────────────────────────────────
    final = store.count_images(args.kb)
    print(f"\n[OK] kb={args.kb} Image KG 构建完成：")
    print(f"  Image nodes: {final.get('image_count')}")
    print(f"  MENTIONS edges: {final.get('mentions_count')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
