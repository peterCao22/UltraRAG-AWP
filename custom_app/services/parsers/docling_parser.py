"""DoclingParser —— 用 Docling 解析通用 DOCX，输出 Phase 4 统一 Chunk。

为什么用 Docling 而不是 MinerU 处理通用 DOCX？
    MinerU 处理 DOCX 的链路是 DOCX → LibreOffice → PDF → MinerU，PDF 中转有损
    （样式名丢失、图片位置精度下降）；Docling 是纯 Python，直接读 DOCX XML，
    通用 DOCX 上精度更高。

部署要求：
    - pip install docling 或 uv sync --extras parsing
    - 首次运行可能下载模型（OCR / 公式识别等）；纯 DOCX 路径通常不需要

实现要点：
    - 用 docling.document_converter.DocumentConverter 解析
    - 把 result.document 导出为 markdown 后，复用 MarkdownParser 的逻辑切块
      —— Docling 的 markdown 输出已经包含 ATX 标题、表格、图片占位
    - 这个委托策略让 Docling 升级时只需关心其 markdown 输出格式
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from custom_app.services.parsers.markdown_parser import MarkdownParser
from custom_app.services.parsers.schema import Chunk

logger = logging.getLogger(__name__)


class DoclingParser:
    """通用 DOCX 解析器（基于 Docling）。

    用法：
        parser = DoclingParser()
        chunks = parser.parse(Path("manual.docx"), kb_root=Path("data/kb/general"))
    """

    def __init__(self) -> None:
        # 延迟加载 DocumentConverter，避免 import 时下载模型
        self._converter: object | None = None
        self._md_parser = MarkdownParser()

    def parse(self, file_path: Path, kb_root: Path) -> list[Chunk]:
        if not file_path.exists():
            raise FileNotFoundError(f"file not found: {file_path}")

        ext = file_path.suffix.lower()
        if ext != ".docx":
            raise ValueError(
                f"DoclingParser only supports .docx, got {ext!r}; "
                "SOP DOCX should route to DocxParser via factory"
            )

        try:
            from docling.document_converter import DocumentConverter
        except ImportError as e:
            raise RuntimeError(
                f"docling not installed: {e}. Install with: uv sync --extras parsing"
            ) from e

        if self._converter is None:
            self._converter = DocumentConverter()

        try:
            result = self._converter.convert(str(file_path))
        except Exception as e:
            raise RuntimeError(f"docling convert failed on {file_path.name}: {e}") from e

        doc = getattr(result, "document", None)
        if doc is None:
            raise RuntimeError(
                f"docling returned no document for {file_path.name}"
            )

        # 导出为 markdown，复用 MarkdownParser 的切块逻辑
        try:
            md_text = doc.export_to_markdown()
        except Exception as e:
            raise RuntimeError(
                f"docling export_to_markdown failed on {file_path.name}: {e}"
            ) from e

        doc_stem = file_path.stem
        # 把 markdown 写到临时缓存目录，MarkdownParser 通过 file_path 读取
        # （也可以让 MarkdownParser 支持字符串输入，但本类范围内不动它）
        cache_dir = kb_root / ".docling_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        md_cache = cache_dir / f"{doc_stem}.md"
        md_cache.write_text(md_text, encoding="utf-8")

        try:
            md_chunks = self._md_parser.parse(md_cache, kb_root)
        finally:
            # 即时清理临时 md，避免污染 kb_root（图片由 Docling 在 doc 内部直接引用，
            # 当前实现下不会落地到 kb_root；如未来需要图片落地，在此扩展）
            try:
                md_cache.unlink()
            except OSError:
                pass

        # 重写 source_type / parser 标记为 docling
        retagged: list[Chunk] = []
        for c in md_chunks:
            retagged.append(replace(c, source_type="general_docx", parser="docling"))
        return retagged
