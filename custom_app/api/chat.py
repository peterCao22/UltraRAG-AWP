import json
import logging
import os
import threading

from flask import Blueprint, Response, jsonify, request, stream_with_context

from custom_app.services.rag_runner import RagRunner
from custom_app.services.session_store import (
    append_chat_turn as persist_chat_turn,
    list_messages as list_messages_for_agent,
)

# ============================================================
# Phase 7 backlog: 多模型切换支持（参考 WeKnora）
# ------------------------------------------------------------
# 目标：让用户在前端会话顶部下拉切换"思考/对话模型"，例如：
#   - gpt-oss-120b（OpenAI 兼容/vLLM 后端）
#   - claude-haiku-4-5
#   - gemini-2.0-flash（当前默认）
#
# 实施要点（按落地顺序）：
# 1. 后端配置（servers/generation/parameter.yaml 或 custom_app 专属配置）：
#       chat_models:
#         - id: gemini-2.0-flash
#           backend: gemini
#           env_required: [GOOGLE_API_KEY]
#         - id: gpt-oss-120b
#           backend: openai_compat
#           base_url: https://...
#           env_required: [ULTRARAG_OPENAI_API_KEY]
# 2. 新增 GET /api/chat/models 列表端点；前端 admin/chat 页缓存。
# 3. POST /api/chat/stream 接收 model_id 字段；按 id 在 LLMAdapter 工厂里
#    选择 GeminiLLMAdapter / OpenAICompatAdapter 等具体实现。
# 4. AgentRunner.__init__ 增加 model_id 参数，_runners/_agent_runners 池
#    要按 (kb_id, model_id) 联合 key 缓存，避免不同模型互踩状态。
# 5. messages_to_gemini_contents 等适配函数当前只服务 Gemini，OpenAI 兼容
#    后端需要另一套 messages 转换（OpenAI 标准 tool_calls/tool role 已满足）。
# 6. 前端 chat.html / admin.html 加模型选择器（参考用户截图样式：远程 / 本地标签）。
# ============================================================

chat_bp = Blueprint("chat_api", __name__)
logger = logging.getLogger(__name__)

# Phase 7 / 7.2.A: 按 (kb_id, model_id, agent_id) 缓存 Runner。
# - model_id="" 表示走 .env 默认（兼容老路径）
# - agent_id="" 表示无 agent_config 行（兼容无 agent_configs 表的部署）
_runners: dict[tuple[str, str, str], RagRunner] = {}
_runners_lock = threading.Lock()

# AgentRunner 实例池（agent_mode=agent 时使用）
_agent_runners: dict[tuple[str, str, str], "AgentRunner"] = {}  # type: ignore[name-defined]
_agent_runners_lock = threading.Lock()


def _runner_key(
    kb_id: str, model_id: str | None, agent_id: str | None = None
) -> tuple[str, str, str]:
    return (kb_id, model_id or "", agent_id or "")


def _check_kb_access(kb_id: str, level: str = "read") -> tuple[bool, str]:
    """P-Perm C7：校验当前请求是否有 kb_id 的访问权限。

    返回 (allowed, reason)；reason 仅在 not allowed 时有意义，便于上层选 4xx/JSON 还是 SSE error 帧。

    通过条件（任一）：
        - ULTRARAG_AUTH_REQUIRED=0（开发模式全开）
        - 带合法 X-Admin-Token（运维通道）
        - 已登录用户且 username == 'admin'（超管语义）
        - 已登录用户且 user_has_kb_permission(uid, kb_id, level)
    """
    import hmac

    auth_off = os.environ.get(
        "ULTRARAG_AUTH_REQUIRED", "1",
    ).strip().lower() in ("0", "false", "no", "off")
    if auth_off:
        return True, ""

    expected = os.getenv("ULTRARAG_ADMIN_TOKEN", "").strip()
    presented = (request.headers.get("X-Admin-Token", "") or "").strip()
    if expected and presented and hmac.compare_digest(presented, expected):
        return True, ""

    try:
        from custom_app.repositories import UserRepository
        from custom_app.services.auth import current_user
    except Exception:  # noqa: BLE001
        return False, "auth subsystem unavailable"

    u = current_user() or {}
    if not u:
        return False, "login required"
    if (u.get("username") or "") == "admin":
        return True, ""
    uid = str(u.get("user_id") or "")
    if not uid:
        return False, "login required"
    if UserRepository().user_has_kb_permission(
        user_id=uid, kb_id=kb_id, min_level=level,
    ):
        return True, ""
    return False, f"no '{level}' permission on kb {kb_id}"


def _load_chat_model_row(model_id: str | None) -> dict | None:
    """Phase 7.1: 从 ChatModelRepository 查模型 row；找不到返回 None（runner 走老 .env 路径）。"""
    if not model_id:
        return None
    try:
        from custom_app.repositories import ChatModelRepository
        return ChatModelRepository().get(model_id)
    except Exception:
        logger.exception("load chat_model row failed model_id=%s", model_id)
        return None


def _load_agent_config_row(
    agent_id: str | None, agent_mode: str
) -> dict | None:
    """Phase 7.2.A: 从 ChatAgentRepository 查 agent 行。

    缺省 agent_id 时按 agent_mode 取 builtin-quick / builtin-agent；
    查不到时返回 None（runner 走 yaml 兜底，向后兼容无 agent_configs 表的部署）。
    """
    try:
        from custom_app.repositories import ChatAgentRepository
        repo = ChatAgentRepository()
        if agent_id:
            row = repo.get(agent_id)
            if row is not None:
                return row
            logger.info(
                "agent_id=%s not found; fallback to builtin by agent_mode=%s",
                agent_id, agent_mode,
            )
        if agent_mode == "agent":
            return repo.get_builtin_agent()
        return repo.get_builtin_quick()
    except Exception:
        logger.exception(
            "load agent_config row failed agent_id=%s agent_mode=%s",
            agent_id, agent_mode,
        )
        return None


def _resolve_request_agent_id(data: dict) -> str | None:
    """从请求 body 取 agent_id；返回空串视为缺省。"""
    raw = str(data.get("agent_id", "") or "").strip()
    return raw or None


def _get_runner(
    kb_id: str,
    model_id: str | None = None,
    agent_id: str | None = None,
    agent_mode: str = "quick",
) -> RagRunner:
    key = _runner_key(kb_id, model_id, agent_id)
    with _runners_lock:
        if key not in _runners:
            r = RagRunner(
                kb_id=kb_id,
                chat_model=_load_chat_model_row(model_id),
                agent_config=_load_agent_config_row(agent_id, agent_mode),
            )
            r.init()
            _runners[key] = r
        return _runners[key]


def _get_agent_runner(
    kb_id: str,
    model_id: str | None = None,
    agent_id: str | None = None,
    agent_mode: str = "agent",
):
    """返回与 (kb_id, model_id, agent_id) 绑定的 AgentRunner。"""
    from custom_app.services.agent_runner import AgentRunner

    key = _runner_key(kb_id, model_id, agent_id)
    with _agent_runners_lock:
        if key not in _agent_runners:
            # 复用 RagRunner 已加载的 rows / vector_store，避免二次磁盘读取
            rag = _get_runner(kb_id, model_id, agent_id, agent_mode=agent_mode)
            ar = AgentRunner(
                kb_id=kb_id,
                chat_model=_load_chat_model_row(model_id),
                agent_config=_load_agent_config_row(agent_id, agent_mode),
            )
            ar.init(
                rows=rag._rows,
                index=rag._index,
                kb_name=kb_id,
                vector_store=getattr(rag, "_vector_store", None),
                # 复用 RagRunner 的 _build_sources：它知道如何把图片转 base64 data URL，
                # agent 模式的最终答案才能挂图（否则 SSE 只发文本）。
                source_builder=rag._build_sources,
            )
            _agent_runners[key] = ar
        return _agent_runners[key]


def invalidate_runner_cache(kb_id: str) -> None:
    """重建索引后必须调用，否则 RagRunner / AgentRunner 仍持有旧的 rows / FAISS 引用，
    新上传的文档查不到、被删除的文档可能仍在召回。

    Phase 7 / 7.2.A: 失效该 kb_id 下所有 (model_id, agent_id) 组合的缓存。
    """
    with _runners_lock:
        for key in [k for k in _runners if k[0] == kb_id]:
            _runners.pop(key, None)
    with _agent_runners_lock:
        for key in [k for k in _agent_runners if k[0] == kb_id]:
            _agent_runners.pop(key, None)
    logger.info("invalidate_runner_cache kb_id=%s: runners evicted", kb_id)


def _compact_reasoning_event(event: dict) -> dict:
    """从 SSE 事件抽取最少必要字段用于历史回放，避免存储大段文本。"""
    et = event.get("type", "")
    out: dict = {"type": et}
    if et == "thought":
        text = str(event.get("content") or "")
        out["content"] = text[:500]
    elif et == "tool_call":
        out["tool_name"] = event.get("tool_name", "")
        out["hint"] = str(event.get("hint") or "")[:200]
    elif et == "tool_result":
        out["tool_name"] = event.get("tool_name", "")
        out["summary"] = str(event.get("summary") or "")[:200]
        if isinstance(event.get("duration_ms"), int):
            out["duration_ms"] = event["duration_ms"]
        # 落库前对 details 再做保护性截断（最长 2000 字符），防止 SSE 上游
        # 截断阈值变大时拖累 SQLite 存储和后续 list_messages 反序列化。
        details = event.get("details")
        if isinstance(details, str) and details:
            out["details"] = details[:2000]
    return out


def _result_to_markdown(question: str, result: dict) -> str:
    lines = [
        "# AGV RAG Answer",
        "",
        f"**Question**: {question}",
        "",
        "## Answer",
        "",
    ]
    answer_blocks = result.get("answer_blocks", []) or []
    if answer_blocks:
        for block in answer_blocks:
            if block.get("type") == "text":
                lines.append(block.get("content", ""))
                lines.append("")
            elif block.get("type") == "image":
                title = block.get("title", "")
                s_idx = int(block.get("source_idx", 0)) + 1
                i_idx = int(block.get("image_idx", 0)) + 1
                lines.append(
                    f"*Evidence image from Source {s_idx} ({title}), Image {i_idx}:*"
                )
                lines.append("")
                lines.append(
                    f"![source-{s_idx}-image-{i_idx}]({block.get('data_url', '')})"
                )
                lines.append("")
    else:
        lines.append(result.get("answer", ""))
        lines.append("")

    lines.append("## Sources")
    lines.append("")
    sources = result.get("sources", []) or []
    if not sources:
        lines.append("_No sources returned._")
        return "\n".join(lines)

    for idx, src in enumerate(sources, 1):
        title = src.get("title", "") or "(untitled)"
        doc = (src.get("doc") or "").strip()
        head = f"Source {idx}: {title}"
        if doc:
            head = f"Source {idx}: [{doc}] {title}"
        body = (src.get("excerpt") or src.get("snippet") or "").strip()
        images = src.get("images", []) or []
        lines.append(f"### {head}")
        lines.append("")
        lines.append(body if body else "_（无正文）_")
        lines.append("")
        lines.append(f"- Images: {len(images)}")
        for j, img in enumerate(images, 1):
            lines.append(f"  - Image {j}:")
            lines.append(f"    ![source-{idx}-image-{j}]({img})")
        lines.append("")

    return "\n".join(lines)


@chat_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@chat_bp.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    kb_id = str(data.get("kb_id", "agv_demo")).strip() or "agv_demo"
    question = str(data.get("question", "")).strip()
    top_k = data.get("top_k")

    if not question:
        return jsonify({"error": "question 不能为空"}), 400

    # P-Perm C7：必须对该 kb 有 read 权限
    allowed, reason = _check_kb_access(kb_id, level="read")
    if not allowed:
        return jsonify({"error": reason or "forbidden", "code": "KB_FORBIDDEN"}), 403

    agent_mode = str(data.get("agent_mode", "quick")).strip().lower()
    if agent_mode not in ("quick", "agent"):
        agent_mode = "quick"

    # Phase 8.3: mode 控制策略 quick / deep_reasoning（IRCoT）
    # 与 agent_mode（quick / agent，控制 ReAct 工具调用）互不冲突
    mode = str(data.get("mode", "quick")).strip().lower()
    if mode not in ("quick", "deep_reasoning"):
        mode = "quick"
    ircot_max_loops = int(data.get("ircot_max_loops", 2) or 2)
    ircot_max_loops = max(1, min(ircot_max_loops, 5))  # 安全范围

    agent_id = _resolve_request_agent_id(data)
    model_id = _resolve_request_model_id(data, agent_id=agent_id, agent_mode=agent_mode)
    try:
        runner = _get_runner(kb_id, model_id, agent_id, agent_mode=agent_mode)
        if mode == "deep_reasoning":
            result = runner.chat_ircot(
                question=question, top_k=top_k, max_loops=ircot_max_loops,
            )
        else:
            result = runner.chat(question=question, top_k=top_k, agent_mode=agent_mode)
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _resolve_request_model_id(
    data: dict,
    *,
    agent_id: str | None = None,
    agent_mode: str = "quick",
) -> str | None:
    """Phase 7 / 7.2.A: 从请求 body 取 model_id。

    优先级：
        1. body.model_id（前端 chip 选的）
        2. agent_config.model_id（agent 绑定了 model 时联动切换）
        3. ChatModelRepository.get_default()（admin 设的默认模型）
        4. None（走 .env 路径，与老行为一致）
    """
    explicit = str(data.get("model_id", "")).strip()
    if explicit:
        return explicit
    if agent_id is not None or agent_mode:
        try:
            agent_row = _load_agent_config_row(agent_id, agent_mode)
            if agent_row:
                from_agent = str(agent_row.get("model_id") or "").strip()
                if from_agent:
                    return from_agent
        except Exception:
            logger.exception("resolve agent_config.model_id failed")
    try:
        from custom_app.repositories import ChatModelRepository
        default = ChatModelRepository().get_default()
        if default:
            return default["model_id"]
    except Exception:
        logger.exception("resolve default model_id failed; fallback to env")
    return None


@chat_bp.route("/api/chat/agents", methods=["GET"])
def get_chat_agents():
    """Phase 7.2.A: 对话页 agent_select dropdown 用。

    仅返回 enabled=true 的 agent；字段最小（不含 prompt / api_key），
    避免暴露管理员配置给普通用户。
    """
    try:
        from custom_app.repositories import ChatAgentRepository
        rows = ChatAgentRepository().list_active(include_disabled=False)
    except Exception:
        logger.exception("list chat agents failed; returning empty")
        rows = []

    out = []
    for r in rows:
        out.append(
            {
                "agent_id": r["agent_id"],
                "name": r["name"],
                "agent_mode": r["agent_mode"],
                "avatar": r.get("avatar", ""),
                "description": r.get("description", ""),
                "is_builtin": bool(r.get("is_builtin", False)),
                "model_id": r.get("model_id") or "",
            }
        )
    return jsonify({"data": out})


@chat_bp.route("/api/chat/models", methods=["GET"])
def get_chat_models():
    """Phase 7: 对话页 chip 下拉用；仅返回 enabled=true 的模型，不返回 api_key/base_url。"""
    from custom_app.repositories import ChatModelRepository
    rows = ChatModelRepository().list_active(include_disabled=False)
    out = []
    for r in rows:
        out.append({
            "model_id": r["model_id"],
            "name": r["name"],
            "provider": r["provider"],
            "model_name": r["model_name"],
            "is_default": r["is_default"],
            "description": r.get("description", ""),
        })
    return jsonify({"data": out})


@chat_bp.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """
    SSE 流式问答接口。

    分阶段耗时（Phase P）：JSON ``profile: true``、请求头 ``X-Ultrarag-Profile: 1``、
    或环境变量 ``ULTRARAG_CHAT_PROFILE=1`` 时，在 ``type=meta`` 事件中附带 ``phase_timings_ms``。

    ``agent_mode``: ``quick`` | ``agent``（层 A 全文扩展，见 RagRunner）。
    ``session_id``: 若提供且在落库范围内，流正常结束后写入该会话的用户/助手消息。
    """
    data = request.get_json(silent=True) or {}
    kb_id = str(data.get("kb_id", "agv_demo")).strip() or "agv_demo"
    question = str(data.get("question", "")).strip()
    top_k = data.get("top_k")
    agent_mode = str(data.get("agent_mode", "quick")).strip().lower()
    if agent_mode not in ("quick", "agent"):
        agent_mode = "quick"
    # Phase 8.3: mode 控制策略（quick / deep_reasoning IRCoT）
    mode = str(data.get("mode", "quick")).strip().lower()
    if mode not in ("quick", "deep_reasoning"):
        mode = "quick"
    ircot_max_loops = int(data.get("ircot_max_loops", 2) or 2)
    ircot_max_loops = max(1, min(ircot_max_loops, 5))
    session_id_opt = str(data.get("session_id", "")).strip() or None
    profile = bool(data.get("profile"))
    if str(os.environ.get("ULTRARAG_CHAT_PROFILE", "")).lower() in ("1", "true", "yes"):
        profile = True
    if (request.headers.get("X-Ultrarag-Profile") or "").strip() == "1":
        profile = True

    if not question:
        return jsonify({"error": "question 不能为空"}), 400

    # P-Perm C7：必须对该 kb 有 read 权限才能问
    allowed, reason = _check_kb_access(kb_id, level="read")
    if not allowed:
        logger.warning(
            "chat_stream forbidden: kb=%s reason=%s", kb_id, reason,
        )
        return jsonify({
            "error": reason or "forbidden",
            "code": "KB_FORBIDDEN",
        }), 403

    def generate():
        accumulated: list[str] = []
        final_answer = ""
        # 仅 agent 模式收集；quick 模式 reasoning_events 保持空，落库为 {}
        reasoning_events: list[dict] = []
        reasoning_meta: dict = {}
        # Phase 12.2: done 事件 yield 完毕后再触发 session memory 摘要（防阻塞 SSE）
        _should_summarize = False
        # Phase 11.1.2（最小版）：累计本轮命中的 chunk_id，done 后写审计
        qa_chunk_ids: list[str] = []
        qa_meta: dict = {}
        _should_audit = False
        try:
            # 在加载 FAISS/语料之前先发 SSE，避免客户端长时间 0 字节（误以为卡死）。
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "status",
                        "content": "正在加载知识库索引（首次访问可能需数十秒）…",
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            agent_id = _resolve_request_agent_id(data)
            model_id = _resolve_request_model_id(
                data, agent_id=agent_id, agent_mode=agent_mode
            )
            if agent_mode == "agent":
                logger.info(
                    "chat_stream routing → AgentRunner kb_id=%s session_id=%s model_id=%s agent_id=%s",
                    kb_id, session_id_opt, model_id, agent_id,
                )
                runner = _get_agent_runner(
                    kb_id, model_id, agent_id, agent_mode=agent_mode
                )
                # 按 KB 配置动态调整启用工具，让 admin 调整后立即生效（不必重建 runner）
                from custom_app.services.agent_config_store import get_enabled_tools
                runner.enabled_tools = get_enabled_tools(kb_id)
                history: list = []
                if session_id_opt:
                    try:
                        history = list_messages_for_agent(session_id_opt)
                    except Exception:
                        logger.exception("list_messages_for_agent failed, proceeding without history")
                event_iter = runner.chat_stream(
                    question=question, top_k=top_k, profile=profile,
                    history=history, session_id=session_id_opt,
                )
            elif mode == "deep_reasoning":
                # Phase 8.3 IRCoT 路径：非流式调用包装成 SSE。
                # 思考过程作为 thought 事件推送给前端展示推理可见。
                logger.info(
                    "chat_stream routing → RagRunner.chat_ircot kb_id=%s loops=%d model_id=%s",
                    kb_id, ircot_max_loops, model_id,
                )
                runner = _get_runner(kb_id, model_id, agent_id, agent_mode="quick")
                # Phase 12.1: 拉历史给 IRCoT 做指代消解
                ircot_history: list = []
                if session_id_opt:
                    try:
                        ircot_history = list_messages_for_agent(session_id_opt)
                    except Exception:
                        logger.exception(
                            "list_messages_for_agent failed (ircot), proceeding without history"
                        )

                def _ircot_to_sse():
                    yield {"type": "status", "content": f"深度思考模式：将多轮检索，最多 {ircot_max_loops} 轮…"}
                    result = runner.chat_ircot(
                        question=question, top_k=top_k, max_loops=ircot_max_loops,
                        history=ircot_history,
                    )
                    # Phase 12.1: 指代消解若采纳，先发事件给前端可视化
                    ref_meta = (result.get("meta") or {}).get("reference_resolution") or {}
                    if ref_meta.get("applied"):
                        yield {"type": "reference_resolution", **ref_meta}
                    # 把每轮思考作为 thought 事件依次推（前端可视化推理过程）
                    for i, thought in enumerate(result.get("thoughts") or [], start=1):
                        yield {
                            "type": "thought",
                            "content": f"【第 {i} 轮思考】{thought}",
                        }
                    # 最终答案一次性 chunk
                    display = str(result.get("answer") or "")
                    if display:
                        yield {"type": "chunk", "content": display}
                    sources = result.get("sources") or []
                    if sources:
                        yield {"type": "sources", "sources": sources}
                    meta_event = {
                        "type": "meta",
                        "kb_id": kb_id,
                        "meta": result.get("meta", {}),
                    }
                    yield meta_event
                    yield {"type": "done", "answer": display}

                event_iter = _ircot_to_sse()
            else:
                logger.info(
                    "chat_stream routing → RagRunner kb_id=%s agent_mode=%s model_id=%s agent_id=%s",
                    kb_id, agent_mode, model_id, agent_id,
                )
                runner = _get_runner(kb_id, model_id, agent_id, agent_mode=agent_mode)
                # Phase 12.1: quick 路径也拉历史给指代消解用
                quick_history: list = []
                if session_id_opt:
                    try:
                        quick_history = list_messages_for_agent(session_id_opt)
                    except Exception:
                        logger.exception(
                            "list_messages_for_agent failed (quick), proceeding without history"
                        )
                event_iter = runner.chat_stream(
                    question=question, top_k=top_k, agent_mode=agent_mode,
                    profile=profile, history=quick_history,
                    session_id=session_id_opt,
                )
            for event in event_iter:
                et = event.get("type")
                if et == "chunk":
                    accumulated.append(str(event.get("content") or ""))
                elif et in ("thought", "tool_call", "tool_result") and agent_mode == "agent":
                    # 累积推理痕迹用于会话历史回放（不含原始 chunk 文本）
                    reasoning_events.append(_compact_reasoning_event(event))
                elif et == "sources":
                    # Phase 11.1.2: 抓 chunk_ids 用于 audit；不影响事件转发
                    srcs = event.get("sources") or []
                    if isinstance(srcs, list):
                        for s in srcs:
                            if not isinstance(s, dict):
                                continue
                            cid = str(s.get("source_id") or "").strip()
                            if cid and cid not in qa_chunk_ids:
                                qa_chunk_ids.append(cid)
                elif et == "clarification":
                    # Phase 12.3: 反问事件存进 audit meta（不另开 event_type）
                    # 留 trigger_reasons / top_score / cross_docs 用于后续统计
                    qa_meta["clarification"] = {
                        "triggered": True,
                        "trigger_reasons": event.get("trigger_reasons") or [],
                        "top_score": event.get("top_score"),
                        "cross_docs": event.get("cross_docs") or [],
                    }
                elif et == "intent":
                    # Phase 11.1.5: 意图分类结果存进 audit meta
                    qa_meta["intent"] = {
                        "intent": event.get("intent"),
                        "confidence": event.get("confidence"),
                        "source": event.get("source"),
                        "ms": event.get("ms"),
                    }
                elif et == "scratchpad_update":
                    # Phase 12.4: scratchpad 增量事件透传给前端可视化
                    # 不直接存 audit；done 事件 meta.scratchpad 已含全量统计
                    pass
                elif et == "done":
                    fa = event.get("answer")
                    if isinstance(fa, str) and fa.strip():
                        final_answer = fa.strip()
                    else:
                        final_answer = "".join(accumulated).strip()
                    meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
                    if isinstance(meta, dict):
                        reasoning_meta = {
                            "iterations": meta.get("iterations"),
                            "effective_agent_mode": meta.get("effective_agent_mode"),
                        }
                        # Phase 12.4: scratchpad 统计存进 audit meta
                        sp = meta.get("scratchpad") or {}
                        if isinstance(sp, dict) and sp.get("size") is not None:
                            qa_meta["scratchpad"] = {
                                "size": sp.get("size", 0),
                                "total_calls": sp.get("total_calls", 0),
                                "total_summarize_ms": sp.get("total_summarize_ms", 0),
                                "facts": sp.get("facts") or [],
                            }
                    if session_id_opt and question:
                        reasoning_payload = None
                        if agent_mode == "agent" and (reasoning_events or reasoning_meta):
                            reasoning_payload = {
                                "iterations": reasoning_meta.get("iterations"),
                                "effective_agent_mode": reasoning_meta.get("effective_agent_mode"),
                                "events": reasoning_events,
                            }
                        try:
                            persist_chat_turn(
                                session_id_opt,
                                kb_id,
                                question,
                                final_answer or "".join(accumulated).strip(),
                                agent_mode=agent_mode,
                                reasoning_for_assistant=reasoning_payload,
                            )
                        except Exception:
                            logger.exception("append_chat_turn failed")
                        # Phase 12.2: 标记 done 后跑 session memory 摘要
                        # 真正调用放到循环外（done 已 yield 给客户端，避免阻塞前端体感）
                        _should_summarize = True
                    # Phase 11.1.2: done 后写 qa 审计（无 session_id 也写，反映匿名提问）
                    if question:
                        qa_meta = {
                            "agent_mode": agent_mode,
                            "effective_agent_mode": reasoning_meta.get("effective_agent_mode"),
                            "retrieval_source_count": (meta or {}).get("retrieval_source_count"),
                            "no_answer_from_documents": (meta or {}).get("no_answer_from_documents", False),
                        }
                        _should_audit = True
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            # Phase 11.1.2: done 后写 qa 审计（done 已 yield，不阻塞前端）
            # 即使 session_id 为空也记录（覆盖匿名 /api/chat/markdown 入口）
            if _should_audit and question:
                try:
                    from custom_app.services.audit_log import log_qa
                    # P-Perm: 注入 user_id（未登录或 admin token 时为空）
                    _uid = ""
                    try:
                        from custom_app.services.auth import current_user_id
                        _uid = current_user_id() or ""
                    except Exception:
                        pass
                    log_qa(
                        kb_id=kb_id,
                        session_id=session_id_opt,
                        query=question,
                        answer=final_answer or "".join(accumulated).strip(),
                        chunk_ids=qa_chunk_ids,
                        extra_meta=qa_meta,
                        user_id=_uid,
                    )
                except Exception:
                    logger.exception("audit_log.log_qa failed")
            # Phase 12.2: done 事件已全部 yield 完，此处同步触发摘要
            # 失败仅记日志（不再向客户端 yield 错误，避免污染流末尾）
            if _should_summarize and session_id_opt:
                try:
                    from custom_app.services.session_memory import maybe_summarize
                    mem_result = maybe_summarize(session_id_opt)
                    if mem_result.applied:
                        logger.info(
                            "session_memory updated session=%s chars=%d "
                            "facts=%d ms=%d",
                            session_id_opt,
                            len(mem_result.summary),
                            len(mem_result.facts),
                            mem_result.ms,
                        )
                except Exception:
                    logger.exception("maybe_summarize failed")

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@chat_bp.route("/api/chat/markdown", methods=["POST"])
def chat_markdown():
    data = request.get_json(silent=True) or {}
    kb_id = str(data.get("kb_id", "agv_demo")).strip() or "agv_demo"
    question = str(data.get("question", "")).strip()
    top_k = data.get("top_k")

    if not question:
        return jsonify({"error": "question 不能为空"}), 400

    # P-Perm C7：必须对该 kb 有 read 权限
    allowed, reason = _check_kb_access(kb_id, level="read")
    if not allowed:
        return jsonify({"error": reason or "forbidden", "code": "KB_FORBIDDEN"}), 403

    agent_mode = str(data.get("agent_mode", "quick")).strip().lower()
    if agent_mode not in ("quick", "agent"):
        agent_mode = "quick"

    agent_id = _resolve_request_agent_id(data)
    model_id = _resolve_request_model_id(data, agent_id=agent_id, agent_mode=agent_mode)
    try:
        runner = _get_runner(kb_id, model_id, agent_id, agent_mode=agent_mode)
        result = runner.chat(question=question, top_k=top_k, agent_mode=agent_mode)
        md = _result_to_markdown(question, result)
        return Response(md, mimetype="text/markdown; charset=utf-8")
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
