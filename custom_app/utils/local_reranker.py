# -*- coding: utf-8 -*-

"""
本地 Reranker 工具类
模型：bge-reranker-v2-m3
实现方式：直接使用 transformers 加载 AutoModelForSequenceClassification

适用场景：
1. 当前项目已经使用较新的 transformers / sentence-transformers
2. 不想因为 FlagEmbedding 降级项目依赖
3. 只需要在 RAG 中对候选文档做 rerank

典型流程：
向量检索 / 全文检索 Top 30 / Top 50
    ↓
LocalReranker.rerank()
    ↓
取 Top 3 / Top 5
    ↓
交给大模型生成答案

注意：
不要每次请求都初始化 LocalReranker。
应该在项目启动时初始化一次，然后反复调用 rerank()。
"""

from typing import Any, Dict, List, Optional, TYPE_CHECKING

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

if TYPE_CHECKING:
    pass  # 占位：保留 forward-ref 友好结构


class LocalReranker:
    def __init__(
        self,
        model_path: str = r"C:\reranker\bge-reranker-v2-m3",
        max_length: int = 1024,
        batch_size: int = 4,
        normalize: bool = True,
        use_fp16: bool = True,
        device: str = "auto",
    ):
        """
        初始化本地 reranker 模型

        :param model_path: 本地模型路径（Phase 4 起从 YAML rag_rerank.model_name_or_path 注入）
        :param max_length: query + document 的最大 token 长度
        :param batch_size: 批量打分大小，RTX 2080 8GB 建议 2~4
        :param normalize: 是否使用 sigmoid 把分数压缩到 0~1
        :param use_fp16: GPU 环境建议 True，CPU 环境自动使用 float32
        :param device:   "auto" / "cuda" / "cpu"；auto 时优先 CUDA，加载失败自动 fallback CPU
        """

        self.model_path = model_path
        self.max_length = max_length
        self.batch_size = batch_size
        self.normalize = normalize
        self.use_fp16 = use_fp16

        preferred = (device or "auto").lower()
        if preferred == "cuda":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        elif preferred == "cpu":
            self.device = "cpu"
        else:  # auto
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.device_name = (
            torch.cuda.get_device_name(0) if self.device == "cuda" else "CPU"
        )

        print("[LocalReranker] Device:", self.device_name)
        print("[LocalReranker] Loading tokenizer:", self.model_path)

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_path,
            local_files_only=True
        )

        print("[LocalReranker] Loading model:", self.model_path)

        # CUDA fallback CPU：加载/转移到 CUDA 失败时降级到 CPU，避免硬挂
        self.model = self._load_model_with_fallback()
        self.model.eval()

        print("[LocalReranker] Model loaded successfully on", self.device_name)

    def _load_model_with_fallback(self):
        """加载模型并搬到目标 device；CUDA 失败时降级 CPU。"""
        target_dtype = (
            torch.float16 if (self.device == "cuda" and self.use_fp16) else torch.float32
        )
        try:
            model = AutoModelForSequenceClassification.from_pretrained(
                self.model_path,
                dtype=target_dtype,
                local_files_only=True,
            )
            model.to(self.device)
            return model
        except Exception as cuda_err:
            if self.device != "cuda":
                # 非 CUDA 路径上失败就直接抛
                raise
            print(
                f"[LocalReranker] CUDA load failed ({cuda_err}); falling back to CPU"
            )
            self.device = "cpu"
            self.device_name = "CPU"
            model = AutoModelForSequenceClassification.from_pretrained(
                self.model_path,
                dtype=torch.float32,
                local_files_only=True,
            )
            model.to("cpu")
            return model

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: int = 5,
        min_score: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        对候选文档进行重排序

        :param query: 用户问题
        :param documents: 候选文档文本列表
        :param top_k: 返回前多少条
        :param min_score: 最低分数过滤，例如 0.1 / 0.3 / 0.5；不传则不过滤
        :return: 排序后的结果列表
        """

        if query is None or str(query).strip() == "":
            raise ValueError("query 不能为空")

        if not documents:
            return []

        clean_documents = []

        for doc in documents:
            if doc is None:
                continue

            doc_text = str(doc).strip()

            if doc_text == "":
                continue

            clean_documents.append(doc_text)

        if not clean_documents:
            return []

        all_results = []

        for start_index in range(0, len(clean_documents), self.batch_size):
            batch_docs = clean_documents[start_index:start_index + self.batch_size]

            batch_scores = self._compute_batch_scores(
                query=query,
                documents=batch_docs
            )

            for doc, score in zip(batch_docs, batch_scores):
                score_value = float(score)

                if min_score is not None and score_value < min_score:
                    continue

                all_results.append({
                    "document": doc,
                    "score": score_value
                })

        all_results.sort(key=lambda item: item["score"], reverse=True)

        if top_k is not None and top_k > 0:
            all_results = all_results[:top_k]

        for index, item in enumerate(all_results, start=1):
            item["rank"] = index

        return all_results

    def rerank_items(
        self,
        query: str,
        items: List[Dict[str, Any]],
        content_key: str = "content",
        top_k: int = 5,
        min_score: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        对带 metadata 的文档对象进行重排序

        示例输入：
        [
            {
                "id": 1,
                "title": "订单规则",
                "content": "公司订单截止时间是美国中部时间中午 12 点。"
            }
        ]

        :param query: 用户问题
        :param items: 候选文档对象列表
        :param content_key: 文档正文所在字段名
        :param top_k: 返回前多少条
        :param min_score: 最低分数过滤
        :return: 保留原始字段，并追加 score 和 rank
        """

        if query is None or str(query).strip() == "":
            raise ValueError("query 不能为空")

        if not items:
            return []

        valid_items = []
        documents = []

        for item in items:
            if item is None:
                continue

            content = item.get(content_key)

            if content is None:
                continue

            content_text = str(content).strip()

            if content_text == "":
                continue

            valid_items.append(item)
            documents.append(content_text)

        if not valid_items:
            return []

        scored_results = []

        for start_index in range(0, len(documents), self.batch_size):
            batch_docs = documents[start_index:start_index + self.batch_size]
            batch_items = valid_items[start_index:start_index + self.batch_size]

            batch_scores = self._compute_batch_scores(
                query=query,
                documents=batch_docs
            )

            for item, score in zip(batch_items, batch_scores):
                score_value = float(score)

                if min_score is not None and score_value < min_score:
                    continue

                new_item = dict(item)
                new_item["score"] = score_value
                scored_results.append(new_item)

        scored_results.sort(key=lambda item: item["score"], reverse=True)

        if top_k is not None and top_k > 0:
            scored_results = scored_results[:top_k]

        for index, item in enumerate(scored_results, start=1):
            item["rank"] = index

        return scored_results

    def _compute_batch_scores(
        self,
        query: str,
        documents: List[str]
    ) -> List[float]:
        """
        批量计算 query 和 documents 的相关性分数
        """

        queries = [query] * len(documents)

        inputs = self.tokenizer(
            queries,
            documents,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt"
        )

        inputs = {
            key: value.to(self.device)
            for key, value in inputs.items()
        }

        with torch.inference_mode():
            outputs = self.model(**inputs)

            logits = outputs.logits

            if logits.dim() == 2 and logits.size(1) > 1:
                raw_scores = logits[:, -1]
            else:
                raw_scores = logits.view(-1)

            if self.normalize:
                scores = torch.sigmoid(raw_scores)
            else:
                scores = raw_scores

            return scores.detach().float().cpu().tolist()

    def warmup(self) -> None:
        """
        预热模型，项目启动后可以调用一次
        """

        self.rerank(
            query="测试问题",
            documents=[
                "这是一段用于预热 reranker 模型的测试文本。",
                "这是一段无关文本。"
            ],
            top_k=1
        )

    def get_device_info(self) -> Dict[str, Any]:
        """
        获取当前运行设备信息
        """

        return {
            "device": self.device,
            "device_name": self.device_name,
            "model_path": self.model_path,
            "max_length": self.max_length,
            "batch_size": self.batch_size,
            "normalize": self.normalize,
            "use_fp16": self.use_fp16
        }


_default_reranker: Optional["LocalReranker"] = None
_default_reranker_config: Optional[Dict[str, Any]] = None


def get_default_reranker(
    model_path: Optional[str] = None,
    batch_size: Optional[int] = None,
    device: str = "auto",
    max_length: int = 1024,
    normalize: bool = True,
    use_fp16: bool = True,
) -> "LocalReranker":
    """
    获取默认 reranker 单例

    Phase 4 起 model_path / batch_size / device 可从外部注入
    （RagRunner 从 servers/retriever/parameter.yaml 的 rag_rerank 段读取）。

    单例策略：第一次调用时按传入参数加载，后续调用忽略参数复用同一个实例。
    如需切换模型路径，应重启进程或显式 reset_default_reranker()。
    """

    global _default_reranker, _default_reranker_config

    if _default_reranker is None:
        resolved_path = model_path or r"C:\reranker\bge-reranker-v2-m3"
        resolved_batch = batch_size if batch_size is not None else 4
        _default_reranker = LocalReranker(
            model_path=resolved_path,
            max_length=max_length,
            batch_size=resolved_batch,
            normalize=normalize,
            use_fp16=use_fp16,
            device=device,
        )
        _default_reranker_config = {
            "model_path": resolved_path,
            "batch_size": resolved_batch,
            "device": device,
        }

    return _default_reranker


def reset_default_reranker() -> None:
    """清空单例，下次 get_default_reranker 会重新加载（仅测试/重配置用）。"""
    global _default_reranker, _default_reranker_config
    _default_reranker = None
    _default_reranker_config = None


if __name__ == "__main__":
    reranker = get_default_reranker()

    print(reranker.get_device_info())

    query = "如何在 Windows 上使用 bge-reranker-v2-m3？"

    docs = [
        "bge-reranker-v2-m3 可以作为 reranker 模型，对 query 和 document 进行相关性打分。",
        "Windows 上可以使用 CUDA 版 PyTorch 加载本地模型。",
        "MySQL 是一个关系型数据库，常用于业务系统存储数据。",
        "瓷砖仓库需要管理库存、批次、库位和出入库记录。"
    ]

    results = reranker.rerank(
        query=query,
        documents=docs,
        top_k=5
    )

    for item in results:
        print("=" * 80)
        print("Rank:", item["rank"])
        print("Score:", item["score"])
        print("Document:", item["document"])