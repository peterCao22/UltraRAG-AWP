"""Parser Protocol —— Phase 4 解析器统一接口。

所有解析器（DocxParser / MineruParser / DoclingParser / MarkdownParser）
都实现这个 Protocol，由 factory 按 (kb_type, file_ext) 路由。

设计原则：
    - 文件级接口：每次解析单个文件（批量逻辑由调用方循环或并行调度）
    - 副作用允许：图片/中间产物落地到 kb_root 下的标准子目录
        - 图片：kb_root/images/<doc_stem>/<img_name>
        - 临时文件：parser 自行清理
    - 返回值：纯 Chunk 列表，不直接写 JSONL（由调用方统一序列化）
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from custom_app.services.parsers.schema import Chunk


@runtime_checkable
class Parser(Protocol):
    """文档解析器 Protocol。

    实现类需保证：
        1. parse() 是无状态的（实例字段只允许是配置 / 模型句柄）
        2. 失败应抛异常，不返回空列表掩盖错误
        3. chunk.kb_id 由调用方填充，parser 留空字符串即可
        4. chunk.source_type / parser 字段必须正确填写
    """

    def parse(self, file_path: Path, kb_root: Path) -> list[Chunk]:
        """解析单个文件，返回 Chunk 列表。

        Args:
            file_path: 待解析的文件绝对路径
            kb_root:   该 KB 的根目录（用于图片/表格等附件落地的相对路径基准）

        Returns:
            解析得到的 Chunk 列表；顺序应反映原文档的阅读顺序。

        Raises:
            FileNotFoundError: file_path 不存在
            ValueError:        文件格式不被该 parser 支持
            RuntimeError:      外部依赖（CLI / 模型）执行失败
        """
        ...
