"""Phase 11.1.A —— Reranker 启动预热单元测试。

策略：mock _ensure_rerank_model；验证 _warmup_reranker 在不同配置下的行为：
- disabled → 不调用 ensure 函数
- enabled + 有 rows → 调用 ensure 触发加载
- enabled + 无 rows → 不调用（避免空 KB 浪费）
- ensure 抛异常 → warmup 静默降级，不传播
- ensure 返回 None（加载失败）→ warmup 不报错
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_app.services.rag_runner import RagRunner


def _make_stub_runner(*, rerank_cfg: dict, rows: list[dict] | None = None) -> RagRunner:
    """构造一个最小化 stub runner（不真起 init）。"""
    runner = RagRunner.__new__(RagRunner)
    runner.kb_id = "test_kb"
    runner._rerank_cfg = rerank_cfg
    runner._rows = rows if rows is not None else [{"id": "x"}]
    runner._rerank_resolved_device = "cpu"
    return runner


class TestWarmupReranker:
    def test_disabled_skips_warmup(self) -> None:
        runner = _make_stub_runner(rerank_cfg={"enabled": False})
        with patch.object(runner, "_ensure_rerank_model") as mock_ensure:
            runner._warmup_reranker()
        mock_ensure.assert_not_called()

    def test_no_rows_skips_warmup(self) -> None:
        runner = _make_stub_runner(rerank_cfg={"enabled": True}, rows=[])
        with patch.object(runner, "_ensure_rerank_model") as mock_ensure:
            runner._warmup_reranker()
        mock_ensure.assert_not_called()

    def test_enabled_with_rows_triggers_load(self) -> None:
        runner = _make_stub_runner(rerank_cfg={"enabled": True})
        mock_model = MagicMock()
        mock_model.device = "cuda"
        with patch.object(
            runner, "_ensure_rerank_model", return_value=mock_model
        ) as mock_ensure:
            runner._warmup_reranker()
        mock_ensure.assert_called_once()

    def test_ensure_returns_none_no_error(self) -> None:
        """加载失败（ensure 返回 None）时 warmup 不抛错。"""
        runner = _make_stub_runner(rerank_cfg={"enabled": True})
        with patch.object(runner, "_ensure_rerank_model", return_value=None):
            # 不应抛错
            runner._warmup_reranker()

    def test_ensure_raises_is_swallowed(self) -> None:
        """ensure 抛任何异常都被 warmup 吞掉，不阻塞 init。"""
        runner = _make_stub_runner(rerank_cfg={"enabled": True})
        with patch.object(
            runner, "_ensure_rerank_model", side_effect=RuntimeError("CUDA OOM"),
        ):
            # 不应抛错
            runner._warmup_reranker()

    def test_enabled_defaults_to_true(self) -> None:
        """rerank_cfg 未显式设 enabled 时按 True 处理（向后兼容）。"""
        runner = _make_stub_runner(rerank_cfg={})
        mock_model = MagicMock()
        mock_model.device = "cpu"
        with patch.object(
            runner, "_ensure_rerank_model", return_value=mock_model
        ) as mock_ensure:
            runner._warmup_reranker()
        mock_ensure.assert_called_once()

    def test_empty_cfg_dict_safe(self) -> None:
        """_rerank_cfg 为 None 时不报错（向后兼容老 init 路径）。"""
        runner = _make_stub_runner(rerank_cfg={})
        runner._rerank_cfg = None
        # 不应抛错
        runner._warmup_reranker()
