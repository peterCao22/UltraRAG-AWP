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

# 每个 kb_id 维护独立的 RagRunner 实例，用锁保护并发写
_runners: dict[str, RagRunner] = {}
_runners_lock = threading.Lock()

# AgentRunner 实例池（agent_mode=agent 时使用）
_agent_runners: dict[str, "AgentRunner"] = {}  # type: ignore[name-defined]
_agent_runners_lock = threading.Lock()


def _get_runner(kb_id: str) -> RagRunner:
    with _runners_lock:
        if kb_id not in _runners:
            r = RagRunner(kb_id=kb_id)
            r.init()
            _runners[kb_id] = r
        return _runners[kb_id]


def _get_agent_runner(kb_id: str):
    """返回与 kb_id 绑定的 AgentRunner，首次调用时复用 RagRunner 的向量存储与语料。"""
    from custom_app.services.agent_runner import AgentRunner

    with _agent_runners_lock:
        if kb_id not in _agent_runners:
            # 复用 RagRunner 已加载的 rows / vector_store，避免二次磁盘读取
            rag = _get_runner(kb_id)
            ar = AgentRunner(kb_id=kb_id)
            ar.init(
                rows=rag._rows,
                index=rag._index,
                kb_name=kb_id,
                vector_store=getattr(rag, "_vector_store", None),
                # 复用 RagRunner 的 _build_sources：它知道如何把图片转 base64 data URL，
                # agent 模式的最终答案才能挂图（否则 SSE 只发文本）。
                source_builder=rag._build_sources,
            )
            _agent_runners[kb_id] = ar
        return _agent_runners[kb_id]


def invalidate_runner_cache(kb_id: str) -> None:
    """重建索引后必须调用，否则 RagRunner / AgentRunner 仍持有旧的 rows / FAISS 引用，
    新上传的文档查不到、被删除的文档可能仍在召回。

    线程安全：分别拿两把锁，避免与 _get_runner / _get_agent_runner 竞争。
    """
    with _runners_lock:
        _runners.pop(kb_id, None)
    with _agent_runners_lock:
        _agent_runners.pop(kb_id, None)
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

    agent_mode = str(data.get("agent_mode", "quick")).strip().lower()
    if agent_mode not in ("quick", "agent"):
        agent_mode = "quick"

    try:
        runner = _get_runner(kb_id)
        result = runner.chat(question=question, top_k=top_k, agent_mode=agent_mode)
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
    session_id_opt = str(data.get("session_id", "")).strip() or None
    profile = bool(data.get("profile"))
    if str(os.environ.get("ULTRARAG_CHAT_PROFILE", "")).lower() in ("1", "true", "yes"):
        profile = True
    if (request.headers.get("X-Ultrarag-Profile") or "").strip() == "1":
        profile = True

    if not question:
        return jsonify({"error": "question 不能为空"}), 400

    def generate():
        accumulated: list[str] = []
        final_answer = ""
        # 仅 agent 模式收集；quick 模式 reasoning_events 保持空，落库为 {}
        reasoning_events: list[dict] = []
        reasoning_meta: dict = {}
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
            if agent_mode == "agent":
                logger.info("chat_stream routing → AgentRunner kb_id=%s session_id=%s", kb_id, session_id_opt)
                runner = _get_agent_runner(kb_id)
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
                    question=question, top_k=top_k, profile=profile, history=history
                )
            else:
                logger.info("chat_stream routing → RagRunner kb_id=%s agent_mode=%s", kb_id, agent_mode)
                runner = _get_runner(kb_id)
                event_iter = runner.chat_stream(
                    question=question, top_k=top_k, agent_mode=agent_mode, profile=profile
                )
            for event in event_iter:
                et = event.get("type")
                if et == "chunk":
                    accumulated.append(str(event.get("content") or ""))
                elif et in ("thought", "tool_call", "tool_result") and agent_mode == "agent":
                    # 累积推理痕迹用于会话历史回放（不含原始 chunk 文本）
                    reasoning_events.append(_compact_reasoning_event(event))
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
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"

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

    agent_mode = str(data.get("agent_mode", "quick")).strip().lower()
    if agent_mode not in ("quick", "agent"):
        agent_mode = "quick"

    try:
        runner = _get_runner(kb_id)
        result = runner.chat(question=question, top_k=top_k, agent_mode=agent_mode)
        md = _result_to_markdown(question, result)
        return Response(md, mimetype="text/markdown; charset=utf-8")
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
