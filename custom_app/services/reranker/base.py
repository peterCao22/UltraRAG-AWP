"""Reranker Protocol —— Phase 4 重排序统一接口。

为 Phase 4 之后 reranker 服务化（HttpReranker）预留接口。
LocalReranker（本地加载 bge-reranker-v2-m3）与未来的 HttpReranker
都实现这个 Protocol，RagRunner 通过依赖注入选择实现。

设计要点：
    - rerank_items 接收 dict 列表（含业务字段），返回原 dict + score/rank 字段
    - 不强约束 batch_size / max_length 等参数，交给具体实现
    - 失败应抛异常，不返回空列表掩盖错误（让上层决定降级策略）
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# 类型别名：rerank 后的 item dict 形态（原字段 + score + rank）
RerankedItem = dict[str, Any]


@runtime_checkable
class Reranker(Protocol):
    """重排序器 Protocol。

    实现类需保证：
        1. 同一 query 对同一 documents 结果稳定（除浮点误差外）
        2. 失败抛异常（CUDA OOM、网络错误等），不静默返回空
        3. 不修改入参 items（应返回新 dict 列表）
    """

    def rerank_items(
        self,
        query: str,
        items: list[dict[str, Any]],
        content_key: str = "content",
        top_k: int = 5,
        min_score: float | None = None,
    ) -> list[RerankedItem]:
        """对带 metadata 的文档 dict 列表进行重排序。

        Args:
            query:       用户问题（与检索/嵌入侧使用的查询文本一致）
            items:       候选文档 dict 列表，必须含 content_key 字段
            content_key: 文档正文字段名，默认 "content"
            top_k:       返回前 K 个；<=0 表示不截断
            min_score:   最低分数过滤；None 表示不过滤

        Returns:
            排序后的 dict 列表，每项保留原字段并追加 `score: float` 和 `rank: int`。
        """
        ...
