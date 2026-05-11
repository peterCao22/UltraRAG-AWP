"""Phase 4 重排序包：Reranker Protocol 与具体实现。

模块结构：
    base —— Reranker Protocol（无重型依赖）

注意：LocalReranker（依赖 torch/transformers）位于 custom_app/utils/local_reranker.py，
未来加 HttpReranker 时再迁到本包。

Phase 5+ 计划：
    http_reranker —— 通过 HTTP 调用远端 reranker 服务（GPU 服务器集中部署）
"""

from custom_app.services.reranker.base import Reranker, RerankedItem

__all__ = ["Reranker", "RerankedItem"]
