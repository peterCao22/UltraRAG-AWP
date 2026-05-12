import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
    DocumentRepository().mark_pending_failed(kb_id, error=str(exc), updated_at=now)


def _run_ingest_job(kb: dict, kb_id: str, job_id: str, force_reindex: bool) -> dict:
    """执行入库任务的完整三阶段流程。"""
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
        _parse_stage(kb, raw_dir, kb_root, chunks_path)
        _update_job_stage(job_id, "parse", {"file_count": file_count})
        _embed_stage(chunks_path, embedding_path)
        _update_job_stage(job_id, "embed")
        chunk_count = _index_stage(chunks_path, embedding_path, index_path)
        _update_job_stage(job_id, "index", {"chunk_count": chunk_count})
        _mark_job_success(job_id, kb_id, chunk_count, chunks_path, embedding_path, index_path, force_reindex)
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
        doc_repo.upsert(
            kb_id=kb_id, tenant_id=kb["tenant_id"],
            doc_id=f"{kb_id}:{safe_name}", file_name=safe_name,
            file_type=ext.lstrip("."), file_path=str(dest),
            channel="web", status="uploaded", updated_at=now_iso(),
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
    limit, offset = _parse_pagination(request.args)
    if not KbRepository().exists(kb_id):
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)
    rows = DocumentRepository().list_for_kb(kb_id, limit=limit, offset=offset)
    return _ok(rows)


@kb_bp.route("/api/kb/<string:kb_id>/documents", methods=["DELETE"])
def delete_document(kb_id: str):
    """删除一条文档记录及其 raw 目录下的源文件（路径必须在知识库 data_path 之下）。"""
    doc_id = str(request.args.get("doc_id", "")).strip()
    if not doc_id:
        return _err("doc_id is required", "DOC_ID_REQUIRED", 400)

    kb = KbRepository().get_basic(kb_id)
    if kb is None:
        return _err(f"kb not found: {kb_id}", "KB_NOT_FOUND", 404)

    doc_repo = DocumentRepository()
    row = doc_repo.get(kb_id, doc_id)
    if row is None:
        return _err(f"document not found: {doc_id}", "DOC_NOT_FOUND", 404)

    kb_root = Path(kb["data_path"]).resolve()
    fp = Path(row["file_path"])
    try:
        resolved = fp.resolve()
        resolved.relative_to(kb_root)
    except (OSError, ValueError):
        return _err("invalid file path", "INVALID_PATH", 400)

    doc_repo.delete(kb_id, doc_id)

    if fp.exists():
        try:
            fp.unlink()
        except OSError:
            pass

    return _ok({"doc_id": doc_id, "deleted": True})


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
    if status == "failed":
        progress["error"] = str(row["last_error"] or "")

    return _ok(progress)


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
