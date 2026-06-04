"""Phase 9.1 图片语义抽取（VLM caption + 实体）。

主入口 ``describe_image()``：给定图片路径 + chunk 上下文，调 Gemini Vision 生成：
    - caption_zh: 30-120 中文字 caption
    - caption_en: 30-100 英文 caption
    - entities: 3-8 条短语（中文实体 + 英文术语原文）

设计原则（与 reference_resolver / session_memory / scratchpad / intent 对齐）：
    - **失败一律降级**：任何异常返回 ``ImageDescription(failed=True, reason=...)``
      不抛回调用方，让上游 backfill / ingest 流程可继续
    - 复用 ``GeminiLLMAdapter`` 风格的 REST 调用 + 重试，但**独立函数**
      （因为需要带 inlineData 图像 part，而 messages_to_gemini_contents 是
      纯文本管线）
    - Gemini API key 优先级：env > DB chat_models 表里 provider='gemini' 行
    - 模型默认 ``gemini-3.1-pro-preview``（DB 已配置；2.0-flash 已下架）

env：
    ULTRARAG_IMAGE_DESCRIBE_ENABLED      默认 1（设 0 全跳，所有 caption 留空）
    ULTRARAG_IMAGE_DESCRIBE_MODEL        默认 gemini-3.1-pro-preview
    ULTRARAG_IMAGE_DESCRIBE_TIMEOUT_SEC  默认 60（单图调用上限）
    ULTRARAG_IMAGE_DESCRIBE_MAX_RETRIES  默认 2
    ULTRARAG_IMAGE_DESCRIBE_MAX_BYTES    默认 4_000_000（>4MB 跳过，避免超
                                          Gemini 单请求上限）
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

DEFAULT_MODEL = "gemini-2.5-flash"  # PoC 验证：3.1-pro-preview 输出过早截断；2.5-flash 稳定且更快
DEFAULT_TIMEOUT_SEC = 60
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_BYTES = 4_000_000  # 4MB
DEFAULT_MAX_CHUNK_CONTEXT_CHARS = 1200  # 截断 chunk_context 防 token 爆

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "prompt"
_PROMPT_TEMPLATE_NAME = "image_caption.jinja"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class ImageDescription:
    """单张图的描述结果。failed=True 时其他字段都是空 / 默认值。"""

    caption_zh: str = ""
    caption_en: str = ""
    entities: list[str] = field(default_factory=list)
    failed: bool = False
    reason: str | None = None        # 失败原因：not_enabled / file_missing / too_large / llm_error / parse_error
    ms: int = 0
    model: str = ""
    raw_text: str | None = None      # 调试用

    def to_dict(self) -> dict[str, Any]:
        return {
            "caption_zh": self.caption_zh,
            "caption_en": self.caption_en,
            "entities": list(self.entities),
            "failed": self.failed,
            "reason": self.reason,
            "ms": self.ms,
            "model": self.model,
        }


# ---------------------------------------------------------------------------
# env / 工具
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool = True) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (ValueError, TypeError):
        return default


def is_enabled() -> bool:
    return _env_bool("ULTRARAG_IMAGE_DESCRIBE_ENABLED", default=True)


_template_cache: dict[str, Any] = {}


def _render_prompt(chunk_context: str) -> str:
    if "tmpl" not in _template_cache:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)))
        _template_cache["tmpl"] = env.get_template(_PROMPT_TEMPLATE_NAME)
    ctx = (chunk_context or "").strip()
    if len(ctx) > DEFAULT_MAX_CHUNK_CONTEXT_CHARS:
        ctx = ctx[:DEFAULT_MAX_CHUNK_CONTEXT_CHARS] + "...[truncated]"
    return _template_cache["tmpl"].render(chunk_context=ctx or "（无上下文）")


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```\w*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
        t = t.strip()
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    # PoC 发现：Gemini 偶发输出截断（caption_zh/en 完整但 entities 截断），
    # 完整 JSON 无法解析。降级用正则把已输出的 caption_zh/en 抽出来，
    # entities 留空——至少保住主要内容。
    return _salvage_partial_caption(t)


def _salvage_partial_caption(text: str) -> dict[str, Any] | None:
    """从被截断的 JSON 文本中正则抽 caption_zh / caption_en，作为兜底。

    返回 {"caption_zh": ..., "caption_en": ..., "entities": []} 或 None。
    仅处理 JSON 字符串内的转义序列（\\" \\\\ \\n \\t），不要碰 Unicode 字符
    （它们已经是 utf-8 字符串，再走 unicode_escape 会乱码）。
    """
    if not text:
        return None

    def _unescape(s: str) -> str:
        """简化版：仅处理 JSON 字符串里常见的几个转义。"""
        # 顺序很重要：先处理 \\，再处理 \"，再处理 \n \t
        return (
            s.replace("\\\\", "\x00")     # 临时占位避免被后续误伤
             .replace('\\"', '"')
             .replace("\\n", "\n")
             .replace("\\r", "\r")
             .replace("\\t", "\t")
             .replace("\x00", "\\")
        )

    def _extract(field: str) -> str:
        # 匹配 "field": "..." —— 值内允许任意非转义 " 之前的字符
        pattern = rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"'
        m = re.search(pattern, text)
        if not m:
            return ""
        return _unescape(m.group(1))

    zh = _extract("caption_zh").strip()
    en = _extract("caption_en").strip()
    if not zh and not en:
        return None
    return {
        "caption_zh": zh,
        "caption_en": en,
        "entities": [],  # 截断时实体不完整，宁可丢
        "_salvaged": True,
    }


# ---------------------------------------------------------------------------
# Gemini Vision 调用
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    """优先 env，其次 DB chat_models 表里 provider='gemini' 行。"""
    key = (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("ULTRARAG_GEMINI_API_KEY")
        or ""
    ).strip()
    if key:
        return key
    try:
        from custom_app.repositories.chat_model_repository import ChatModelRepository
        repo = ChatModelRepository()
        rows = repo.list_active(tenant_id=1, include_disabled=False)
    except Exception as e:  # noqa: BLE001
        logger.debug("ChatModelRepository unavailable for image_describer: %s", e)
        return ""
    gemini_rows = [r for r in rows if (r.get("provider") or "").strip() == "gemini"]
    if gemini_rows:
        return str(gemini_rows[0].get("api_key") or "").strip()
    return ""


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix in (".bmp",):
        return "image/bmp"
    guessed = mimetypes.guess_type(str(path))[0]
    return guessed or "application/octet-stream"


def _call_gemini_vision(
    *,
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    model: str,
    api_key: str,
    timeout_sec: int,
    max_retries: int,
) -> tuple[str, str]:
    """直接 REST 调 Gemini Vision；返回 (raw_text, model_used)。失败抛异常。

    与 GeminiLLMAdapter.call 等价的最小实现：手动构造含 inlineData part 的 body。
    """
    import requests  # 延迟 import，与项目其他模块一致

    url = f"{GEMINI_API_BASE}/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 1024,
            # 强制结构化 JSON 输出（Gemini 2.0+）——避免 Gemini 3.1 在
            # 复杂 prompt 下输出不完整 JSON 导致 parse_error
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "caption_zh": {"type": "string"},
                    "caption_en": {"type": "string"},
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["caption_zh", "caption_en", "entities"],
            },
        },
    }

    last_err: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=timeout_sec)
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as e:
            last_err = e
            logger.warning(
                "image_describer network error model=%s attempt=%d/%d: %s",
                model, attempt, max_retries, e,
            )
            if attempt < max_retries:
                time.sleep(1.0 * attempt)
                continue
            raise

        if resp.status_code >= 400:
            logger.warning(
                "image_describer http error model=%s status=%s body=%s",
                model, resp.status_code, resp.text[:500],
            )
            raise RuntimeError(
                f"gemini_status_{resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        # 提取 candidates[0].content.parts[*].text
        try:
            cand = data["candidates"][0]
            finish_reason = cand.get("finishReason", "")
            parts = cand["content"]["parts"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(
                f"gemini_response_malformed: {e} body={json.dumps(data)[:300]}"
            ) from e
        texts = []
        for p in parts:
            t = p.get("text")
            if t:
                texts.append(t)
        result_text = "\n".join(texts).strip()
        # finishReason MAX_TOKENS / SAFETY / RECITATION 等都是潜在问题
        if finish_reason and finish_reason not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            logger.warning(
                "image_describer non-stop finish_reason=%s len=%d preview=%r",
                finish_reason, len(result_text), result_text[:100],
            )
        return result_text, model

    # 重试耗尽
    raise RuntimeError(f"gemini_retries_exhausted: {last_err}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def describe_image(
    image_path: str | Path,
    *,
    chunk_context: str = "",
    kb_root: str | Path | None = None,
) -> ImageDescription:
    """生成图片的中英 caption + 实体。

    参数:
        image_path:    图片文件路径（绝对路径或相对 kb_root 的相对路径）
        chunk_context: 同 chunk 的文本上下文（提升 caption 准确性）
        kb_root:       KB 根目录；image_path 是相对路径时用它解析

    返回:
        ImageDescription；failed=True 时 caption_zh/caption_en/entities 为
        默认值，调用方可继续（如写空 caption 到 jsonl）
    """
    t0 = time.perf_counter()
    result = ImageDescription()

    if not is_enabled():
        result.failed = True
        result.reason = "not_enabled"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 解析路径
    p = Path(image_path)
    if kb_root and not p.is_absolute():
        p = Path(kb_root) / p
    if not p.exists() or not p.is_file():
        result.failed = True
        result.reason = "file_missing"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 大小检查
    max_bytes = _env_int("ULTRARAG_IMAGE_DESCRIBE_MAX_BYTES", DEFAULT_MAX_BYTES)
    file_size = p.stat().st_size
    if file_size > max_bytes:
        result.failed = True
        result.reason = f"too_large:{file_size}>{max_bytes}"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result
    if file_size == 0:
        result.failed = True
        result.reason = "empty_file"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    # 读图 + 构造调用
    try:
        image_bytes = p.read_bytes()
        mime = _guess_mime(p)
    except OSError as e:
        result.failed = True
        result.reason = f"read_error:{e}"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    api_key = _resolve_api_key()
    if not api_key:
        result.failed = True
        result.reason = "no_api_key"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    model = (
        os.environ.get("ULTRARAG_IMAGE_DESCRIBE_MODEL") or DEFAULT_MODEL
    ).strip()
    timeout_sec = max(1, _env_int(
        "ULTRARAG_IMAGE_DESCRIBE_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC,
    ))
    max_retries = max(1, _env_int(
        "ULTRARAG_IMAGE_DESCRIBE_MAX_RETRIES", DEFAULT_MAX_RETRIES,
    ))

    prompt = _render_prompt(chunk_context)
    # PoC 发现：Gemini 2.5 Flash 偶发输出在 60 字符处提前 "STOP"，content
    # parts 不完整 → parse 失败。**独立调用同一张图**通常成功，说明是短时
    # 限流 / 服务端缓存抖动。retry 加退避 sleep 让服务端忘记上次请求。
    parse_attempts = 3
    parse_retry_backoff_sec = 1.5
    raw_text = ""
    model_used = ""
    parsed: dict[str, Any] | None = None
    for parse_attempt in range(1, parse_attempts + 1):
        try:
            raw_text, model_used = _call_gemini_vision(
                image_bytes=image_bytes,
                mime_type=mime,
                prompt=prompt,
                model=model,
                api_key=api_key,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
            )
        except Exception as e:  # noqa: BLE001
            result.failed = True
            result.reason = f"llm_error:{type(e).__name__}"
            result.model = model
            result.ms = int((time.perf_counter() - t0) * 1000)
            return result

        result.raw_text = raw_text
        result.model = model_used

        parsed = _parse_llm_json(raw_text)
        if parsed and isinstance(parsed, dict):
            break
        if parse_attempt < parse_attempts:
            logger.info(
                "image_describer parse_error attempt=%d/%d, sleep %.1fs and retry (raw_len=%d)",
                parse_attempt, parse_attempts, parse_retry_backoff_sec, len(raw_text),
            )
            time.sleep(parse_retry_backoff_sec * parse_attempt)

    if not parsed or not isinstance(parsed, dict):
        result.failed = True
        result.reason = "parse_error"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    caption_zh = str(parsed.get("caption_zh") or "").strip()
    caption_en = str(parsed.get("caption_en") or "").strip()
    entities_raw = parsed.get("entities") or []
    entities: list[str] = []
    if isinstance(entities_raw, list):
        for item in entities_raw:
            s = str(item or "").strip()
            if s and s not in entities:
                entities.append(s[:80])  # 单条硬截断

    if not caption_zh and not caption_en:
        result.failed = True
        result.reason = "empty_caption"
        result.ms = int((time.perf_counter() - t0) * 1000)
        return result

    result.caption_zh = caption_zh
    result.caption_en = caption_en
    result.entities = entities
    if parsed.get("_salvaged"):
        result.reason = "salvaged_partial"
    result.ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "image_describer ok path=%s ms=%d zh_chars=%d en_chars=%d entities=%d salvaged=%s",
        str(image_path), result.ms, len(caption_zh), len(caption_en),
        len(entities), bool(parsed.get("_salvaged")),
    )
    return result
