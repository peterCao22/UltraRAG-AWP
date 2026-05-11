"""Phase 4 解析器包：统一的文档解析接口与多格式实现。

模块结构：
    schema  —— Chunk / ChunkStructure / ChunkImage / ChunkTable 数据类
    base    —— Parser Protocol（所有解析器的统一接口）
    factory —— Parser 工厂，按 (kb_type, file_ext) 路由到具体实现
"""

from custom_app.services.parsers.schema import (
    Chunk,
    ChunkImage,
    ChunkStructure,
    ChunkTable,
)

__all__ = ["Chunk", "ChunkImage", "ChunkStructure", "ChunkTable"]
