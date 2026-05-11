"""
知识图谱实体/关系抽取服务。

使用 Gemini API 对 chunk 文本进行实体抽取和关系抽取，
结果存入 SQLite 的 kg_entities / kg_relations 表。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from custom_app.db import get_conn, now_iso
from custom_app.services.google_embedder import strip_images_footer

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompt"

# 实体类型白名单
ENTITY_TYPES = [
    "Person", "Organization", "Location", "Product", "Event",
    "Date", "Work", "Concept", "Resource", "Category", "Operation",
]


def _load_template(template_name: str) -> str:
    """加载 Jinja2 模板文件。"""
    from jinja2 import Environment, FileSystemLoader

    try:
        env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
        tmpl = env.get_template(template_name)
        return tmpl.render(language="zh-CN")
    except Exception as e:
        logger.warning("Failed to load template %s: %s, using fallback", template_name, e)
        if "entities" in template_name:
            return _ENTITY_EXTRACT_FALLBACK
        return _RELATION_EXTRACT_FALLBACK


# 内联 fallback prompt（不依赖 jinja 模板）
_ENTITY_EXTRACT_FALLBACK = (
    "Extract entities from text. Types: " + ", ".join(ENTITY_TYPES) +
    '\nOutput JSON array: [{"title":"...","type":"...","description":"..."}].'
    "\nIf none found, return []."
)

_RELATION_EXTRACT_FALLBACK = (
    'Extract relationships between entities. Output JSON array:'
    '\n[{"source":"...","target":"...","relation_type":"...","description":"...","strength":5-10}].'
    "\nIf none found, return []."
)


def _call_gemini_json(system_prompt: str, text: str, max_retries: int = 3) -> dict:
    """调用 Gemini API，期望 JSON 文本输出。"""
    import requests

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("ULTRARAG_GEMINI_API_KEY") or ""
    model = os.environ.get("ULTRARAG_GEMINI_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": f"# Question\nQ: {text}\nA:"}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=body, timeout=60)
            resp.raise_for_status()
            result = resp.json()
            candidates = result.get("candidates") or []
            for candidate in candidates:
                parts = (candidate.get("content") or {}).get("parts") or []
                for part in parts:
                    t = part.get("text")
                    if t:
                        return t.strip()
            logger.warning("Empty response from Gemini (attempt %d)", attempt + 1)
        except Exception as e:
            logger.warning("Gemini API error (attempt %d): %s", attempt + 1, e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"Failed to call Gemini after {max_retries} retries")


def extract_entities_from_text(text: str) -> List[dict]:
    """从文本中提取实体列表。

    返回: [{"title": str, "type": str, "description": str}, ...]
    """
    prompt = _load_template("kg_extract_entities.jinja")
    try:
        raw = _call_gemini_json(prompt, text)
        entities = json.loads(raw)
        if not isinstance(entities, list):
            return []
        # 验证并过滤
        result = []
        for e in entities:
            if (isinstance(e, dict)
                    and e.get("title")
                    and e.get("type") in ENTITY_TYPES):
                result.append({
                    "title": str(e["title"]).strip(),
                    "type": e["type"],
                    "description": str(e.get("description", "")).strip(),
                })
        return result
    except (json.JSONDecodeError, RuntimeError) as e:
        logger.warning("Entity extraction failed: %s", e)
        return []


def extract_relations_from_text(entities: List[dict], text: str) -> List[dict]:
    """从文本中提取实体间的关系列表。

    返回: [{"source": str, "target": str, "relation_type": str,
            "description": str, "strength": int}, ...]
    """
    if not entities:
        return []

    prompt = _load_template("kg_extract_relations.jinja")

    entity_lines = "\n".join(
        f'  {{"title":"{e["title"]}","type":"{e["type"]}","description":"{e["description"]}"}}'
        for e in entities
    )
    input_text = f"Entities:\n[\n{entity_lines}\n]\n\nText: {text}"

    try:
        raw = _call_gemini_json(prompt, input_text)
        relations = json.loads(raw)
        if not isinstance(relations, list):
            return []
        # 验证
        entity_titles = {e["title"] for e in entities}
        result = []
        for r in relations:
            if (isinstance(r, dict)
                    and r.get("source") in entity_titles
                    and r.get("target") in entity_titles
                    and r.get("relation_type")):
                strength = int(r.get("strength", 5))
                if strength < 5:
                    strength = 5
                elif strength > 10:
                    strength = 10
                result.append({
                    "source": r["source"],
                    "target": r["target"],
                    "relation_type": str(r["relation_type"]).strip(),
                    "description": str(r.get("description", "")).strip(),
                    "strength": strength,
                })
        return result
    except (json.JSONDecodeError, RuntimeError) as e:
        logger.warning("Relation extraction failed: %s", e)
        return []


def _upsert_entity(kb_id: str, entity: dict, chunk_id: str) -> int:
    """插入或更新实体，返回实体 id。"""
    with get_conn() as conn:
        # 查找是否已存在
        row = conn.execute(
            "SELECT id, chunk_ids FROM kg_entities WHERE kb_id=? AND entity_name=?",
            (kb_id, entity["title"]),
        ).fetchone()

        if row:
            entity_id = row["id"]
            chunk_ids = json.loads(row["chunk_ids"] or "[]")
            if chunk_id not in chunk_ids:
                chunk_ids.append(chunk_id)
            conn.execute(
                "UPDATE kg_entities SET description=?, chunk_ids=?, entity_type=? WHERE id=?",
                (entity["description"], json.dumps(chunk_ids, ensure_ascii=False),
                 entity["type"], entity_id),
            )
        else:
            ts = now_iso()
            conn.execute(
                "INSERT INTO kg_entities (kb_id, entity_name, entity_type, description, chunk_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kb_id, entity["title"], entity["type"], entity["description"],
                 json.dumps([chunk_id], ensure_ascii=False), ts),
            )
            entity_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return entity_id


def _add_relation_if_not_exists(kb_id: str, source_id: int, target_id: int, relation: dict) -> bool:
    """添加关系，若已存在则跳过。返回是否新增。"""
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM kg_relations WHERE kb_id=? AND source_id=? AND target_id=? AND relation_type=?",
            (kb_id, source_id, target_id, relation["relation_type"]),
        ).fetchone()
        if exists:
            return False
        ts = now_iso()
        conn.execute(
            "INSERT INTO kg_relations (kb_id, source_id, target_id, relation_type, description, strength, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kb_id, source_id, target_id, relation["relation_type"],
             relation["description"], relation["strength"], ts),
        )
        return True


def extract_and_store_chunk(kb_id: str, chunk: dict) -> Tuple[int, int]:
    """对单个 chunk 进行实体和关系抽取，并存入 SQLite。

    返回: (entity_count, relation_count)
    """
    chunk_id = chunk.get("id", "")
    title = chunk.get("title", "")
    contents = chunk.get("contents", "")

    # 拼接标题和内容，去除图片后缀
    text = strip_images_footer(contents)
    if title:
        text = f"{title}\n{text}"

    entities = extract_entities_from_text(text)
    if not entities:
        return 0, 0

    # 存储实体
    entity_id_map: Dict[str, int] = {}
    for e in entities:
        eid = _upsert_entity(kb_id, e, chunk_id)
        entity_id_map[e["title"]] = eid

    # 抽取并存储关系
    relations = extract_relations_from_text(entities, text)
    rel_count = 0
    for r in relations:
        source_id = entity_id_map.get(r["source"])
        target_id = entity_id_map.get(r["target"])
        if source_id and target_id and source_id != target_id:
            if _add_relation_if_not_exists(kb_id, source_id, target_id, r):
                rel_count += 1

    return len(entities), rel_count


def extract_kb(kb_id: str, chunks_path: str, batch_size: int = 20) -> dict:
    """对整个知识库进行图谱抽取。

    参数:
        kb_id: 知识库 ID
        chunks_path: chunks.jsonl 文件路径
        batch_size: 每批处理的 chunk 数（用于限流）

    返回: {"entity_count": int, "relation_count": int, "chunk_count": int, "errors": int}
    """
    chunks_path_obj = Path(chunks_path)
    if not chunks_path_obj.exists():
        raise FileNotFoundError(f"Chunks file not found: {chunks_path}")

    total_entities = 0
    total_relations = 0
    chunk_count = 0
    errors = 0

    # 清除旧图谱数据
    with get_conn() as conn:
        conn.execute("DELETE FROM kg_relations WHERE kb_id=?", (kb_id,))
        conn.execute("DELETE FROM kg_entities WHERE kb_id=?", (kb_id,))

    logger.info("Starting KG extraction for kb_id=%s from %s", kb_id, chunks_path)

    with open(chunks_path_obj, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
                e_count, r_count = extract_and_store_chunk(kb_id, chunk)
                total_entities += e_count
                total_relations += r_count
                chunk_count += 1

                if chunk_count % batch_size == 0:
                    logger.info(
                        "Processed %d chunks: %d entities, %d relations",
                        chunk_count, total_entities, total_relations,
                    )
                    time.sleep(0.5)  # 限流
            except Exception as e:
                errors += 1
                logger.error("Failed to process chunk %d: %s", i, e)

    logger.info(
        "KG extraction complete for kb_id=%s: %d chunks, %d entities, %d relations, %d errors",
        kb_id, chunk_count, total_entities, total_relations, errors,
    )
    return {
        "entity_count": total_entities,
        "relation_count": total_relations,
        "chunk_count": chunk_count,
        "errors": errors,
    }
