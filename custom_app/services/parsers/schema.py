"""Phase 4 统一 Chunk schema。

向后兼容设计：
    - 老字段 id / title / contents / doc / images 完全保留
    - 老 chunks.jsonl 仍可读（嵌入/检索侧用 .get 兼容性访问新字段）
    - 新字段 source_type / parser / structure / tables / vector_id 全部可选
    - images 从字符串数组升级为对象数组；解析侧统一输出对象数组，下游兼容旧字符串

序列化策略：
    通过 to_jsonl_dict() 输出 dict，便于写入 JSONL；
    空数组 / None 值显式保留，避免下游 KeyError。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ChunkImage:
    """chunk 内嵌图片的统一描述。

    img_id 作为稳定标识，为 Phase 5+ 视觉检索（CLIP）留钩子。
    """

    path: str
    caption: str = ""
    page_idx: Optional[int] = None
    img_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "caption": self.caption,
            "page_idx": self.page_idx,
            "img_id": self.img_id,
        }


@dataclass(frozen=True)
class ChunkTable:
    """chunk 内嵌表格的统一描述。

    markdown 字段直接存表格的 markdown 文本，便于嵌入/展示。
    """

    markdown: str
    caption: str = ""
    page_idx: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "caption": self.caption,
            "page_idx": self.page_idx,
        }


@dataclass(frozen=True)
class ChunkStructure:
    """chunk 的结构化元数据。

    字段约定：
        heading_path  : 标题层级链（["第3章", "3.2 子节"]），检索时可做父级加权
        heading_level : 当前 chunk 所属标题等级，0 表示正文
        step_number   : 仅 SOP 文档有 STEP 时填，其他场景为 None
        page_idx      : 页码（0-based），无分页概念的文档为 None
    """

    heading_path: tuple[str, ...] = field(default_factory=tuple)
    heading_level: int = 0
    step_number: Optional[int] = None
    page_idx: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading_path": list(self.heading_path),
            "heading_level": self.heading_level,
            "step_number": self.step_number,
            "page_idx": self.page_idx,
        }


@dataclass(frozen=True)
class Chunk:
    """Phase 4 统一 Chunk 数据类。

    与 Phase 3 chunks.jsonl 完全兼容：老 chunks.jsonl 无 source_type / parser /
    structure / tables / vector_id 字段时，from_jsonl_dict 会给默认值。
    """

    id: str
    title: str
    contents: str
    doc: str
    kb_id: str = ""
    source_type: str = "sop_docx"
    parser: str = "docx_parser"
    structure: ChunkStructure = field(default_factory=ChunkStructure)
    images: tuple[ChunkImage, ...] = field(default_factory=tuple)
    tables: tuple[ChunkTable, ...] = field(default_factory=tuple)
    vector_id: Optional[str] = None

    def to_jsonl_dict(self) -> dict[str, Any]:
        """序列化为可写入 chunks.jsonl 的 dict。

        老字段（id/title/contents/doc/images）放在前面保持视觉一致；
        images 输出为对象数组（每项 path/caption/page_idx/img_id）。
        """
        return {
            "id": self.id,
            "title": self.title,
            "contents": self.contents,
            "doc": self.doc,
            "kb_id": self.kb_id,
            "images": [img.to_dict() for img in self.images],
            "source_type": self.source_type,
            "parser": self.parser,
            "structure": self.structure.to_dict(),
            "tables": [tbl.to_dict() for tbl in self.tables],
            "vector_id": self.vector_id,
        }

    @classmethod
    def from_jsonl_dict(cls, data: dict[str, Any]) -> Chunk:
        """从 chunks.jsonl 的一行 dict 重建 Chunk。

        向后兼容：老 chunks.jsonl 无新字段时填默认值；
        images 字段同时兼容字符串数组（老格式）和对象数组（新格式）。
        """
        images_raw = data.get("images") or []
        images: list[ChunkImage] = []
        for item in images_raw:
            if isinstance(item, str):
                images.append(ChunkImage(path=item))
            elif isinstance(item, dict):
                images.append(
                    ChunkImage(
                        path=item.get("path", ""),
                        caption=item.get("caption", ""),
                        page_idx=item.get("page_idx"),
                        img_id=item.get("img_id"),
                    )
                )

        tables_raw = data.get("tables") or []
        tables = tuple(
            ChunkTable(
                markdown=t.get("markdown", ""),
                caption=t.get("caption", ""),
                page_idx=t.get("page_idx"),
            )
            for t in tables_raw
            if isinstance(t, dict)
        )

        struct_raw = data.get("structure") or {}
        structure = ChunkStructure(
            heading_path=tuple(struct_raw.get("heading_path") or []),
            heading_level=int(struct_raw.get("heading_level") or 0),
            step_number=struct_raw.get("step_number"),
            page_idx=struct_raw.get("page_idx"),
        )

        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            contents=data.get("contents", ""),
            doc=data.get("doc", ""),
            kb_id=data.get("kb_id", ""),
            source_type=data.get("source_type", "sop_docx"),
            parser=data.get("parser", "docx_parser"),
            structure=structure,
            images=tuple(images),
            tables=tables,
            vector_id=data.get("vector_id"),
        )
