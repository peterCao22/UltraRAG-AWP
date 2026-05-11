"""MarkdownParser —— 轻量 Markdown / 纯文本解析器（无外部依赖）。

设计：
    - 按 ATX 标题（# / ## / ###）切块，重建 heading_path 层级
    - 图片链接 ![alt](path) 解析为 ChunkImage 对象
    - 纯文本（.txt）无标题时整文档作为单 chunk

不支持：
    - Setext 标题（=== / ---）—— SOP/通用场景 ATX 是主流，留待后续
    - 表格语义解析 —— 表格行原样保留在 contents 字段，下游嵌入可直接吃
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from custom_app.services.parsers.schema import (
    Chunk,
    ChunkImage,
    ChunkStructure,
)


# ATX 标题：1-6 个 # 开头，后跟空格和标题文本
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Markdown 图片：![alt](path "title")，title 可选
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")


class MarkdownParser:
    """Markdown / 纯文本解析器。

    用法：
        parser = MarkdownParser()
        chunks = parser.parse(Path("doc.md"), kb_root=Path("data/kb/general"))
    """

    def parse(self, file_path: Path, kb_root: Path) -> list[Chunk]:
        if not file_path.exists():
            raise FileNotFoundError(f"markdown file not found: {file_path}")
        ext = file_path.suffix.lower()
        if ext not in {".md", ".markdown", ".txt"}:
            raise ValueError(f"unsupported extension for MarkdownParser: {ext}")

        text = file_path.read_text(encoding="utf-8", errors="replace")
        doc_stem = file_path.stem

        # txt 文件不解析标题，整文档一个 chunk
        if ext == ".txt":
            return self._build_single_chunk(text, doc_stem)

        return self._parse_markdown(text, doc_stem)

    def _build_single_chunk(self, text: str, doc_stem: str) -> list[Chunk]:
        body = text.strip()
        if not body:
            return []
        return [
            Chunk(
                id=f"{doc_stem}_chunk_1",
                title=doc_stem.replace("_", " "),
                contents=body,
                doc=doc_stem,
                source_type="markdown",
                parser="markdown",
                structure=ChunkStructure(
                    heading_path=tuple(),
                    heading_level=0,
                ),
                images=tuple(self._extract_images(body)),
            )
        ]

    def _parse_markdown(self, text: str, doc_stem: str) -> list[Chunk]:
        """按 ATX 标题切块；维护标题栈用于 heading_path 重建。"""
        chunks: list[Chunk] = []
        # 标题栈：索引 = level - 1，值 = 该 level 的标题文本
        # 例如 H1 "Intro" H2 "Setup" → stack = ["Intro", "Setup", None, ...]
        heading_stack: list[Optional[str]] = [None] * 6
        current_heading_level = 0
        current_title = doc_stem.replace("_", " ")
        current_body: list[str] = []
        intro_emitted = False
        section_idx = 0

        def emit() -> None:
            nonlocal section_idx
            body = "\n".join(current_body).strip()
            if not body:
                return
            section_idx += 1
            path = tuple(h for h in heading_stack if h)
            level = current_heading_level
            chunks.append(
                Chunk(
                    id=f"{doc_stem}_section_{section_idx}",
                    title=current_title,
                    contents=body,
                    doc=doc_stem,
                    source_type="markdown",
                    parser="markdown",
                    structure=ChunkStructure(
                        heading_path=path,
                        heading_level=level,
                    ),
                    images=tuple(self._extract_images(body)),
                )
            )

        for line in text.splitlines():
            m = _HEADING_RE.match(line)
            if m:
                # 遇到新标题：先把之前积累的正文 emit 成一个 chunk
                emit()
                current_body = []
                level = len(m.group(1))
                heading_text = m.group(2).strip()
                # 更新标题栈：当前 level 设新值，更深层级清空
                heading_stack[level - 1] = heading_text
                for deeper in range(level, 6):
                    heading_stack[deeper] = None
                current_heading_level = level
                current_title = heading_text
                intro_emitted = True
                continue
            current_body.append(line)

        # 末尾 emit 剩余内容
        emit()

        # 无任何标题的 markdown 文件：fallback 到单 chunk
        if not intro_emitted and not chunks:
            return self._build_single_chunk(text, doc_stem)
        # 有标题但首段（intro）也作为正文：上面循环已处理
        return chunks

    @staticmethod
    def _extract_images(text: str) -> list[ChunkImage]:
        """从 markdown 文本中提取 ![alt](path) 形式的图片引用。"""
        images: list[ChunkImage] = []
        seen_paths: set[str] = set()
        for m in _IMAGE_RE.finditer(text):
            alt = m.group(1).strip()
            path = m.group(2).strip()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            images.append(ChunkImage(path=path, caption=alt))
        return images
