"""Phase 9.1 image_describer 单元测试。

覆盖：
  1. happy path：mock Gemini 返回 happy JSON → caption_zh/en + entities
  2. ENABLED=0 → failed=True, reason='not_enabled'
  3. 文件不存在 → reason='file_missing'
  4. 图过大 → reason='too_large'
  5. 空文件 → reason='empty_file'
  6. 无 API key → reason='no_api_key'
  7. Gemini HTTP 失败 → reason='llm_error:...'
  8. parse 错误（非 JSON）→ reason='parse_error'
  9. empty_caption（解析成功但两个 caption 都空）→ reason='empty_caption'
 10. ImageDescription.to_dict 契约
 11. ChunkImage.from_jsonl_dict 向后兼容（旧字符串、旧 dict、新 dict）
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_app.services.parsers.image_describer import (
    ImageDescription,
    describe_image,
)
from custom_app.services.parsers.schema import ChunkImage


@pytest.fixture()
def tiny_jpeg(tmp_path: Path) -> Path:
    """生成一个最小合法 jpeg 文件（用于路径存在但内容不重要的 mock 场景）。"""
    p = tmp_path / "tiny.jpg"
    # 最小 JPEG header (SOI) + EOI marker = 4 bytes，能让 _guess_mime 命中 jpeg
    p.write_bytes(b"\xff\xd8\xff\xd9")
    return p


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_describe_image_happy_path(tiny_jpeg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(
            json.dumps({
                "caption_zh": "AGV 急停按钮特写，红色蘑菇头位于控制面板左上方。",
                "caption_en": "Close-up of AGV emergency stop button, red mushroom head on upper left of control panel.",
                "entities": ["急停按钮", "E-Stop Button", "AGV", "控制面板"],
            }),
            "gemini-3.1-pro-preview",
        )),
    )

    result = describe_image(tiny_jpeg, chunk_context="STEP 5: Press emergency stop")

    assert result.failed is False
    assert "急停按钮" in result.caption_zh
    assert "emergency stop" in result.caption_en.lower()
    assert "急停按钮" in result.entities
    assert "E-Stop Button" in result.entities
    assert result.model == "gemini-3.1-pro-preview"
    assert result.reason is None


def test_describe_image_to_dict_shape(tiny_jpeg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(
            json.dumps({"caption_zh": "z", "caption_en": "e", "entities": ["x"]}),
            "m",
        )),
    )
    result = describe_image(tiny_jpeg)
    d = result.to_dict()
    assert set(d.keys()) == {
        "caption_zh", "caption_en", "entities", "failed", "reason", "ms", "model",
    }
    assert d["caption_zh"] == "z"
    assert d["entities"] == ["x"]


# ---------------------------------------------------------------------------
# 失败降级路径
# ---------------------------------------------------------------------------


def test_disabled(tiny_jpeg, monkeypatch):
    monkeypatch.setenv("ULTRARAG_IMAGE_DESCRIBE_ENABLED", "0")
    result = describe_image(tiny_jpeg)
    assert result.failed is True
    assert result.reason == "not_enabled"


def test_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    result = describe_image(tmp_path / "nonexistent.jpg")
    assert result.failed is True
    assert result.reason == "file_missing"


def test_too_large(tiny_jpeg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setenv("ULTRARAG_IMAGE_DESCRIBE_MAX_BYTES", "1")  # 让任何文件都"过大"
    result = describe_image(tiny_jpeg)
    assert result.failed is True
    assert result.reason.startswith("too_large:")


def test_empty_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    result = describe_image(empty)
    assert result.failed is True
    assert result.reason == "empty_file"


def test_no_api_key(tiny_jpeg, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ULTRARAG_GEMINI_API_KEY", raising=False)
    # DB 也没有 gemini 行
    fake_repo = MagicMock()
    fake_repo.list_active.return_value = []
    monkeypatch.setattr(
        "custom_app.repositories.chat_model_repository.ChatModelRepository",
        lambda *a, **kw: fake_repo,
    )
    result = describe_image(tiny_jpeg)
    assert result.failed is True
    assert result.reason == "no_api_key"


def test_llm_error(tiny_jpeg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(side_effect=RuntimeError("gemini_status_500: ...")),
    )
    result = describe_image(tiny_jpeg)
    assert result.failed is True
    assert result.reason.startswith("llm_error:")
    assert result.model  # 即便失败也填了 model


def test_parse_error(tiny_jpeg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=("not a json at all just text", "m")),
    )
    result = describe_image(tiny_jpeg)
    assert result.failed is True
    assert result.reason == "parse_error"


def test_empty_caption(tiny_jpeg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(
            json.dumps({"caption_zh": "", "caption_en": "", "entities": []}),
            "m",
        )),
    )
    result = describe_image(tiny_jpeg)
    assert result.failed is True
    assert result.reason == "empty_caption"


# ---------------------------------------------------------------------------
# JSON parse 容错
# ---------------------------------------------------------------------------


def test_parse_strips_code_fences(tiny_jpeg, monkeypatch):
    """Gemini 偶尔输出 ```json {...} ``` 包裹，应能解析。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    raw = '```json\n{"caption_zh": "z", "caption_en": "e", "entities": ["x"]}\n```'
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(raw, "m")),
    )
    result = describe_image(tiny_jpeg)
    assert result.failed is False
    assert result.caption_zh == "z"


def test_parse_handles_extra_text_around_json(tiny_jpeg, monkeypatch):
    """Gemini 可能在 JSON 前后加一些解释，正则抓首个 {...}。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    raw = 'Here is the result:\n{"caption_zh": "z", "caption_en": "e", "entities": []}\nDone.'
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(raw, "m")),
    )
    result = describe_image(tiny_jpeg)
    assert result.failed is False
    assert result.caption_zh == "z"


# ---------------------------------------------------------------------------
# ChunkImage 向后兼容（Phase 9.1 schema 升级）
# ---------------------------------------------------------------------------


def test_chunk_image_old_string_format_still_loads():
    """旧 chunks.jsonl 用字符串路径 → ChunkImage(path=str)，新字段默认空。"""
    from custom_app.services.parsers.schema import Chunk

    chunk = Chunk.from_jsonl_dict({
        "id": "c1", "title": "t", "contents": "x", "doc": "d",
        "images": ["images/foo.jpg", "images/bar.png"],
    })
    assert len(chunk.images) == 2
    assert chunk.images[0].path == "images/foo.jpg"
    assert chunk.images[0].caption_zh == ""
    assert chunk.images[0].caption_en == ""
    assert chunk.images[0].entities == ()


def test_chunk_image_old_dict_format_still_loads():
    """旧 dict 格式（仅 caption）继续 work。"""
    from custom_app.services.parsers.schema import Chunk

    chunk = Chunk.from_jsonl_dict({
        "id": "c1", "title": "t", "contents": "x", "doc": "d",
        "images": [{"path": "images/foo.jpg", "caption": "old caption"}],
    })
    assert chunk.images[0].caption == "old caption"
    assert chunk.images[0].caption_zh == ""  # 不自动迁移


def test_chunk_image_new_format_loads_all_fields():
    """Phase 9.1 新格式：含 caption_zh/caption_en/entities。"""
    from custom_app.services.parsers.schema import Chunk

    chunk = Chunk.from_jsonl_dict({
        "id": "c1", "title": "t", "contents": "x", "doc": "d",
        "images": [{
            "path": "images/foo.jpg",
            "caption_zh": "中文描述",
            "caption_en": "english description",
            "entities": ["A", "B", "C"],
        }],
    })
    assert chunk.images[0].caption_zh == "中文描述"
    assert chunk.images[0].caption_en == "english description"
    assert chunk.images[0].entities == ("A", "B", "C")


def test_chunk_image_to_dict_includes_new_fields():
    img = ChunkImage(
        path="p", caption_zh="zh", caption_en="en", entities=("x", "y"),
    )
    d = img.to_dict()
    assert d["caption_zh"] == "zh"
    assert d["caption_en"] == "en"
    assert d["entities"] == ["x", "y"]  # list 形式（jsonl 友好）


# ---------------------------------------------------------------------------
# parse_error retry + salvage（PoC 发现的 Gemini 早停场景）
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_second_attempt(tiny_jpeg, monkeypatch):
    """Gemini 第 1 次返回不完整 JSON，第 2 次返回完整 → 应该成功。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    incomplete = '{"caption_zh": "图片显示了一块大型电池'  # 未闭合
    complete = json.dumps({
        "caption_zh": "完整描述", "caption_en": "complete", "entities": ["x"],
    })
    call_mock = MagicMock(side_effect=[
        (incomplete, "gemini-2.5-flash"),
        (complete, "gemini-2.5-flash"),
    ])
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        call_mock,
    )
    # 加速测试（默认 backoff 1.5s）
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer.time.sleep",
        lambda *a, **kw: None,
    )

    result = describe_image(tiny_jpeg, chunk_context="STEP 3")

    assert result.failed is False
    assert result.caption_zh == "完整描述"
    assert call_mock.call_count == 2  # 重试了一次


def test_salvage_partial_caption_zh_only(tiny_jpeg, monkeypatch):
    """完整 caption_zh 已输出但 entities 截断 → salvage 取 caption_zh，
    reason='salvaged_partial'。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    # 3 次都返回相同的截断响应（caption_zh + caption_en 完整，entities 没闭）
    truncated = (
        '{"caption_zh": "AGV 控制面板告警", '
        '"caption_en": "AGV control panel alarm", '
        '"entities": ["AGV", "control'  # 截断
    )
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(truncated, "gemini-2.5-flash")),
    )
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer.time.sleep",
        lambda *a, **kw: None,
    )

    result = describe_image(tiny_jpeg, chunk_context="ID 34 alarm")

    assert result.failed is False  # salvage 成功
    assert result.reason == "salvaged_partial"
    assert result.caption_zh == "AGV 控制面板告警"
    assert result.caption_en == "AGV control panel alarm"
    assert result.entities == []  # 截断时 entities 留空


def test_salvage_caption_zh_only_when_en_missing(tiny_jpeg, monkeypatch):
    """连 caption_en 都没输出，只有 caption_zh → 仍 salvage。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    truncated = '{"caption_zh": "图片描述", "capt'  # 截在 caption_en 之前
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(truncated, "gemini-2.5-flash")),
    )
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer.time.sleep",
        lambda *a, **kw: None,
    )

    result = describe_image(tiny_jpeg)
    assert result.failed is False
    assert result.reason == "salvaged_partial"
    assert result.caption_zh == "图片描述"
    assert result.caption_en == ""


def test_salvage_fails_when_no_caption_extracted(tiny_jpeg, monkeypatch):
    """完全无法抽出任何 caption → 真失败 parse_error。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    junk = "Random text that has nothing JSON-ish about it"
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        MagicMock(return_value=(junk, "gemini-2.5-flash")),
    )
    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer.time.sleep",
        lambda *a, **kw: None,
    )

    result = describe_image(tiny_jpeg)
    assert result.failed is True
    assert result.reason == "parse_error"


def test_response_schema_in_request_body(tiny_jpeg, monkeypatch):
    """生成式 JSON 模式（responseSchema）在 body 里出现，保证 PoC 修复不被回滚。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    captured = {}

    def fake_call(*, image_bytes, mime_type, prompt, model, api_key,
                  timeout_sec, max_retries):
        captured["model"] = model
        # 模拟一个成功响应让流程跑完
        return (
            json.dumps({"caption_zh": "z", "caption_en": "e", "entities": ["x"]}),
            model,
        )

    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        fake_call,
    )

    result = describe_image(tiny_jpeg)
    assert result.failed is False
    # 默认模型已切到 gemini-2.5-flash（PoC 验证）
    assert "2.5-flash" in captured["model"] or "2.5" in captured["model"]


def test_default_model_is_gemini_2_5_flash():
    """default model 应为 2.5-flash（PoC 验证：3.1-pro 不稳定）。"""
    from custom_app.services.parsers.image_describer import DEFAULT_MODEL
    assert DEFAULT_MODEL == "gemini-2.5-flash"


def test_model_param_overrides_env(tiny_jpeg, monkeypatch):
    """describe_image(model=...) 应优先于 env。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setenv("ULTRARAG_IMAGE_DESCRIBE_MODEL", "gemini-2.5-flash")

    captured = {}

    def fake_call(*, image_bytes, mime_type, prompt, model, api_key,
                  timeout_sec, max_retries):
        captured["model"] = model
        return (
            json.dumps({"caption_zh": "z", "caption_en": "e", "entities": []}),
            model,
        )

    monkeypatch.setattr(
        "custom_app.services.parsers.image_describer._call_gemini_vision",
        fake_call,
    )

    result = describe_image(tiny_jpeg, model="gemini-2.5-pro")
    assert result.failed is False
    assert captured["model"] == "gemini-2.5-pro"
    assert result.model == "gemini-2.5-pro"
