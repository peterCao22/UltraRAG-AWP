"""Phase 6.1: 启动时的"卡死恢复"。

Flask 进程崩溃后，kb_documents 表里可能残留 parsing/embedding/indexing/
deleting 状态的行。这些 ingest 流程实际已经死掉，但行还停在中间态，前端
会一直转圈圈。

每次 create_app() 启动调用 recover_stale_documents()，把超过 N 分钟仍未
更新的中间态行标 failed + 写入解释性 error_message。

阈值默认 10 分钟（可经 ULTRARAG_DOC_STALE_MINUTES 覆盖）；只看
kb_documents.updated_at（in-flight 时 _broadcast_doc_status 会持续刷新）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from custom_app.db import now_iso
from custom_app.repositories import DocumentRepository

logger = logging.getLogger(__name__)


DEFAULT_STALE_MINUTES = 10
STALE_ERROR_MESSAGE = "进程异常中断，请重试"


def _stale_minutes() -> int:
    raw = os.environ.get("ULTRARAG_DOC_STALE_MINUTES", "").strip()
    if not raw:
        return DEFAULT_STALE_MINUTES
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_STALE_MINUTES
    except ValueError:
        return DEFAULT_STALE_MINUTES


def _threshold_iso(now: datetime | None = None, *, minutes: int | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    mins = minutes if minutes is not None else _stale_minutes()
    return (now - timedelta(minutes=mins)).isoformat()


def recover_stale_documents(*, minutes: int | None = None) -> int:
    """把超过 minutes 分钟仍在 processing 的文档标 failed。

    返回处理的行数。失败时只记 warning，不抛——这是启动钩子，不能挡 Flask。
    """
    try:
        repo = DocumentRepository()
        threshold = _threshold_iso(minutes=minutes)
        stale = repo.find_stale_processing(threshold_iso=threshold)
        if not stale:
            return 0
        now = now_iso()
        for row in stale:
            try:
                repo.update_document_status(
                    row["kb_id"], row["doc_id"],
                    status="failed",
                    updated_at=now,
                    error_message=STALE_ERROR_MESSAGE,
                )
            except Exception:
                logger.exception(
                    "recover_stale_documents: failed to mark kb=%s doc=%s",
                    row.get("kb_id"), row.get("doc_id"),
                )
        logger.info(
            "recover_stale_documents: marked %d stale documents as failed",
            len(stale),
        )
        return len(stale)
    except Exception:
        logger.exception("recover_stale_documents crashed; ignored at startup")
        return 0
