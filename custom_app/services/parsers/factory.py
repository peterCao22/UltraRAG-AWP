"""Parser 工厂 —— 按 (kb_type, file_ext) 路由到具体 Parser 实现。

路由表（Phase 4.1）：
    sop_docx + .docx                  → DocxParser（现有，业务定制）
    general  + .docx                  → DoclingParser
    general  + .pdf                   → MineruParser
    general  + .png/.jpg/.jpeg/.bmp/.tiff → MineruParser (OCR)
    general  + .md/.markdown          → MarkdownParser
    general  + .txt                   → MarkdownParser

设计要点：
    - 重型 parser（MinerU、Docling）使用延迟 import；
      get_parser 调用时才加载，避免无依赖环境下 import 失败
    - DocxParser 是函数式接口（parse_docx），用 _DocxParserAdapter 包装为 Parser
    - get_supported_extensions 用于 upload 白名单动态裁剪

KB type 常量：
    KB_TYPE_SOP_DOCX = "sop_docx"
    KB_TYPE_GENERAL  = "general"
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from custom_app.services.parsers.base import Parser
from custom_app.services.parsers.schema import Chunk

KB_TYPE_SOP_DOCX = "sop_docx"
KB_TYPE_GENERAL = "general"

VALID_KB_TYPES = frozenset({KB_TYPE_SOP_DOCX, KB_TYPE_GENERAL})

# 路由表的"映射目标"是字符串 key，get_parser 内部按 key 延迟加载实现
# 这样 .venv 缺 torch/mineru CLI 时，仍可 import factory 本身做单元测试
_ROUTING_TABLE: dict[tuple[str, str], str] = {
    # sop_docx：仅支持 .docx，走业务定制
    (KB_TYPE_SOP_DOCX, ".docx"): "docx",
    # general：多格式
    (KB_TYPE_GENERAL, ".docx"): "docling",
    (KB_TYPE_GENERAL, ".pdf"): "mineru",
    (KB_TYPE_GENERAL, ".png"): "mineru",
    (KB_TYPE_GENERAL, ".jpg"): "mineru",
    (KB_TYPE_GENERAL, ".jpeg"): "mineru",
    (KB_TYPE_GENERAL, ".bmp"): "mineru",
    (KB_TYPE_GENERAL, ".tiff"): "mineru",
    (KB_TYPE_GENERAL, ".tif"): "mineru",
    (KB_TYPE_GENERAL, ".md"): "markdown",
    (KB_TYPE_GENERAL, ".markdown"): "markdown",
    (KB_TYPE_GENERAL, ".txt"): "markdown",
}


class ParserNotAvailableError(RuntimeError):
    """指定 (kb_type, ext) 对应的 parser 不可用（依赖缺失或路由未配置）。"""


class _DocxParserAdapter:
    """把 docx_parser.parse_docx 函数包装为 Parser Protocol 实现。

    parse_docx 现有签名: (docx_path, kb_root) -> List[Dict]
    需适配：返回 list[Chunk]
    """

    def parse(self, file_path: Path, kb_root: Path) -> list[Chunk]:
        from custom_app.services.docx_parser import parse_docx

        raw_chunks = parse_docx(file_path, kb_root)
        return [Chunk.from_jsonl_dict(d) for d in raw_chunks]


def _normalize_ext(file_path: Path) -> str:
    return file_path.suffix.lower()


def get_parser(kb_type: str, file_path: Path) -> Parser:
    """按 KB 类型 + 文件扩展名返回对应 Parser 实例。

    Args:
        kb_type:    sop_docx / general
        file_path:  待解析文件（用于取扩展名）

    Returns:
        Parser 实例

    Raises:
        ValueError: kb_type 不在 VALID_KB_TYPES
        ParserNotAvailableError: 路由未配置或 parser 依赖加载失败
    """
    if kb_type not in VALID_KB_TYPES:
        raise ValueError(
            f"invalid kb_type {kb_type!r}, expected one of {sorted(VALID_KB_TYPES)}"
        )
    ext = _normalize_ext(file_path)
    key = (kb_type, ext)
    parser_key = _ROUTING_TABLE.get(key)
    if parser_key is None:
        raise ParserNotAvailableError(
            f"no parser registered for kb_type={kb_type!r} ext={ext!r}"
        )

    if parser_key == "docx":
        return _DocxParserAdapter()
    if parser_key == "markdown":
        from custom_app.services.parsers.markdown_parser import MarkdownParser

        return MarkdownParser()
    if parser_key == "mineru":
        try:
            from custom_app.services.parsers.mineru_parser import MineruParser
        except ImportError as e:
            raise ParserNotAvailableError(
                f"MineruParser unavailable: {e}. "
                "Install parsing extras: uv sync --extras parsing"
            ) from e
        return MineruParser()
    if parser_key == "docling":
        try:
            from custom_app.services.parsers.docling_parser import DoclingParser
        except ImportError as e:
            raise ParserNotAvailableError(
                f"DoclingParser unavailable: {e}. "
                "Install parsing extras: uv sync --extras parsing"
            ) from e
        return DoclingParser()

    raise ParserNotAvailableError(f"unknown parser key {parser_key!r}")


def get_supported_extensions(kb_type: str) -> set[str]:
    """返回指定 KB 类型支持的所有文件扩展名集合（含前导点）。

    用于 upload_documents 白名单：根据 KB type 动态返回允许的扩展名。
    """
    if kb_type not in VALID_KB_TYPES:
        raise ValueError(f"invalid kb_type {kb_type!r}")
    return {ext for (kt, ext) in _ROUTING_TABLE if kt == kb_type}


def is_supported(kb_type: str, file_path: Path) -> bool:
    """快速检查某文件能否被指定 KB 类型解析（不实际加载 parser）。"""
    if kb_type not in VALID_KB_TYPES:
        return False
    ext = _normalize_ext(file_path)
    return (kb_type, ext) in _ROUTING_TABLE


def parse_files(
    kb_type: str,
    file_paths: Iterable[Path],
    kb_root: Path,
    *,
    kb_id: str = "",
) -> list[Chunk]:
    """便利函数：批量解析多个文件，返回合并后的 Chunk 列表。

    跳过不支持的文件（写日志），失败的文件抛出异常（不静默吞）。
    自动填充 chunk.kb_id 字段。
    """
    import logging

    log = logging.getLogger(__name__)
    out: list[Chunk] = []
    for fp in file_paths:
        if not is_supported(kb_type, fp):
            log.warning(
                "skip unsupported file kb_type=%s file=%s", kb_type, fp.name
            )
            continue
        parser = get_parser(kb_type, fp)
        chunks = parser.parse(fp, kb_root)
        # 注入 kb_id（Chunk 是 frozen dataclass，需 dataclasses.replace）
        if kb_id:
            from dataclasses import replace

            chunks = [replace(c, kb_id=kb_id) for c in chunks]
        out.extend(chunks)
    return out
