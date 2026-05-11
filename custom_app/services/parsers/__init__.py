"""Phase 4 解析器包：统一的文档解析接口与多格式实现。

模块结构：
    schema           —— Chunk / ChunkStructure / ChunkImage / ChunkTable 数据类
    base             —— Parser Protocol（所有解析器的统一接口）
    factory          —— Parser 工厂，按 (kb_type, file_ext) 路由到具体实现
    markdown_parser  —— MarkdownParser（轻量，无外部依赖）
    mineru_parser    —— MineruParser（需可选依赖 mineru CLI）
    docling_parser   —— DoclingParser（需可选依赖 docling）
"""

from custom_app.services.parsers.base import Parser
from custom_app.services.parsers.factory import (
    KB_TYPE_GENERAL,
    KB_TYPE_SOP_DOCX,
    VALID_KB_TYPES,
    ParserNotAvailableError,
    get_parser,
    get_supported_extensions,
    is_supported,
    parse_files,
)
from custom_app.services.parsers.schema import (
    Chunk,
    ChunkImage,
    ChunkStructure,
    ChunkTable,
)

__all__ = [
    # schema
    "Chunk",
    "ChunkImage",
    "ChunkStructure",
    "ChunkTable",
    # protocol
    "Parser",
    # factory
    "KB_TYPE_GENERAL",
    "KB_TYPE_SOP_DOCX",
    "VALID_KB_TYPES",
    "ParserNotAvailableError",
    "get_parser",
    "get_supported_extensions",
    "is_supported",
    "parse_files",
]
