# 文件说明：Phase 1 会话落库的轻量访问层，供 REST 与流式对话完成后追加消息使用。
# Phase 5.1.7：通过 SessionRepository 访问 DB，原生 SQL 已全部迁移。
from __future__ import annotations

import json
from typing import Any, List, Optional

from custom_app.db import new_id, now_iso
from custom_app.repositories import SessionRepository


def _safe_dump_reasoning(value: Any) -> str:
    """非 dict 一律落库为 '{}'，防止上游传错类型污染历史。"""
    if not isinstance(value, dict):
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _safe_load_reasoning(raw: Any) -> dict:
    """容错解析：损坏 JSON / 非对象返回空 dict。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def create_session(
    kb_id: str, *, title: str = "", agent_mode: str = "quick"
) -> dict[str, Any]:
    """创建空会话并返回记录字段。"""
    sid = new_id("sess")
    ts = now_iso()
    t = (title or "").strip() or "新对话"
    am = (agent_mode or "quick").strip().lower()
    if am not in ("quick", "agent"):
        am = "quick"
    SessionRepository().create_session(
        session_id=sid, kb_id=kb_id, title=t, agent_mode=am, created_at=ts,
    )
    return {
        "session_id": sid,
        "kb_id": kb_id,
        "title": t,
        "agent_mode": am,
        "created_at": ts,
        "updated_at": ts,
    }


def list_sessions_for_kb(kb_id: str, *, limit: int = 100) -> List[dict[str, Any]]:
    """按更新时间倒序列出某知识库下的会话。"""
    return SessionRepository().list_sessions_for_kb(kb_id, limit=limit)


def get_session(session_id: str) -> dict[str, Any] | None:
    """按 id 取会话一行；不存在返回 None。"""
    return SessionRepository().get_session(session_id)


def list_messages(session_id: str) -> List[dict[str, Any]]:
    """列出会话内消息（含反序列化的 reasoning 字段），按插入顺序。"""
    raw_msgs = SessionRepository().list_messages(session_id)
    rows = []
    for r in raw_msgs:
        d = dict(r)
        d["reasoning"] = _safe_load_reasoning(d.pop("reasoning_json", None))
        d["session_id"] = session_id  # Repository 输出未含 session_id
        rows.append(d)
    return rows


def update_session_title(session_id: str, title: str) -> bool:
    """更新会话标题；成功返回 True。"""
    t = (title or "").strip()
    if not t:
        return False
    # Repository 的 update_title 接口固定截断 title[:500]；这里保持现有 ts 行为
    repo = SessionRepository()
    # 先检查存在性（保留原 rowcount>0 语义）
    if repo.get_session(session_id) is None:
        return False
    repo.update_title(session_id, title=t, updated_at=now_iso())
    return True


def delete_session(session_id: str) -> bool:
    """删除会话及其所有消息；成功返回 True。"""
    sid = (session_id or "").strip()
    if not sid:
        return False
    return SessionRepository().delete_session(sid)


def append_chat_turn(
    session_id: str,
    kb_id: str,
    user_text: str,
    assistant_text: str,
    *,
    agent_mode: str = "quick",
    reasoning_for_assistant: Optional[dict] = None,
) -> bool:
    """
    在已成功完成的流式对话后写入一轮 user/assistant 消息，并刷新标题（首条时）。

    参数:
        session_id: 会话 id。
        kb_id: 必须与该行一致，防止串库。
        user_text: 用户问题原文。
        assistant_text: 助手最终展示文本（与 SSE done.answer 对齐）。
        agent_mode: 本轮使用的模式（quick / agent），同步更新会话的 agent_mode 字段。
        reasoning_for_assistant: 仅对 assistant 消息生效的推理元数据 dict。
            非 dict 或解析失败一律落 {}。

    返回:
        是否写入成功（会话不存在或 kb 不匹配时为 False）。
    """
    am = (agent_mode or "quick").strip().lower()
    if am not in ("quick", "agent"):
        am = "quick"
    reasoning_blob = _safe_dump_reasoning(reasoning_for_assistant)

    repo = SessionRepository()
    row = repo.get_session_kb_and_title(session_id)
    if row is None or row.get("kb_id") != kb_id:
        return False
    prev_title = (row.get("title") or "").strip()

    ts1 = now_iso()
    repo.append_user_message(session_id, content=user_text, created_at=ts1)
    ts2 = now_iso()
    repo.append_assistant_message(
        session_id, content=assistant_text,
        reasoning_json=reasoning_blob, created_at=ts2,
    )
    new_title = prev_title
    if (not prev_title or prev_title == "新对话") and user_text.strip():
        new_title = user_text.strip()[:120]
    repo.update_title_and_mode(
        session_id, title=new_title, agent_mode=am, updated_at=ts2,
    )
    return True
