import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import numpy as np
import faiss
from flask import Blueprint, jsonify, request
from custom_app.db import new_id, now_iso
from custom_app.repositories import (
    DocumentRepository,
    JobRepository,
    KbRepository,
)
from custom_app.services.docx_parser import parse_directory, write_chunks_jsonl
from custom_app.services.filename_safe import unicode_safe_filename
from custom_app.services.google_embedder import build_embedding_npy
from custom_app.services.job_executor import JobExecutor
from custom_app.services.parsers import (
    KB_TYPE_GENERAL,
    KB_TYPE_SOP_DOCX,
    VALID_KB_TYPES,
    get_supported_extensions,
)

kb_bp = Blueprint("kb_api", __name__)
_JOB_EXECUTOR = JobExecutor(max_workers=1)

# Phase 4.2: 白名单按 kb.type 动态计算；保留常量做兜底（用于无 type 的老 KB）
_LEGACY_ALLOWED_EXTENSIONS = {".docx", ".pdf"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _req_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _ok(data):
    return jsonify({"request_id": _req_id(), "data": data})


def _err(msg: str, code: str, status: int):
    return jsonify({"request_id": _req_id(), "error": msg, "code": code}), status


def _kb_base_dir() -> Path:
    """知识库根目录（绝对路径，相对于当前工作目录）。"""
    return Path.cwd().resolve() / "data" / "kb"


def _kb_paths(kb_id: str) -> tuple[str, str, str]:
    """返回 (data_path, index_path, embedding_path) 绝对路径字符串。"""
    base = _kb_base_dir() / kb_id
    return (
        str(base),
        str(base / "index" / "index.index"),
        str(base / "embedding" / "embedding.npy"),
    )


def _parse_pagination(args) -> tuple[int, int]:
    """从请求参数解析 limit/offset，默认 limit=100, offset=0。"""
    try:
        limit = max(1, min(int(args.get("limit", 100)), 500))
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = max(0, int(args.get("offset", 0)))
    except (ValueError, TypeError):
        offset = 0
    return limit, offset


def _decorate_job_row(item: dict | None) -> dict | None:
    if item is None:
        return None
    payload_raw = item.get("payload_json") or "{}"
    result_raw = item.get("result_json") or "{}"
    try:
        item["payload"] = json.loads(payload_raw)
    except Exception:
        item["payload"] = {}
    try:
        item["result"] = json.loads(result_raw)
    except Exception:
        item["result"] = {}

    status = str(item.get("status", ""))
    if status == "success":
        chunk_count = item["result"].get("chunk_count")
        if chunk_count is not None:
            item["summary"] = f"success: indexed {chunk_count} chunks"
        else:
            item["summary"] = "success"
    elif status == "failed":
        err = str(item.get("last_error", "")).strip()
        item["summary"] = f"failed: {err[:160]}" if err else "failed"
    else:
        item["summary"] = status
    return item


def _has_running_job(kb_id: str) -> bool:
    """检查 kb 是否有运行中的任务（自动恢复超时僵尸任务）。"""
    stale_timeout_seconds = 120
    job_repo = JobRepository()
    running_rows = job_repo.find_running(kb_id)
    now = datetime.now(timezone.utc)
    for row in running_rows:
        started_at = row["started_at"]
        if not started_at:
            continue
        try:
            st = datetime.fromisoformat(str(started_at))
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            if (now - st).total_seconds() > stale_timeout_seconds:
                job_repo.mark_stale_recovered(row["job_id"], finished_at=now_iso())
        except Exception:
            continue

    return job_repo.has_running(kb_id)


# ── ingest job helpers (refactored) ──────────────────────────────────────────

def _mark_job_running(job_id: str) -> None:
    JobRepository().mark_running(job_id, started_at=now_iso())


def _update_job_stage(job_id: str, stage: str, extra: dict | None = None) -> None:
    """在 ingest 各阶段完成后更新 result_json，供进度接口实时查询。"""
    job_repo = JobRepository()
    result_json = job_repo.get_result_json(job_id)
    existing: dict = {}
    if result_json:
        try:
            existing = json.loads(result_json or "{}")
        except Exception:
            existing = {}
    stages_done: list = existing.get("stages_done", [])
    if stage not in stages_done:
        stages_done.append(stage)
    existing.update({"stages_done": stages_done, **(extra or {})})
    job_repo.update_result_json(
        job_id, result_json=json.dumps(existing), updated_at=now_iso()
    )


def _kb_type(kb: dict) -> str:
    """读取 kb 的 type 字段；老库无此字段时退化到 sop_docx。"""
    return str(kb.get("type") or KB_TYPE_SOP_DOCX)


def _scan_raw_files(kb: dict, raw_dir: Path) -> list[Path]:
    """扫描 raw_dir 中所有 kb.type 支持的文件，按文件名排序。

    Phase 4.2：替换原"硬编码 *.docx 通配"，改成按 KB 类型支持的扩展名集合。
    """
    if not raw_dir.exists():
        return []
    exts = get_supported_extensions(_kb_type(kb))
    files: list[Path] = []
    for fp in raw_dir.iterdir():
        if not fp.is_file():
            continue
        if fp.suffix.lower() in exts:
            files.append(fp)
    return sorted(files, key=lambda p: p.name)


def _register_documents(kb: dict, kb_id: str, raw_dir: Path, chunks_path: Path) -> None:
    """扫描 raw_dir 下的文件并在 kb_documents 中登记（或更新状态为 pending）。

    Phase 4.2：按 kb.type 支持的所有扩展名扫描；file_type 字段记录实际扩展名
    （如 'docx' / 'pdf' / 'png'），便于排障与未来 doc-level 路由。
    """
    raw_files = _scan_raw_files(kb, raw_dir)
    doc_repo = DocumentRepository()
    if raw_files:
        for fp in raw_files:
            file_type = fp.suffix.lower().lstrip(".") or "unknown"
            doc_repo.upsert(
                kb_id=kb_id, tenant_id=kb["tenant_id"],
                doc_id=f"{kb_id}:{fp.name}", file_name=fp.name,
                file_type=file_type, file_path=str(fp),
                channel="api", status="pending", updated_at=now_iso(),
            )
    elif chunks_path.exists():
        doc_repo.upsert(
            kb_id=kb_id, tenant_id=kb["tenant_id"],
            doc_id=f"{kb_id}:chunks", file_name="chunks.jsonl",
            file_type="jsonl", file_path=str(chunks_path),
            channel="api", status="pending", updated_at=now_iso(),
        )
    else:
        kb_type = _kb_type(kb)
        exts = sorted(get_supported_extensions(kb_type))
        raise RuntimeError(
            f"no supported files found under {raw_dir} for kb_type={kb_type!r} "
            f"(expected one of {exts}); and chunks missing: {chunks_path}"
        )


def _parse_stage(kb: dict, raw_dir: Path, kb_root: Path, chunks_path: Path) -> None:
    """阶段1：解析所有支持的文件 → chunks.jsonl。

    Phase 4.2：
      - sop_docx KB 仍走 docx_parser.parse_directory（保留 SOP 业务定制分块）
      - general KB 走 Parser 工厂（per-file 分发到 MineruParser/DoclingParser/MarkdownParser）
    """
    raw_files = _scan_raw_files(kb, raw_dir)
    if not raw_files:
        return  # 调用方在 _register_documents 阶段已校验

    kb_type = _kb_type(kb)

    if kb_type == KB_TYPE_SOP_DOCX:
        # SOP 路径不变：parse_directory 保留 STEP/Heading 业务定制分块
        chunks = parse_directory(raw_dir, kb_root)
        write_chunks_jsonl(chunks, chunks_path)
        return

    # general 路径：用 Parser 工厂逐文件解析
    from custom_app.services.parsers import parse_files

    chunks = parse_files(kb_type, raw_files, kb_root, kb_id=kb.get("kb_id", ""))
    # parse_files 返回 list[Chunk]；write_chunks_jsonl 期望 list[dict]
    chunk_dicts = [c.to_jsonl_dict() for c in chunks]
    write_chunks_jsonl(chunk_dicts, chunks_path)


def _context_stage(chunks_path: Path) -> dict[str, int]:
    """Phase 8.2.1：parse 之后、embed 之前给每个 chunk 生成「文档级上下文摘要」。

    设计原则（与 [[project-phase8-12-roadmap]] 共识 §五.5）：
        - 失败降级：单 chunk 失败 → context="" 仍可索引，不阻塞 ingest
        - 幂等：已有非空 context 的 chunk 默认跳过；重建索引不重新调 Gemini
        - 可关闭：env ULTRARAG_DISABLE_CONTEXTUAL=1 时跳过整 stage（dev 排障用）

    返回：{"generated": N, "skipped": N, "failed": N}；stage 自身永不抛错。
    """
    if os.environ.get("ULTRARAG_DISABLE_CONTEXTUAL", "").strip().lower() in (
        "1", "true", "yes"
    ):
        logger.info("context stage skipped via ULTRARAG_DISABLE_CONTEXTUAL")
        return {"generated": 0, "skipped": 0, "failed": 0, "disabled": 1}

    try:
        from custom_app.services.chunking.contextual import ContextEnricher
    except ImportError as e:
        logger.warning("context stage skipped: cannot import ContextEnricher (%s)", e)
        return {"generated": 0, "skipped": 0, "failed": 0, "disabled": 1}

    try:
        enricher = ContextEnricher()
    except RuntimeError as e:
        # 没配 GOOGLE_API_KEY 之类的启动错误 → 降级跳过整 stage（与共识一致）
        logger.warning("context stage skipped (init failed): %s", e)
        return {"generated": 0, "skipped": 0, "failed": 0, "disabled": 1}

    try:
        n_gen, n_skip, n_fail = enricher.enrich_chunks_jsonl(chunks_path)
    except Exception as e:  # noqa: BLE001 — stage 级降级
        logger.warning("context stage failed unexpectedly: %s", e)
        return {"generated": 0, "skipped": 0, "failed": -1, "error": str(e)}

    return {"generated": n_gen, "skipped": n_skip, "failed": n_fail}


def _embed_stage(chunks_path: Path, embedding_path: Path) -> None:
    """阶段2：chunks.jsonl → embedding.npy。"""
    build_embedding_npy(str(chunks_path), str(embedding_path))


def _index_stage(chunks_path: Path, embedding_path: Path, index_path: Path) -> int:
    """阶段3：embedding.npy → FAISS index，返回 chunk 数量。"""
    if not chunks_path.exists():
        raise FileNotFoundError(f"chunks file not found after parse: {chunks_path}")
    rows = [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    emb = np.load(str(embedding_path))
    if emb.ndim != 2 or emb.shape[0] == 0:
        raise RuntimeError("embedding matrix is empty; check raw DOCX input")
    ids = np.arange(emb.shape[0]).astype(np.int64)
    emb = np.asarray(emb, dtype=np.float32, order="C")
    cpu_flat = faiss.IndexFlatIP(emb.shape[1])
    cpu_index = faiss.IndexIDMap2(cpu_flat)
    cpu_index.add_with_ids(emb, ids)
    faiss.write_index(cpu_index, str(index_path))
    return len(rows)


def _qdrant_stage(
    kb_id: str,
    chunks_path: Path,
    embedding_path: Path,
    force_reindex: bool,
) -> int:
    """Stage 3b：embedding.npy + chunks.jsonl → Qdrant collection（主向量库）。

    读取 _index_stage 已生成的磁盘文件，构建 payload，upsert 到 Qdrant。
    若 force_reindex 或 collection 中已有数据，先重建 collection 清除旧数据。

    返回 upsert 后 collection 的向量总数。
    """
    from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore

    rows = [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    emb = np.load(str(embedding_path))
    if emb.ndim != 2 or emb.shape[0] == 0:
        raise RuntimeError("embedding matrix is empty; cannot upsert to Qdrant")

    if len(rows) != emb.shape[0]:
        raise RuntimeError(
            f"chunk count {len(rows)} != embedding rows {emb.shape[0]}; "
            "data mismatch, aborting Qdrant upsert"
        )

    chunk_ids = [str(c.get("id", "")) for c in rows]
    payloads = [
        {
            "kb_id": kb_id,
            "doc": c.get("doc", ""),
            "source_type": c.get("source_type", "unknown"),
            "parser": c.get("parser", "unknown"),
            "chunk_data": c,
        }
        for c in rows
    ]

    store = QdrantVectorStore(kb_id=kb_id, embed_dim=emb.shape[1])
    should_recreate = force_reindex or store.size() > 0
    store.ensure_collection(recreate=should_recreate)
    store.upsert(chunk_ids, emb, payloads)

    count = store.size()
    logger.info(
        "qdrant_stage kb=%s: upserted %d vectors, collection size=%d",
        kb_id, len(chunk_ids), count,
    )
    return count


def _should_extract_kg(kb_id: str) -> bool:
    """判断该 KB 是否应在 ingest 时自动提取知识图谱。

    复用 enabled_tools_json 里的 "query_knowledge_graph" 开关，无需 DB 迁移。
    未配置时 get_enabled_tools 返回全量工具列表（含 query_knowledge_graph），
    即默认开启 KG 提取。

    参数:
        kb_id: 知识库 ID

    返回:
        True = 应提取 KG；False = 跳过
    """
    from custom_app.services.agent_config_store import get_enabled_tools
    # get_enabled_tools 空 kb_id 返回全量工具，此处无需额外守护
    return "query_knowledge_graph" in (get_enabled_tools(kb_id) or [])


def _kg_stage(kb_id: str, chunks_path: Path) -> dict:
    """阶段4：chunks.jsonl → KgStore（Neo4j / SQLite），返回提取统计。

    内部调用 extract_kb()，其会先 delete_all_for_kb 再全量写入，
    因此重建索引时旧图谱自动清除，无需额外处理。

    参数:
        kb_id:       知识库 ID，用于 KgStore 隔离与旧数据清除
        chunks_path: chunks.jsonl 路径

    返回:
        dict，含 entity_count、relation_count、chunk_count、errors

    注意:
        - 每个 chunk 约需 2 次 Gemini API 调用，整体耗时与 chunk 数线性相关
        - ULTRARAG_KG_BACKEND 决定写入 Neo4j 还是 SQLite（当前默认 neo4j）
    """
    from custom_app.services.kg_extractor import extract_kb
    return extract_kb(kb_id, str(chunks_path))


def _mark_job_success(job_id: str, kb_id: str, chunk_count: int,
                      chunks_path: Path, embedding_path: Path, index_path: Path,
                      force_reindex: bool) -> None:
    """成功收尾：更新 job + KB + documents 三表。

    注：3 步分别 commit；非原子事务，但单条 UPDATE 都是幂等。
    极端情况若中途崩溃，watchdog + 重 ingest 会修复。
    """
    now = now_iso()
    JobRepository().mark_success(
        job_id, finished_at=now, result={
            "force_reindex": force_reindex,
            "chunks_path": str(chunks_path),
            "embedding_path": str(embedding_path),
            "index_path": str(index_path),
            "chunk_count": chunk_count,
            "summary": f"indexed {chunk_count} chunks",
        },
    )
    KbRepository().mark_indexed(kb_id, updated_at=now)
    DocumentRepository().mark_all_indexed(kb_id, updated_at=now)


def _mark_job_failed(job_id: str, kb_id: str, exc: Exception) -> None:
    now = now_iso()
    JobRepository().mark_failed(job_id, finished_at=now, error=str(exc))
    # Phase 6.1: 在途的所有文档（pending/parsing/embedding/indexing）都标 failed
    DocumentRepository().mark_pending_failed(kb_id, error=str(exc), updated_at=now)


# ── Phase 6.1: per-document status helpers ──────────────────────────────────

def _broadcast_doc_status(kb_id: str, status: str) -> None:
    """把该 KB 当前所有"在途"文档批量推进到 status。

    "在途"= pending / parsing / embedding / indexing。一旦 completed / failed
    就不再被广播覆盖。这是 6.1 的折中：parse/embed/index 流水线本身是 KB 维度
    的（一次处理全部 chunks.jsonl），所以单文件粒度的状态由 stage 广播提供，
    单文件级别的失败/重试粒度等 6.2 拆解 chunks.jsonl 后再做。
    """
    repo = DocumentRepository()
    bundle = repo.list_documents_with_status(kb_id)
    in_flight = [
        d for d in bundle["documents"]
        if d.get("status") in ("pending", "parsing", "embedding", "indexing")
    ]
    now = now_iso()
    for d in in_flight:
        repo.update_document_status(
            kb_id, d["doc_id"], status=status, updated_at=now,
        )


def _run_partial_ingest_job(
    kb: dict, kb_id: str, job_id: str, doc_ids: list[str],
) -> dict:
    """Phase 6.2: 单/批文件增量入库。

    只对 doc_ids 范围内的文档跑：parse → chunks.jsonl 增量替换 → embed → Qdrant
    delete-then-upsert → KG delete_by_doc + 增量 extract_kb。

    与 _run_ingest_job 的区别：
    - 不动 raw 目录里其它文件
    - 不更新 embedding.npy（按 6.2 §六 决策；npy 只在全量重建时刷新）
    - 不更新 FAISS（FAISS 已弃用，Step 7 会删）
    - Qdrant collection 复用（不 recreate）

    返回 {"ok": bool, "job_id": str, "status": str, ...}
    """
    from custom_app.repositories import DocumentRepository
    from custom_app.services.google_embedder import (
        embed_texts,
        compose_doc_embedding_text,
    )
    from custom_app.utils.chunks_io import (
        append_chunks,
        collect_chunk_ids_for_doc,
        doc_id_to_stem,
        remove_doc_from_chunks,
    )

    try:
        _mark_job_running(job_id)

        kb_root = Path(kb["data_path"])
        raw_dir = kb_root / "raw"
        chunks_path = kb_root / "corpora" / "chunks.jsonl"
        chunks_path.parent.mkdir(parents=True, exist_ok=True)

        doc_repo = DocumentRepository()
        kb_type = _kb_type(kb)

        # 1) 校验 doc 行存在，构造 doc_id → file_path / file_name / stem 映射
        targets: list[dict] = []
        for did in doc_ids:
            row = doc_repo.get(kb_id, did)
            if row is None:
                raise RuntimeError(f"document not found: {did}")
            targets.append({
                "doc_id": did,
                "file_name": row["file_name"],
                "file_path": Path(row["file_path"]),
                "stem": doc_id_to_stem(did),
            })

        # 2) parsing 阶段：每个 doc 独立 parse → 新 chunks 列表
        for t in targets:
            doc_repo.update_document_status(
                kb_id, t["doc_id"], status="parsing", updated_at=now_iso(),
            )

        new_chunks: list[dict] = []
        parse_errors: dict[str, str] = {}
        for t in targets:
            try:
                if kb_type == KB_TYPE_SOP_DOCX:
                    from custom_app.services.docx_parser import parse_docx
                    rows = parse_docx(t["file_path"], kb_root)
                    new_chunks.extend(rows)
                else:
                    from custom_app.services.parsers import parse_files
                    chunks_objs = parse_files(
                        kb_type, [t["file_path"]], kb_root, kb_id=kb_id,
                    )
                    new_chunks.extend(c.to_jsonl_dict() for c in chunks_objs)
            except Exception as exc:
                logger.exception(
                    "partial parse failed kb=%s doc=%s", kb_id, t["doc_id"]
                )
                parse_errors[t["doc_id"]] = str(exc)

        # 把 parse 失败的标 failed；剩下的继续
        for did, err in parse_errors.items():
            doc_repo.update_document_status(
                kb_id, did, status="failed",
                updated_at=now_iso(),
                error_message=err[:500],
            )
        remaining = [t for t in targets if t["doc_id"] not in parse_errors]
        if not remaining and parse_errors:
            raise RuntimeError(f"all targets failed in parse stage: {parse_errors}")
        _update_job_stage(job_id, "parse", {"file_count": len(remaining)})

        # 3) chunks.jsonl：先删该 doc 的旧行，再追加新 chunks
        old_chunk_ids: list[str] = []
        for t in remaining:
            old_chunk_ids.extend(collect_chunk_ids_for_doc(chunks_path, t["stem"]))
            remove_doc_from_chunks(chunks_path, t["stem"])
        append_chunks(chunks_path, new_chunks)

        # 4) embedding 阶段：只为 new_chunks 跑 embedding
        for t in remaining:
            doc_repo.update_document_status(
                kb_id, t["doc_id"], status="embedding", updated_at=now_iso(),
            )
        if new_chunks:
            texts = [compose_doc_embedding_text(c) for c in new_chunks]
            emb = embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")
        else:
            emb = None
        _update_job_stage(job_id, "embed", {"new_chunks": len(new_chunks)})

        # 5) Qdrant：先 delete 旧 chunk_ids，再 upsert 新点
        for t in remaining:
            doc_repo.update_document_status(
                kb_id, t["doc_id"], status="indexing", updated_at=now_iso(),
            )
        if new_chunks and emb is not None:
            from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore
            store = QdrantVectorStore(kb_id=kb_id, embed_dim=emb.shape[1])
            store.ensure_collection(recreate=False)
            if old_chunk_ids:
                try:
                    store.delete(old_chunk_ids)
                except Exception:
                    logger.exception(
                        "qdrant delete old vectors failed kb=%s; continuing", kb_id,
                    )
            chunk_ids = [str(c.get("id", "")) for c in new_chunks]
            payloads = [
                {
                    "kb_id": kb_id,
                    "doc": c.get("doc", ""),
                    "source_type": c.get("source_type", "unknown"),
                    "parser": c.get("parser", "unknown"),
                    "chunk_data": c,
                }
                for c in new_chunks
            ]
            store.upsert(chunk_ids, emb, payloads)
        _update_job_stage(job_id, "qdrant", {"upserted": len(new_chunks)})

        # 6) KG：先 delete_by_doc，再增量 extract_kb（限定 doc_stem 集合）
        if _should_extract_kg(kb_id):
            try:
                from custom_app.services.kg_extractor import extract_kb
                from custom_app.services.kgstore.base import build_kg_store
                kg_store = build_kg_store()
                stems = {t["stem"] for t in remaining}
                doc_id_for_stem = {t["stem"]: t["doc_id"] for t in remaining}
                for t in remaining:
                    try:
                        kg_store.delete_by_doc(kb_id, t["doc_id"])
                    except Exception:
                        logger.exception(
                            "kg delete_by_doc failed kb=%s doc=%s",
                            kb_id, t["doc_id"],
                        )
                kg_result = extract_kb(
                    kb_id, str(chunks_path),
                    doc_id_for_stem=doc_id_for_stem,
                    target_doc_stems=stems,
                )
                _update_job_stage(job_id, "kg", {
                    "kg_status": "ok",
                    "kg_entity_count": int(kg_result.get("entity_count", 0)),
                    "kg_relation_count": int(kg_result.get("relation_count", 0)),
                    "kg_chunk_count": int(kg_result.get("chunk_count", 0)),
                    "kg_error_count": int(kg_result.get("errors", 0)),
                })
            except Exception as kg_exc:
                logger.exception("partial kg_stage failed kb=%s", kb_id)
                _update_job_stage(job_id, "kg_failed", {
                    "kg_status": "failed",
                    "kg_error": str(kg_exc),
                })

        # 7) 把剩下的 doc 行标 completed + chunk_count
        now = now_iso()
        per_stem_count: dict[str, int] = {}
        for c in new_chunks:
            stem = str(c.get("doc", ""))
            per_stem_count[stem] = per_stem_count.get(stem, 0) + 1
        for t in remaining:
            doc_repo.update_document_status(
                kb_id, t["doc_id"], status="completed",
                updated_at=now,
                chunk_count=per_stem_count.get(t["stem"], 0),
                processed_at=now,
            )

        # 8) 收尾：标 job success；不调 mark_all_indexed（其它文档不动）
        JobRepository().mark_success(
            job_id, finished_at=now, result={
                "partial": True,
                "doc_ids": [t["doc_id"] for t in remaining],
                "failed_doc_ids": list(parse_errors.keys()),
                "chunk_count": len(new_chunks),
                "summary": f"partial indexed {len(remaining)} docs / {len(new_chunks)} chunks",
            },
        )
        KbRepository().mark_indexed(kb_id, updated_at=now)

        # 失效 runner 缓存，让查询拿到新数据
        try:
            from custom_app.api.chat import invalidate_runner_cache
            invalidate_runner_cache(kb_id)
        except Exception:
            logger.exception("invalidate_runner_cache failed kb=%s", kb_id)

        return {
            "ok": True, "job_id": job_id, "status": "success",
            "doc_ids": [t["doc_id"] for t in remaining],
            "failed_doc_ids": list(parse_errors.keys()),
        }

    except Exception as exc:
        logger.exception("partial ingest crashed kb=%s doc_ids=%s", kb_id, doc_ids)
        now = now_iso()
        JobRepository().mark_failed(job_id, finished_at=now, error=str(exc))
        # 在途文档标 failed
        try:
            doc_repo = DocumentRepository()
            for did in doc_ids:
                row = doc_repo.get(kb_id, did)
                if row and row.get("status") in (
                    "pending", "parsing", "embedding", "indexing"
                ):
                    doc_repo.update_document_status(
                        kb_id, did, status="failed",
                        updated_at=now, error_message=str(exc)[:500],
                    )
        except Exception:
            logger.exception("failed to mark docs failed on partial crash")
        return {"ok": False, "job_id": job_id, "status": "failed", "error": str(exc)}


def _attribute_chunk_counts(kb_id: str, chunks_path: Path) -> None:
    """ingest 完成后，按 chunks.jsonl 中的 doc 字段把分块数摊到每个文档行。

    chunk_count 是 doc_stem 维度的；doc_id = f"{kb_id}:{fp.name}"（含扩展）。
    匹配规则：取 doc_id 中 ':' 后的 stem 与 chunk['doc'] 对照。
    无法对上的文档保持 chunk_count=0。
    """
    if not chunks_path.exists():
        return
    counts: dict[str, int] = {}
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        doc_stem = str(obj.get("doc", "")).strip()
        if not doc_stem:
            continue
        counts[doc_stem] = counts.get(doc_stem, 0) + 1

    repo = DocumentRepository()
    bundle = repo.list_documents_with_status(kb_id)
    now = now_iso()
    for d in bundle["documents"]:
        if d.get("status") != "completed":
            continue
        # doc_id 形如 "{kb_id}:{file_name}"；取 file_name 的 stem
        doc_id = d["doc_id"]
        file_name = doc_id.split(":", 1)[1] if ":" in doc_id else doc_id
        stem = Path(file_name).stem
        cnt = counts.get(stem, 0)
        repo.update_document_status(
            kb_id, doc_id, status="completed", updated_at=now,
            chunk_count=cnt, processed_at=now,
        )


def _run_ingest_job(kb: dict, kb_id: str, job_id: str, force_reindex: bool) -> dict:
    """执行入库任务的完整五阶段流程（Phase X: Qdrant 为主向量库 + KG 提取）。

    阶段顺序：
        1. parse   : 原始文件 → chunks.jsonl
        2. embed   : chunks.jsonl → embedding.npy（Gemini）
        3a. index  : embedding.npy → FAISS 索引文件（向后兼容）
        3b. qdrant : embedding.npy → Qdrant collection（PRIMARY）
        4. kg      : chunks.jsonl → KgStore（可选，按 enabled_tools 决定）

    参数:
        kb:            knowledge_bases 表的一行（dict），含 data_path/embedding_path/index_path
        kb_id:         知识库 ID
        job_id:        当前 ingest job ID
        force_reindex: 是否强制重建（True 时跳过旧索引检查；Qdrant collection 也重建）

    返回:
        {"ok": bool, "job_id": str, "status": str, ...}

    注意:
        - Stage 3b Qdrant upsert 失败时仅记录警告，不影响 job 最终状态
        - Stage 4 KG 提取失败时仅记录警告，不影响 job 最终状态
        - KG 提取耗时与 chunk 数成正比（每 chunk ~2 次 Gemini API）
    """
    try:
        _mark_job_running(job_id)

        kb_root = Path(kb["data_path"])
        raw_dir = kb_root / "raw"
        corpora_dir = kb_root / "corpora"
        chunks_path = corpora_dir / "chunks.jsonl"
        embedding_path = Path(kb["embedding_path"])
        index_path = Path(kb["index_path"])

        for d in [raw_dir, corpora_dir, embedding_path.parent, index_path.parent]:
            d.mkdir(parents=True, exist_ok=True)

        _register_documents(kb, kb_id, raw_dir, chunks_path)
        # Phase 4.2: file_count 不再硬编码 *.docx；按 kb.type 支持的扩展名汇总
        file_count = len(_scan_raw_files(kb, raw_dir))

        # Phase 6.1: 每个 stage 进入前把在途文档批量推进到对应状态。
        _broadcast_doc_status(kb_id, "parsing")
        _parse_stage(kb, raw_dir, kb_root, chunks_path)
        _update_job_stage(job_id, "parse", {"file_count": file_count})

        # Phase 8.2.1：parse 之后、embed 之前给每个 chunk 生成「文档级上下文摘要」
        # 失败不阻塞 ingest（共识 §五.5：降级 + 日志告警）
        _broadcast_doc_status(kb_id, "context")
        ctx_stats = _context_stage(chunks_path)
        _update_job_stage(job_id, "context", ctx_stats)

        _broadcast_doc_status(kb_id, "embedding")
        _embed_stage(chunks_path, embedding_path)
        _update_job_stage(job_id, "embed")

        _broadcast_doc_status(kb_id, "indexing")
        chunk_count = _index_stage(chunks_path, embedding_path, index_path)
        _update_job_stage(job_id, "index", {"chunk_count": chunk_count})

        # Stage 3b: Qdrant upsert（主向量库，失败只警告不阻塞）
        try:
            qdrant_count = _qdrant_stage(kb_id, chunks_path, embedding_path, force_reindex)
            _update_job_stage(job_id, "qdrant", {"qdrant_chunk_count": qdrant_count})
        except Exception as qdrant_exc:
            logger.warning("qdrant_stage failed kb=%s: %s", kb_id, qdrant_exc)
            _update_job_stage(job_id, "qdrant_failed", {"qdrant_error": str(qdrant_exc)})

        # Stage 4: KG 实体关系提取（可选，失败不中断 ingest）
        # 条件：KB 的 enabled_tools 包含 query_knowledge_graph
        # 失败策略：仍然不抛出（保持索引可用），但把详细原因写进 result_json 的
        # kg_status / kg_error / kg_error_type，前端 jobs 进度页可直接展示，
        # 避免 KG 静默归零却没人发现（之前的 bug）。
        if _should_extract_kg(kb_id):
            try:
                kg_result = _kg_stage(kb_id, chunks_path)
                kg_entity = int(kg_result.get("entity_count", 0))
                kg_rel = int(kg_result.get("relation_count", 0))
                kg_errors = int(kg_result.get("errors", 0))
                if kg_entity == 0 and kg_rel == 0:
                    # 完成但写入 0：通常意味着 LLM 抽取全部失败，需要明确告警
                    kg_status = "empty" if kg_errors == 0 else "errors"
                    kg_message = (
                        f"KG 抽取已完成但未写入任何实体/关系（chunks={kg_result.get('chunk_count', 0)}, "
                        f"errors={kg_errors}）。请检查 logs/kg_ingest.log。"
                    )
                    logger.warning("kg_stage produced empty graph kb=%s: %s", kb_id, kg_message)
                else:
                    kg_status = "ok"
                    kg_message = ""
                _update_job_stage(job_id, "kg", {
                    "kg_status": kg_status,
                    "kg_entity_count": kg_entity,
                    "kg_relation_count": kg_rel,
                    "kg_chunk_count": int(kg_result.get("chunk_count", 0)),
                    "kg_error_count": kg_errors,
                    "kg_message": kg_message,
                })
            except Exception as kg_exc:
                logger.exception("kg_stage failed kb=%s", kb_id)
                _update_job_stage(job_id, "kg_failed", {
                    "kg_status": "failed",
                    "kg_error": str(kg_exc),
                    "kg_error_type": type(kg_exc).__name__,
                })

        _mark_job_success(job_id, kb_id, chunk_count, chunks_path, embedding_path, index_path, force_reindex)
        # Phase 6.1: completed 后按文件名把 chunk_count / processed_at 分摊到每个文档
        try:
            _attribute_chunk_counts(kb_id, chunks_path)
        except Exception:
            logger.exception("attribute_chunk_counts failed kb=%s", kb_id)
        # 重建索引后必须失效 chat.py 中按 kb_id 缓存的 RagRunner / AgentRunner，
        # 否则它们仍持有旧 rows / 旧 FAISS，新增文档查不到、删除的文档仍可能召回。
        # 失败路径不做失效（旧索引仍可继续服务）。
        try:
            from custom_app.api.chat import invalidate_runner_cache
            invalidate_runner_cache(kb_id)
        except Exception:
            logger.exception("invalidate_runner_cache failed kb=%s", kb_id)
        return {"ok": True, "job_id": job_id, "status": "success"}

    except Exception as exc:
        _mark_job_failed(job_id, kb_id, exc)
        return {"ok": False, "job_id": job_id, "status": "failed", "error": str(exc)}


# ── KB CRUD ──────────────────────────────────────────────────────────────────

@kb_bp.route("/api/kb", methods=["POST"])
def create_kb():
    body = request.get_json(silent=True) or {}
    kb_id = str(body.get("kb_id", "")).strip()
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    tenant_id = str(body.get("tenant_id", "default")).strip() or "default"
    # Phase 4.2: kb 类型，决定解析器路由 (sop_docx / general)；老库不传时默认 sop_docx
    kb_type = str(body.get("type", KB_TYPE_SOP_DOCX)).strip() or KB_TYPE_SOP_DOCX

    if not kb_id:
        return _err("kb_id is required", "KB_ID_REQUIRED", 400)
    if not name:
        return _err("name is required", "KB_NAME_REQUIRED", 400)
    if kb_type not in VALID_KB_TYPES:
        return _err(
            f"invalid type {kb_type!r}, expected one of {sorted(VALID_KB_TYPES)}",
            "KB_TYPE_INVALID",
            400,
        )

    data_path, index_path, embedding_path = _kb_paths(kb_id)
    created_at = now_iso()

    kb_repo = KbRepository()
    if kb_repo.exists(kb_id):
        return _err(f"kb_id already exists: {kb_id}", "KB_ALREADY_EXISTS", 409)
    # 若有同 kb_id 的 archived 记录（软删除残留），先清掉再插入，避免主键冲突
    kb_repo.hard_delete(kb_id)
    kb_repo.create(
        kb_id=kb_id, name=name, description=description,
        tenant_id=tenant_id, kb_type=kb_type,
        data_path=data_path, index_path=index_path, embedding_path=embedding_path,
        created_at=created_at,
    )

    for rel in ["raw", "corpora", "embedding", "index", "images"]:
        (Path(data_path) / rel).mkdir(parents=True, exist_ok=True)

    return _ok({"kb_id": kb_id, "type": kb_type, "status": "active"})


@kb_bp.route("/api/kb", methods=["GET"])
def list_kb():
    role_id = request.args.get("role_id", "").strip() or None
    include_archived = str(request.args.get("include_archived", "false")).lower() == "true"
    limit, offset = _parse_pagination(request.args)
    rows = KbRepository().list_paginated(
        role_id=role_id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return _ok(rows)


@kb_bp.route("/api/kb/<string:kb_id>", methods=["GET"])
def get_kb(kb_id: str):
    include_archived = str(request.args.get("include_archived", "false")).lower() == "true"
    item = KbRepository().get(kb_id, include_archived=include_archived)
    if item is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    return _ok(item)


@kb_bp.route("/api/kb/<string:kb_id>", methods=["PUT"])
def update_kb(kb_id: str):
    kb_repo = KbRepository()
    current = kb_repo.get_basic(kb_id)
    if current is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    body = request.get_json(silent=True) or {}
    new_name = str(body.get("name", current["name"])).strip()
    new_desc = str(body.get("description", current["description"])).strip()

    if not new_name:
        return _err("name cannot be empty", "KB_NAME_EMPTY", 400)

    kb_repo.update_basic(kb_id, name=new_name, description=new_desc, updated_at=now_iso())
    return _ok({"kb_id": kb_id, "name": new_name, "description": new_desc})


@kb_bp.route("/api/kb/<string:kb_id>", methods=["DELETE"])
def delete_kb(kb_id: str):
    hard = str(request.args.get("hard", "false")).lower() == "true"
    kb_repo = KbRepository()
    current = kb_repo.get_basic(kb_id)
    if current is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    if hard:
        kb_repo.hard_delete(kb_id)
        kb_dir = Path(current["data_path"])
        if kb_dir.exists():
            shutil.rmtree(kb_dir, ignore_errors=True)

        # 清理 Qdrant collection
        try:
            from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore

            qdrant_store = QdrantVectorStore(kb_id=kb_id)
            qdrant_store.delete_collection()
        except Exception as qdrant_del_exc:
            logger.warning(
                "Failed to delete Qdrant collection kb=%s: %s",
                kb_id,
                qdrant_del_exc,
            )

        # 清理 Neo4j KG 数据
        try:
            from custom_app.services.kgstore.base import build_kg_store

            kg_store = build_kg_store()
            rc, ec = kg_store.delete_all_for_kb(kb_id)
            logger.info(
                "Deleted KG data for kb=%s: %d relations, %d entities",
                kb_id,
                ec,
                rc,
            )
        except Exception as kg_del_exc:
            logger.warning(
                "Failed to delete KG data kb=%s: %s", kb_id, kg_del_exc
            )
    else:
        kb_repo.archive(kb_id, updated_at=now_iso())
    return _ok({"kb_id": kb_id, "deleted": True, "hard": hard})


# ── Document upload ───────────────────────────────────────────────────────────

@kb_bp.route("/api/kb/<string:kb_id>/documents/upload", methods=["POST"])
def upload_documents(kb_id: str):
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    # Phase 4.2: 白名单按 kb.type 动态计算
    try:
        allowed_exts = get_supported_extensions(_kb_type(kb))
    except ValueError:
        # 不应发生（DB 里 type 列已校验），但兜底防御
        allowed_exts = _LEGACY_ALLOWED_EXTENSIONS

    files = request.files.getlist("files") or request.files.getlist("file")
    if not files or all(f.filename == "" for f in files):
        return _err("no file provided", "NO_FILE", 400)

    raw_dir = Path(kb["data_path"]) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    doc_repo = DocumentRepository()
    uploaded: list[str] = []
    for f in files:
        if not f.filename:
            continue
        safe_name = unicode_safe_filename(f.filename)
        ext = Path(safe_name).suffix.lower()
        if ext not in allowed_exts:
            continue
        # 同名冲突时追加序号，保证多个 Unicode 名各自落盘
        dest = raw_dir / safe_name
        if dest.exists():
            stem = Path(safe_name).stem
            idx = 1
            while True:
                candidate = raw_dir / f"{stem}_{idx}{ext}"
                if not candidate.exists():
                    dest = candidate
                    safe_name = candidate.name
                    break
                idx += 1
        f.save(str(dest))
        # Phase 6.1：上传完成统一写 'pending'，与状态枚举对齐
        # （uploaded 不是 6.1 枚举的合法值，前端汇总条/轮询无法识别）
        doc_repo.upsert(
            kb_id=kb_id, tenant_id=kb["tenant_id"],
            doc_id=f"{kb_id}:{safe_name}", file_name=safe_name,
            file_type=ext.lstrip("."), file_path=str(dest),
            channel="web", status="pending", updated_at=now_iso(),
        )
        uploaded.append(safe_name)

    if not uploaded:
        allowed_display = ", ".join(sorted(allowed_exts))
        return _err(
            f"no valid files found (allowed: {allowed_display})",
            "NO_VALID_FILE",
            400,
        )
    return _ok({"uploaded": len(uploaded), "files": uploaded})


# ── Ingest jobs ───────────────────────────────────────────────────────────────

@kb_bp.route("/api/kb/<string:kb_id>/ingest", methods=["POST"])
def create_ingest_job(kb_id: str):
    body = request.get_json(silent=True) or {}
    force_reindex = bool(body.get("force_reindex", False))
    async_mode = bool(body.get("async", False))

    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    if _has_running_job(kb_id):
        return _err(f"kb has running job: {kb_id}", "KB_JOB_RUNNING", 409)

    job_id = new_id("job")
    JobRepository().create_ingest_job(
        job_id=job_id, tenant_id=kb["tenant_id"], kb_id=kb_id,
        payload={"force_reindex": force_reindex}, created_at=now_iso(),
    )

    if async_mode:
        _JOB_EXECUTOR.submit(_run_ingest_job, dict(kb), kb_id, job_id, force_reindex)
        return _ok({"job_id": job_id, "status": "pending", "async": True})

    res = _run_ingest_job(kb, kb_id, job_id, force_reindex)
    if res.get("ok"):
        return _ok({"job_id": job_id, "status": "success"})
    return _err(f"ingest failed: {res.get('error', 'unknown error')}", "INGEST_FAILED", 500)


@kb_bp.route("/api/kb/<string:kb_id>/jobs", methods=["GET"])
def list_jobs(kb_id: str):
    limit, offset = _parse_pagination(request.args)
    kb_repo = KbRepository()
    if not kb_repo.exists(kb_id):
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    rows = JobRepository().list_for_kb(kb_id, limit=limit, offset=offset)
    return _ok([_decorate_job_row(r) for r in rows])


@kb_bp.route("/api/kb/<string:kb_id>/jobs/<string:job_id>", methods=["GET"])
def get_job(kb_id: str, job_id: str):
    kb_repo = KbRepository()
    if not kb_repo.exists(kb_id):
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    job = JobRepository().get_for_kb(kb_id, job_id)
    item = _decorate_job_row(job)
    if item is None:
        return _err(f"job not found: {job_id}", "JOB_NOT_FOUND", 404)
    return _ok(item)


@kb_bp.route("/api/kb/<string:kb_id>/jobs/<string:job_id>/cancel", methods=["POST"])
def cancel_job(kb_id: str, job_id: str):
    job_repo = JobRepository()
    job = job_repo.get_for_kb(kb_id, job_id)
    if job is None:
        return _err(f"job not found: {job_id}", "JOB_NOT_FOUND", 404)
    if job["status"] in ("success", "failed", "cancelled"):
        return _ok({"job_id": job_id, "status": job["status"], "cancelled": False})
    job_repo.mark_cancelled(kb_id, job_id, finished_at=now_iso())
    return _ok({"job_id": job_id, "status": "cancelled", "cancelled": True})


@kb_bp.route("/api/kb/<string:kb_id>/jobs/<string:job_id>/retry", methods=["POST"])
def retry_job(kb_id: str, job_id: str):
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    job_repo = JobRepository()
    job = job_repo.get_for_kb(kb_id, job_id)
    if job is None:
        return _err(f"job not found: {job_id}", "JOB_NOT_FOUND", 404)
    if job["status"] == "running":
        return _err("job is running", "JOB_RUNNING", 409)
    if _has_running_job(kb_id):
        return _err(f"kb has running job: {kb_id}", "KB_JOB_RUNNING", 409)
    payload = json.loads(job["payload_json"] or "{}")
    force_reindex = bool(payload.get("force_reindex", False))
    job_repo.reset_for_retry(kb_id, job_id, updated_at=now_iso())
    return _run_ingest_job(kb, kb_id, job_id, force_reindex)


@kb_bp.route("/api/kb/<string:kb_id>/jobs/<string:job_id>/run", methods=["POST"])
def run_pending_job(kb_id: str, job_id: str):
    body = request.get_json(silent=True) or {}
    async_mode = bool(body.get("async", False))
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    job_repo = JobRepository()
    job = job_repo.get_for_kb(kb_id, job_id)
    if job is None:
        return _err(f"job not found: {job_id}", "JOB_NOT_FOUND", 404)
    if job["status"] == "running":
        return _err("job is running", "JOB_RUNNING", 409)
    if _has_running_job(kb_id):
        return _err(f"kb has running job: {kb_id}", "KB_JOB_RUNNING", 409)
    payload = json.loads(job["payload_json"] or "{}")
    force_reindex = bool(payload.get("force_reindex", False))
    job_repo.reset_for_run(kb_id, job_id, updated_at=now_iso())
    if async_mode:
        _JOB_EXECUTOR.submit(_run_ingest_job, dict(kb), kb_id, job_id, force_reindex)
        return _ok({"job_id": job_id, "status": "pending", "async": True})
    res = _run_ingest_job(kb, kb_id, job_id, force_reindex)
    if res.get("ok"):
        return _ok({"job_id": job_id, "status": "success"})
    return _err(f"ingest failed: {res.get('error', 'unknown error')}", "INGEST_FAILED", 500)


@kb_bp.route("/api/kb/<string:kb_id>/documents", methods=["GET"])
def list_documents(kb_id: str):
    """列出该 KB 下所有文档（Phase 6.1：含 summary 派生字段）。

    返回：
        {
          "documents": [{ doc_id, file_name, file_type, status,
                          chunk_count, processed_at, error_message, ... }],
          "summary":   { pending, parsing, embedding, indexing,
                         completed, failed, deleting }
        }

    分页参数被忽略：admin 详情页一次性渲染该 KB 所有文档+汇总，且文档量级
    一般 ≤几百。如果未来文档数量爆炸，再追加 limit/offset。
    """
    if not KbRepository().exists(kb_id):
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    return _ok(DocumentRepository().list_documents_with_status(kb_id))


@kb_bp.route(
    "/api/kb/<string:kb_id>/documents/batch-status",
    methods=["POST"],
)
def batch_document_status(kb_id: str):
    """Phase 6.1：轮询用，返回指定 doc_ids 的最新状态字段。

    body: {"doc_ids": ["kb_id:foo.docx", ...]}
    """
    if not KbRepository().exists(kb_id):
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    body = request.get_json(silent=True) or {}
    raw_ids = body.get("doc_ids") or []
    if not isinstance(raw_ids, list):
        return _err("doc_ids must be a list", "INVALID_DOC_IDS", 400)
    doc_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    # 上限保护：避免恶意/失控前端把整张表批回来
    doc_ids = doc_ids[:500]
    rows = DocumentRepository().batch_get_documents(kb_id, doc_ids)
    return _ok(rows)


@kb_bp.route(
    "/api/kb/<string:kb_id>/documents/<string:doc_id>/retry",
    methods=["POST"],
)
def retry_document(kb_id: str, doc_id: str):
    """Phase 6.1：单文档失败重试。

    实现选择（6.1 折中版）：不做"只重跑这一个文件"——chunks.jsonl 是全 KB
    维度的流水线，单独抽出一份成本高。本路由先把该文档行重置回 pending +
    清错误信息，然后触发一次新的 ingest job（force_reindex=False）。这跟
    "重建索引"按钮路径一致，但只在该 KB 有失败文档时调用更直观。
    单文件级别的真重建留到 Phase 6.2 拆解 chunks.jsonl 后再做。
    """
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    doc_repo = DocumentRepository()
    row = doc_repo.get(kb_id, doc_id)
    if row is None:
        return _err(f"document not found: {doc_id}", "DOC_NOT_FOUND", 404)

    if _has_running_job(kb_id):
        return _err(f"kb has running job: {kb_id}", "KB_JOB_RUNNING", 409)

    now = now_iso()
    doc_repo.update_document_status(
        kb_id, doc_id, status="pending", updated_at=now,
    )

    job_id = new_id("job")
    JobRepository().create_ingest_job(
        job_id=job_id, tenant_id=kb["tenant_id"], kb_id=kb_id,
        payload={"force_reindex": False, "doc_retry": doc_id},
        created_at=now,
    )
    _JOB_EXECUTOR.submit(_run_ingest_job, dict(kb), kb_id, job_id, False)
    return _ok({"job_id": job_id, "doc_id": doc_id, "status": "pending"})


@kb_bp.route(
    "/api/kb/<string:kb_id>/documents/<string:doc_id>/chunks",
    methods=["GET"],
)
def list_document_chunks(kb_id: str, doc_id: str):
    """Phase 6.1：取该文档的全部 chunk，详情面板用。

    参数：
        max_chars  contents 每块最大字符数，默认 0=不截断
    """
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    doc = DocumentRepository().get(kb_id, doc_id)
    if doc is None:
        return _err(f"document not found: {doc_id}", "DOC_NOT_FOUND", 404)

    file_name = doc.get("file_name") or doc_id.split(":", 1)[-1]
    doc_stem = Path(file_name).stem

    chunks_path = Path(kb["data_path"]) / "corpora" / "chunks.jsonl"
    if not chunks_path.exists():
        return _ok({"doc_id": doc_id, "doc_stem": doc_stem, "chunks": []})

    try:
        max_chars = int(request.args.get("max_chars", 0))
    except (ValueError, TypeError):
        max_chars = 0
    if max_chars < 0:
        max_chars = 0

    out: list[dict] = []
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except Exception:
            continue
        if chunk.get("doc") != doc_stem:
            continue
        contents = str(chunk.get("contents", ""))
        if max_chars and len(contents) > max_chars:
            contents = contents[:max_chars] + "..."
        out.append({
            "id": chunk.get("id", ""),
            "title": chunk.get("title", ""),
            "doc": chunk.get("doc", ""),
            "contents": contents,
            "char_count": len(str(chunk.get("contents", ""))),
            "image_count": len(chunk.get("images") or []),
        })
    return _ok({"doc_id": doc_id, "doc_stem": doc_stem, "chunks": out})


def _delete_document_full(kb: dict, kb_id: str, doc_id: str) -> dict:
    """Phase 6.2: 完整清理单文档：Qdrant + KG + chunks.jsonl + DB 行 + raw 文件。

    步骤（任一失败抛 RuntimeError，调用方负责把 doc 状态回滚或返回 5xx）：
        1) doc 行标 deleting
        2) chunks.jsonl 拿到该 doc 的 chunk_ids（删 Qdrant 用）
        3) Qdrant：按 chunk_ids 删除该 doc 的所有向量
        4) KG：delete_by_doc（relations + 孤儿/裁剪实体）
        5) chunks.jsonl：原子覆写过滤掉该 doc
        6) kb_documents 行 delete
        7) raw 文件 unlink
    """
    from custom_app.utils.chunks_io import (
        collect_chunk_ids_for_doc,
        doc_id_to_stem,
        remove_doc_from_chunks,
    )

    doc_repo = DocumentRepository()
    row = doc_repo.get(kb_id, doc_id)
    if row is None:
        raise RuntimeError(f"document not found: {doc_id}")

    kb_root = Path(kb["data_path"]).resolve()
    chunks_path = kb_root / "corpora" / "chunks.jsonl"
    fp = Path(row["file_path"])
    try:
        fp.resolve().relative_to(kb_root)
    except (OSError, ValueError):
        raise RuntimeError("invalid file path")

    stem = doc_id_to_stem(doc_id)

    # 1) 标 deleting
    doc_repo.update_document_status(
        kb_id, doc_id, status="deleting", updated_at=now_iso(),
    )

    # 2) 收集 chunk_ids
    chunk_ids = collect_chunk_ids_for_doc(chunks_path, stem)

    # 3) Qdrant
    qdrant_deleted = 0
    try:
        from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore
        if chunk_ids:
            store = QdrantVectorStore(kb_id=kb_id)  # 删除时不需要 embed_dim
            store.delete(chunk_ids)
            qdrant_deleted = len(chunk_ids)
    except Exception:
        logger.exception("qdrant delete failed kb=%s doc=%s", kb_id, doc_id)
        # 不中断：删除主流程要求最终一致；若 Qdrant 真死了由 6.2 retry 兜底
        # （TODO: 暴露失败给用户）

    # 4) KG
    kg_rel = kg_ent = 0
    try:
        from custom_app.services.kgstore.base import build_kg_store
        kg_store = build_kg_store()
        kg_rel, kg_ent = kg_store.delete_by_doc(kb_id, doc_id)
    except Exception:
        logger.exception("kg delete_by_doc failed kb=%s doc=%s", kb_id, doc_id)

    # 5) chunks.jsonl
    removed_chunks = 0
    try:
        removed_chunks = remove_doc_from_chunks(chunks_path, stem)
    except Exception:
        logger.exception("chunks.jsonl edit failed kb=%s doc=%s", kb_id, doc_id)

    # 6) DB 行
    doc_repo.delete(kb_id, doc_id)

    # 7) raw 文件
    if fp.exists():
        try:
            fp.unlink()
        except OSError:
            logger.warning("raw unlink failed kb=%s doc=%s path=%s", kb_id, doc_id, fp)

    # 失效 runner 缓存，让查询拿到新数据
    try:
        from custom_app.api.chat import invalidate_runner_cache
        invalidate_runner_cache(kb_id)
    except Exception:
        logger.exception("invalidate_runner_cache failed kb=%s", kb_id)

    return {
        "doc_id": doc_id,
        "deleted": True,
        "chunks_removed": removed_chunks,
        "qdrant_deleted": qdrant_deleted,
        "kg_relations_deleted": kg_rel,
        "kg_entities_deleted": kg_ent,
    }


@kb_bp.route("/api/kb/<string:kb_id>/documents", methods=["DELETE"])
def delete_document_legacy(kb_id: str):
    """Phase 6.2: 兼容老调用 ?doc_id=X 的 DELETE；新前端用路径参数版本。

    完整清理：Qdrant + KG + chunks.jsonl + DB + raw 文件。
    """
    doc_id = str(request.args.get("doc_id", "")).strip()
    if not doc_id:
        return _err("doc_id is required", "DOC_ID_REQUIRED", 400)
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    try:
        result = _delete_document_full(kb, kb_id, doc_id)
    except RuntimeError as exc:
        msg = str(exc)
        if "not found" in msg:
            return _err(msg, "DOC_NOT_FOUND", 404)
        if "invalid file path" in msg:
            return _err(msg, "INVALID_PATH", 400)
        return _err(msg, "DELETE_FAILED", 500)
    return _ok(result)


@kb_bp.route(
    "/api/kb/<string:kb_id>/documents/<string:doc_id>",
    methods=["DELETE"],
)
def delete_document(kb_id: str, doc_id: str):
    """Phase 6.2: RESTful 单文档删除（完整清理）。"""
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    try:
        result = _delete_document_full(kb, kb_id, doc_id)
    except RuntimeError as exc:
        msg = str(exc)
        if "not found" in msg:
            return _err(msg, "DOC_NOT_FOUND", 404)
        if "invalid file path" in msg:
            return _err(msg, "INVALID_PATH", 400)
        return _err(msg, "DELETE_FAILED", 500)
    return _ok(result)


@kb_bp.route(
    "/api/kb/<string:kb_id>/documents/<string:doc_id>/reindex",
    methods=["POST"],
)
def reindex_document(kb_id: str, doc_id: str):
    """Phase 6.2: 单文档增量重建（不动其它文件）。"""
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    if DocumentRepository().get(kb_id, doc_id) is None:
        return _err(f"document not found: {doc_id}", "DOC_NOT_FOUND", 404)
    if _has_running_job(kb_id):
        return _err(f"kb has running job: {kb_id}", "KB_JOB_RUNNING", 409)

    job_id = new_id("job")
    JobRepository().create_ingest_job(
        job_id=job_id, tenant_id=kb["tenant_id"], kb_id=kb_id,
        payload={"partial": True, "doc_ids": [doc_id]},
        created_at=now_iso(),
    )
    _JOB_EXECUTOR.submit(
        _run_partial_ingest_job, dict(kb), kb_id, job_id, [doc_id],
    )
    return _ok({"job_id": job_id, "doc_id": doc_id, "status": "pending"})


@kb_bp.route(
    "/api/kb/<string:kb_id>/documents/batch-reindex",
    methods=["POST"],
)
def batch_reindex_documents(kb_id: str):
    """Phase 6.2: 批量增量重建。body: {"doc_ids": [...]}。"""
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    body = request.get_json(silent=True) or {}
    raw_ids = body.get("doc_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return _err("doc_ids must be a non-empty list", "INVALID_DOC_IDS", 400)
    doc_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    if not doc_ids:
        return _err("doc_ids must be a non-empty list", "INVALID_DOC_IDS", 400)

    doc_repo = DocumentRepository()
    for did in doc_ids:
        if doc_repo.get(kb_id, did) is None:
            return _err(f"document not found: {did}", "DOC_NOT_FOUND", 404)

    if _has_running_job(kb_id):
        return _err(f"kb has running job: {kb_id}", "KB_JOB_RUNNING", 409)

    job_id = new_id("job")
    JobRepository().create_ingest_job(
        job_id=job_id, tenant_id=kb["tenant_id"], kb_id=kb_id,
        payload={"partial": True, "doc_ids": doc_ids},
        created_at=now_iso(),
    )
    _JOB_EXECUTOR.submit(
        _run_partial_ingest_job, dict(kb), kb_id, job_id, doc_ids,
    )
    return _ok({"job_id": job_id, "doc_ids": doc_ids, "status": "pending"})


@kb_bp.route("/api/kb/<string:kb_id>/chunks", methods=["GET"])
def list_chunks(kb_id: str):
    """返回知识库的分块预览，用于 Phase 3 前端展示分块内容。

    查询参数：
      limit     int   每页数量，默认 20，最大 200
      offset    int   偏移量，默认 0
      max_chars int   preview 字段的最大字符数，默认 300
      doc       str   按文档名过滤（精确匹配 chunks 的 doc 字段）
    """
    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    chunks_path = Path(kb["data_path"]) / "corpora" / "chunks.jsonl"
    if not chunks_path.exists():
        return _ok([])

    try:
        max_chars = max(1, int(request.args.get("max_chars", 300)))
    except (ValueError, TypeError):
        max_chars = 300
    limit, offset = _parse_pagination(request.args)
    doc_filter = request.args.get("doc", "").strip() or None

    all_chunks = []
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except Exception:
            continue
        if doc_filter and chunk.get("doc") != doc_filter:
            continue
        contents = str(chunk.get("contents", ""))
        preview = contents[:max_chars] + ("..." if len(contents) > max_chars else "")
        images = chunk.get("images") or []
        all_chunks.append({
            "id": chunk.get("id", ""),
            "title": chunk.get("title", ""),
            "doc": chunk.get("doc", ""),
            "preview": preview,
            "image_count": len(images),
        })

    return _ok(all_chunks[offset: offset + limit])


@kb_bp.route("/api/kb/<string:kb_id>/jobs/<string:job_id>/progress", methods=["GET"])
def get_job_progress(kb_id: str, job_id: str):
    """返回 ingest 任务的阶段进度，用于 Phase 3 前端展示进度。

    响应字段：
      job_id       任务 ID
      status       pending/running/success/failed/cancelled
      stage        当前/最终阶段：parse/embed/index/done/pending
      stages_done  已完成的阶段列表
      chunk_count  索引的 chunk 数量（success 时有值）
      file_count   解析的文件数量（parse 完成后有值）
      error        错误信息（failed 时有值）
    """
    if not KbRepository().exists(kb_id):
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    row = JobRepository().get_for_kb(kb_id, job_id)

    if row is None:
        return _err(f"job not found: {job_id}", "JOB_NOT_FOUND", 404)

    try:
        result = json.loads(row["result_json"] or "{}")
    except Exception:
        result = {}

    status = str(row["status"])
    stages_done: list = result.get("stages_done", [])

    if status == "success":
        stage = "done"
    elif stages_done:
        stage = stages_done[-1]
    else:
        stage = "pending"

    progress = {
        "job_id": row["job_id"],
        "status": status,
        "stage": stage,
        "stages_done": stages_done,
        "chunk_count": result.get("chunk_count"),
        "file_count": result.get("file_count"),
    }
    # KG 阶段单独透传，便于前端 / 排障接口直接消费
    kg_status = result.get("kg_status")
    if kg_status is not None:
        progress["kg"] = {
            "status": kg_status,
            "entity_count": result.get("kg_entity_count"),
            "relation_count": result.get("kg_relation_count"),
            "chunk_count": result.get("kg_chunk_count"),
            "error_count": result.get("kg_error_count"),
            "error": result.get("kg_error") or result.get("kg_message") or "",
            "error_type": result.get("kg_error_type"),
        }
    if status == "failed":
        progress["error"] = str(row["last_error"] or "")

    return _ok(progress)


# ── diagnostics ──────────────────────────────────────────────────────────────

@kb_bp.route("/api/kb/<string:kb_id>/diagnostics", methods=["GET"])
def kb_diagnostics(kb_id: str):
    """一键排障端点：聚合 KB 的关键依赖状态。

    返回字段示例：
      {
        "kb_id": "gen_test",
        "kb_status": "active",
        "document_count": 3,
        "chunks_path_exists": true,
        "chunk_count": 13,
        "vector_backend": "qdrant",
        "vector_count": 13,
        "vector_error": null,
        "kg_backend": "neo4j",
        "kg_entity_count": 0,
        "kg_relation_count": 0,
        "kg_error": null,
        "kg_extract_enabled": true,
        "gemini_model": "gemini-3.1-pro-preview",
        "last_indexed_at": "...",
        "last_job": {"job_id": "...", "status": "success", "kg": {...}}
      }
    """
    kb_repo = KbRepository()
    kb = kb_repo.get(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    chunks_path = Path(kb["data_path"]) / "corpora" / "chunks.jsonl"
    chunk_count = 0
    if chunks_path.exists():
        try:
            chunk_count = sum(
                1 for line in chunks_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError as exc:
            logger.warning("diagnostics: read chunks.jsonl failed kb=%s: %s", kb_id, exc)

    vector_backend = (os.environ.get("ULTRARAG_VECTOR_BACKEND") or "faiss").strip().lower()
    vector_count: Optional[int] = None
    vector_error: Optional[str] = None
    if vector_backend == "qdrant":
        try:
            from custom_app.services.vectorstore.qdrant_store import QdrantVectorStore
            store = QdrantVectorStore(kb_id=kb_id, embed_dim=768)
            vector_count = int(store.size())
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {exc}"
    elif vector_backend == "faiss":
        try:
            idx_path = Path(kb["index_path"])
            vector_count = int(idx_path.stat().st_size > 0) if idx_path.exists() else 0
        except OSError as exc:
            vector_error = str(exc)

    kg_backend = (os.environ.get("ULTRARAG_KG_BACKEND") or "sqlite").strip().lower()
    kg_entity_count = 0
    kg_relation_count = 0
    kg_error: Optional[str] = None
    try:
        from custom_app.services.kg_search import get_graph_stats
        stats = get_graph_stats(kb_id) or {}
        kg_entity_count = int(stats.get("entity_count") or 0)
        kg_relation_count = int(stats.get("relation_count") or 0)
    except Exception as exc:  # noqa: BLE001
        kg_error = f"{type(exc).__name__}: {exc}"

    last_jobs = JobRepository().list_for_kb(kb_id, limit=1, offset=0)
    last_job_summary: Optional[dict] = None
    if last_jobs:
        decorated = _decorate_job_row(dict(last_jobs[0]))
        if decorated:
            res = decorated.get("result") or {}
            last_job_summary = {
                "job_id": decorated.get("job_id"),
                "status": decorated.get("status"),
                "summary": decorated.get("summary"),
                "finished_at": decorated.get("finished_at"),
                "kg_status": res.get("kg_status"),
                "kg_message": res.get("kg_message") or res.get("kg_error"),
            }

    payload = {
        "kb_id": kb_id,
        "kb_status": kb.get("status"),
        "document_count": kb.get("document_count"),
        "last_indexed_at": kb.get("last_indexed_at"),
        "chunks_path_exists": chunks_path.exists(),
        "chunk_count": chunk_count,
        "vector_backend": vector_backend,
        "vector_count": vector_count,
        "vector_error": vector_error,
        "kg_backend": kg_backend,
        "kg_extract_enabled": _should_extract_kg(kb_id),
        "kg_entity_count": kg_entity_count,
        "kg_relation_count": kg_relation_count,
        "kg_error": kg_error,
        "gemini_model": os.environ.get("ULTRARAG_GEMINI_MODEL", "gemini-2.0-flash"),
        "last_job": last_job_summary,
    }
    return _ok(payload)


# ── agent tool config ────────────────────────────────────────────────────────

_TOOL_LABELS = {
    "knowledge_search": "搜索知识库（语义向量）",
    "keyword_search": "文本关键词搜索",
    "list_knowledge_chunks": "阅读文档完整内容（Deep Read）",
    "query_knowledge_graph": "知识图谱查询（实体关系）",
    "final_answer": "提交最终答案",
}


def _all_tools_metadata() -> list[dict]:
    from custom_app.services.agent_config_store import ALL_TOOLS, REQUIRED_TOOLS
    required = set(REQUIRED_TOOLS)
    return [
        {
            "name": t,
            "label": _TOOL_LABELS.get(t, t),
            "required": t in required,
        }
        for t in ALL_TOOLS
    ]


@kb_bp.route("/api/kb/<string:kb_id>/agent_config", methods=["GET"])
def get_agent_config(kb_id: str):
    """读取某 KB 的 Agent 工具启用配置。未配置时返回默认值（全部启用）。"""
    from custom_app.services.agent_config_store import get_enabled_tools
    return _ok({
        "kb_id": kb_id,
        "enabled_tools": get_enabled_tools(kb_id),
        "all_tools": _all_tools_metadata(),
    })


@kb_bp.route("/api/kb/<string:kb_id>/agent_config", methods=["PUT"])
def put_agent_config(kb_id: str):
    """更新某 KB 的 Agent 工具启用配置。"""
    from custom_app.services.agent_config_store import set_enabled_tools

    data = request.get_json(silent=True) or {}
    tools = data.get("enabled_tools")
    if not isinstance(tools, list):
        return _err("enabled_tools 必须是字符串数组", "invalid_payload", 400)

    try:
        normalized = set_enabled_tools(kb_id, tools)
    except ValueError as exc:
        return _err(str(exc), "invalid_kb_id", 400)

    return _ok({
        "kb_id": kb_id,
        "enabled_tools": normalized,
        "all_tools": _all_tools_metadata(),
    })
