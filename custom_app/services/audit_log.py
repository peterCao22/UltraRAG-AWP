"""Phase 11.1.2（最小版）问答审计日志服务。

主入口：``log_qa(...)`` —— done 后调用，记一条 event_type='qa' 审计行。

设计约束：
    - **静默降级**：任何异常都不抛回主链路；最多 logger.warning 一行
    - **不阻塞主对话**：调用方应在 done 事件 yield 给客户端**之后**触发
    - 不做异步队列 / 不引 Redis；首版直接同步 INSERT，DB 故障时丢弃该条
    - 后续若量级（>>1 QPS）或写延迟成为瓶颈，再迁移到 buffer + flush
"""

from __future__ import annotations

import json
import logging
from typing import Any

from custom_app.db import now_iso
from custom_app.repositories.audit_repository import AuditRepository

logger = logging.getLogger(__name__)

EVENT_QA = "qa"


def log_qa(
    *,
    kb_id: str,
    session_id: str | None,
    query: str,
    answer: str,
    chunk_ids: list[str] | None = None,
    tenant_id: str = "default",
    extra_meta: dict[str, Any] | None = None,
) -> bool:
    """记一条 event_type='qa' 审计行。

    参数:
        kb_id:      问答所在知识库；空字符串时仍记录（但 kb_id 列为空）
        session_id: 会话 id；无会话场景（直接 chat_markdown）传 None
        query:      用户原文 query（完整保留，未脱敏）
        answer:     系统最终展示答案（display Markdown 文本）
        chunk_ids:  retrieved 命中的 chunk_id 列表
        tenant_id:  Phase 10 多租户用；当前固定 'default'
        extra_meta: 额外 meta dict（如 agent_mode / latency_ms / model_name）

    返回:
        True 写库成功；False 失败已被静默吞掉
    """
    try:
        chunk_ids_json = json.dumps(chunk_ids or [], ensure_ascii=False)
        meta_json = json.dumps(extra_meta or {}, ensure_ascii=False)
        AuditRepository().append(
            ts=now_iso(),
            event_type=EVENT_QA,
            kb_id=kb_id or "",
            session_id=session_id or "",
            tenant_id=tenant_id or "default",
            query=query or "",
            answer=answer or "",
            chunk_ids_json=chunk_ids_json,
            meta_json=meta_json,
        )
        return True
    except Exception as e:  # noqa: BLE001 — 审计失败不阻塞主对话
        logger.warning(
            "audit_log.log_qa failed kb=%s session=%s: %s",
            kb_id, session_id, e,
        )
        return False
