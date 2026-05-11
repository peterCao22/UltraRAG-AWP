"""MineruParser —— 用 MinerU CLI 解析 PDF / 图片，输出 Phase 4 统一 Chunk。

部署要求：
    - pip install mineru[core] 或 uv sync --extras parsing
    - 首次运行会自动下载 GB 级模型
    - Office 文档解析需要系统级 libreoffice CLI（本类不直接处理 office；
      .docx 走 DoclingParser，避免 PDF 中转有损）
    - 推荐 GPU；CPU 模式约慢 5-10x

实现要点：
    - 调用 mineru CLI：`mineru -p <input> -o <output> -m auto`
    - 读取 `<output>/<stem>_content_list.json` 或 `<output>/<stem>/auto/<stem>_content_list.json`
    - **不调用 RAG-Anything 的 separate_content()**：那会丢失图文邻近关系
    - 直接在 content_list 上按 `text_level` 重建标题树切块
    - 图片相对化到 `kb_root/images/<doc_stem>/` 下，与 docx_parser 一致
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from custom_app.services.parsers.schema import (
    Chunk,
    ChunkImage,
    ChunkStructure,
    ChunkTable,
)

logger = logging.getLogger(__name__)

# MinerU CLI 默认子进程超时（秒）；大 PDF 可能需要更久，调用方可在环境变量里覆盖
_DEFAULT_TIMEOUT_SEC = 1800


class MineruParser:
    """PDF / 图片解析器（基于 MinerU 2.x）。"""

    def __init__(
        self,
        *,
        method: str = "auto",
        lang: Optional[str] = None,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        mineru_executable: str = "mineru",
    ) -> None:
        """
        Args:
            method:            MinerU 解析方法（auto / txt / ocr）
            lang:              文档主语言（zh / en / ...）；None 表示让 MinerU 自动检测
            timeout_sec:       子进程超时
            mineru_executable: CLI 名称（默认 "mineru"，便于测试 mock）
        """
        self.method = method
        self.lang = lang
        self.timeout_sec = timeout_sec
        self.mineru_executable = mineru_executable

    # ------------------------------------------------------------------
    # Parser Protocol
    # ------------------------------------------------------------------

    def parse(self, file_path: Path, kb_root: Path) -> list[Chunk]:
        if not file_path.exists():
            raise FileNotFoundError(f"file not found: {file_path}")

        ext = file_path.suffix.lower()
        if ext not in {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}:
            raise ValueError(f"unsupported extension for MineruParser: {ext}")

        self._check_executable()

        doc_stem = file_path.stem
        # MinerU 输出到临时目录，再把图片搬到 kb_root/images/<doc_stem>/
        with tempfile.TemporaryDirectory(prefix="mineru_") as tmp:
            tmp_dir = Path(tmp)
            self._run_cli(file_path, tmp_dir)
            content_list_path, images_dir = self._locate_outputs(tmp_dir, doc_stem)
            content_list = self._read_content_list(content_list_path)

            kb_image_dir = kb_root / "images" / doc_stem
            kb_image_dir.mkdir(parents=True, exist_ok=True)
            image_map = self._copy_images_to_kb(
                images_dir, kb_image_dir, doc_stem
            )

        chunks = self._build_chunks_from_content_list(
            content_list,
            doc_stem=doc_stem,
            ext=ext,
            image_map=image_map,
        )
        return chunks

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _check_executable(self) -> None:
        if shutil.which(self.mineru_executable) is None:
            raise RuntimeError(
                f"mineru executable not found in PATH (looked for {self.mineru_executable!r}). "
                "Install with: uv sync --extras parsing"
            )

    def _run_cli(self, input_path: Path, output_dir: Path) -> None:
        cmd = [
            self.mineru_executable,
            "-p", str(input_path),
            "-o", str(output_dir),
            "-m", self.method,
        ]
        if self.lang:
            cmd.extend(["-l", self.lang])

        logger.info("running mineru: %s", " ".join(cmd))
        try:
            # Windows: 隐藏控制台窗口
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_sec,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"mineru timed out after {self.timeout_sec}s on {input_path.name}"
            ) from e

        if result.returncode != 0:
            tail_stderr = (result.stderr or "")[-2000:]
            raise RuntimeError(
                f"mineru CLI failed (exit {result.returncode}) on {input_path.name}: "
                f"{tail_stderr}"
            )

    @staticmethod
    def _locate_outputs(
        output_dir: Path, doc_stem: str
    ) -> tuple[Path, Optional[Path]]:
        """定位 MinerU 输出的 content_list.json 与 images 目录。

        MinerU 2.x 输出结构有两种：
            扁平：<output_dir>/<stem>_content_list.json
            嵌套：<output_dir>/<stem>/auto/<stem>_content_list.json
        """
        candidates = [
            output_dir / f"{doc_stem}_content_list.json",
            output_dir / doc_stem / "auto" / f"{doc_stem}_content_list.json",
            output_dir / doc_stem / "vlm" / f"{doc_stem}_content_list.json",
        ]
        # 兜底：递归查找
        for cand in candidates:
            if cand.exists():
                return cand, cand.parent / "images"
        # 兜底扫描
        for path in output_dir.rglob(f"{doc_stem}_content_list.json"):
            return path, path.parent / "images"
        raise RuntimeError(
            f"mineru output not found: expected {doc_stem}_content_list.json under {output_dir}"
        )

    @staticmethod
    def _read_content_list(path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"invalid mineru output {path}: {e}") from e
        if not isinstance(data, list):
            raise RuntimeError(
                f"mineru content_list should be a JSON array, got {type(data).__name__}"
            )
        return data

    @staticmethod
    def _copy_images_to_kb(
        src_images_dir: Optional[Path],
        kb_image_dir: Path,
        doc_stem: str,
    ) -> dict[str, str]:
        """把 MinerU 输出图片搬到 KB 的标准位置，返回 (原相对路径 -> KB 相对路径) 映射。

        映射 key 是 content_list 中 img_path 字段的原始值；
        映射 value 是相对于 kb_root 的路径（如 "images/<stem>/img_0001.png"）。
        """
        if src_images_dir is None or not src_images_dir.exists():
            return {}
        mapping: dict[str, str] = {}
        for idx, src in enumerate(sorted(src_images_dir.iterdir()), start=1):
            if not src.is_file():
                continue
            # 统一重命名为 img_NNNN.<ext>，便于追踪
            ext = src.suffix.lower() or ".png"
            new_name = f"img_{idx:04d}{ext}"
            dst = kb_image_dir / new_name
            shutil.copy2(src, dst)
            # MinerU 在 content_list 里写的 img_path 可能是 "images/xxx.png" 或绝对路径
            # 都用 basename 做 key，匹配时再 normalize
            mapping[src.name] = f"images/{doc_stem}/{new_name}"
        return mapping

    @classmethod
    def _build_chunks_from_content_list(
        cls,
        content_list: list[dict[str, Any]],
        *,
        doc_stem: str,
        ext: str,
        image_map: dict[str, str],
    ) -> list[Chunk]:
        """直接在 content_list 上跑分块逻辑（不调 separate_content）。

        分块策略：
            - 按 text_level >= 1 的 text item 作为切分点（标题）
            - 标题之间的所有 item 归属当前 section
            - 标题更新 heading_stack（6 级深度，参考 markdown_parser）
        """
        source_type = "image" if ext != ".pdf" else "general_pdf"

        heading_stack: list[Optional[str]] = [None] * 6
        current_heading_level = 0
        current_title = doc_stem.replace("_", " ")
        current_text_lines: list[str] = []
        current_images: list[ChunkImage] = []
        current_tables: list[ChunkTable] = []
        chunks: list[Chunk] = []
        section_idx = 0

        def emit() -> None:
            nonlocal section_idx
            body = "\n".join(line for line in current_text_lines if line).strip()
            if not body and not current_images and not current_tables:
                return
            section_idx += 1
            path = tuple(h for h in heading_stack if h)
            chunks.append(
                Chunk(
                    id=f"{doc_stem}_section_{section_idx}",
                    title=current_title,
                    contents=body,
                    doc=doc_stem,
                    source_type=source_type,
                    parser="mineru",
                    structure=ChunkStructure(
                        heading_path=path,
                        heading_level=current_heading_level,
                        page_idx=cls._first_page_idx_in(
                            current_text_lines, current_images, current_tables
                        ),
                    ),
                    images=tuple(current_images),
                    tables=tuple(current_tables),
                )
            )

        for item in content_list:
            item_type = item.get("type", "")
            page_idx = item.get("page_idx")

            if item_type == "text":
                text_level = int(item.get("text_level") or 0)
                text = (item.get("text") or "").strip()
                if not text:
                    continue
                if text_level >= 1:
                    # 标题：先 emit 当前 section，再开启新 section
                    emit()
                    current_text_lines = []
                    current_images = []
                    current_tables = []
                    level = min(text_level, 6)
                    heading_stack[level - 1] = text
                    for deeper in range(level, 6):
                        heading_stack[deeper] = None
                    current_heading_level = level
                    current_title = text
                else:
                    # 普通段落
                    current_text_lines.append(text)
            elif item_type == "image":
                img_rel = cls._resolve_image_path(
                    item.get("img_path", ""), image_map
                )
                caption_list = item.get("image_caption") or item.get("img_caption") or []
                caption = cls._join_caption(caption_list)
                if img_rel:
                    current_images.append(
                        ChunkImage(
                            path=img_rel,
                            caption=caption,
                            page_idx=page_idx,
                            img_id=f"{doc_stem}_img_{len(current_images) + 1:04d}",
                        )
                    )
                    # 同时在正文里保留位置占位（与 docx_parser 一致）
                    current_text_lines.append(f"[IMG: {img_rel}]")
            elif item_type == "table":
                table_md = item.get("table_body") or ""
                caption_list = item.get("table_caption") or []
                caption = cls._join_caption(caption_list)
                if table_md:
                    current_tables.append(
                        ChunkTable(
                            markdown=table_md,
                            caption=caption,
                            page_idx=page_idx,
                        )
                    )
                    # 表格也放到正文里，让嵌入和 LLM 都能看到
                    if caption:
                        current_text_lines.append(f"[表格: {caption}]")
                    current_text_lines.append(table_md)
            elif item_type == "equation":
                latex = (item.get("latex") or "").strip()
                if latex:
                    current_text_lines.append(f"$$\n{latex}\n$$")

        # 末尾 emit
        emit()

        # 整文档无标题：fallback 单 chunk
        if not chunks:
            return cls._build_fallback_chunk(
                content_list, doc_stem, ext, image_map
            )
        return chunks

    @staticmethod
    def _resolve_image_path(
        raw_path: str, image_map: dict[str, str]
    ) -> str:
        """把 content_list 里的 img_path 映射到 KB 相对路径。"""
        if not raw_path:
            return ""
        # MinerU 通常输出 "images/xxx.png" 或裸文件名
        basename = Path(raw_path).name
        if basename in image_map:
            return image_map[basename]
        # 未在映射中：保留原值（少见，记日志便于排查）
        logger.warning("unmapped image path from mineru: %s", raw_path)
        return raw_path

    @staticmethod
    def _join_caption(captions: Any) -> str:
        if isinstance(captions, list):
            return ", ".join(str(c) for c in captions if c)
        if isinstance(captions, str):
            return captions
        return ""

    @staticmethod
    def _first_page_idx_in(
        text_lines: list[str],
        images: list[ChunkImage],
        tables: list[ChunkTable],
    ) -> Optional[int]:
        """从 chunk 内任意带 page_idx 的元素提取代表性页码。"""
        for img in images:
            if img.page_idx is not None:
                return img.page_idx
        for tbl in tables:
            if tbl.page_idx is not None:
                return tbl.page_idx
        return None

    @classmethod
    def _build_fallback_chunk(
        cls,
        content_list: list[dict[str, Any]],
        doc_stem: str,
        ext: str,
        image_map: dict[str, str],
    ) -> list[Chunk]:
        """整文档无标题时的兜底：聚合所有 text/image/table。"""
        source_type = "image" if ext != ".pdf" else "general_pdf"
        text_lines: list[str] = []
        images: list[ChunkImage] = []
        tables: list[ChunkTable] = []
        for item in content_list:
            t = item.get("type", "")
            if t == "text":
                txt = (item.get("text") or "").strip()
                if txt:
                    text_lines.append(txt)
            elif t == "image":
                img_rel = cls._resolve_image_path(item.get("img_path", ""), image_map)
                if img_rel:
                    images.append(
                        ChunkImage(
                            path=img_rel,
                            caption=cls._join_caption(item.get("image_caption")),
                            page_idx=item.get("page_idx"),
                            img_id=f"{doc_stem}_img_{len(images) + 1:04d}",
                        )
                    )
                    text_lines.append(f"[IMG: {img_rel}]")
            elif t == "table":
                table_md = item.get("table_body") or ""
                if table_md:
                    tables.append(
                        ChunkTable(
                            markdown=table_md,
                            caption=cls._join_caption(item.get("table_caption")),
                            page_idx=item.get("page_idx"),
                        )
                    )
                    text_lines.append(table_md)
        body = "\n".join(line for line in text_lines if line).strip()
        if not body and not images and not tables:
            return []
        return [
            Chunk(
                id=f"{doc_stem}_chunk_1",
                title=doc_stem.replace("_", " "),
                contents=body,
                doc=doc_stem,
                source_type=source_type,
                parser="mineru",
                structure=ChunkStructure(
                    heading_path=tuple(),
                    heading_level=0,
                ),
                images=tuple(images),
                tables=tuple(tables),
            )
        ]
