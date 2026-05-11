"""Phase 4 向量存储包：VectorStore Protocol 与具体实现。

模块结构：
    base        —— VectorStore Protocol + Hit dataclass（无 faiss 依赖）
    faiss_store —— FaissVectorStore（包装现有 FAISS 逻辑，需 faiss 包）

Phase 5 计划新增：
    qdrant_store —— QdrantVectorStore（HTTP 客户端，远端 Qdrant 服务）

注意：FaissVectorStore 不在包级导出，避免无 faiss 环境下 import 失败。
需要使用时显式 `from custom_app.services.vectorstore.faiss_store import FaissVectorStore`。
"""

from custom_app.services.vectorstore.base import Hit, VectorStore

__all__ = ["Hit", "VectorStore"]
