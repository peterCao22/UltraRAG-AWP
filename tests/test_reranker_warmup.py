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


class TestWarmupRemoteProbe:
    """Phase 11.1.D：remote 模式探活——发一次最小 rerank 请求。"""

    def test_remote_backend_sends_probe_request(self) -> None:
        runner = _make_stub_runner(rerank_cfg={"enabled": True})
        mock_model = MagicMock()
        mock_model.device = "remote"
        with patch.object(runner, "_ensure_rerank_model", return_value=mock_model):
            runner._warmup_reranker()
        # remote 模式应该真发一次 rerank
        mock_model.rerank.assert_called_once()
        # 探活用最小 payload
        call = mock_model.rerank.call_args
        assert call.args[0] == "warmup"
        assert call.args[1] == ["ping", "pong"]

    def test_local_backend_does_not_send_probe(self) -> None:
        """local 模式权重已加载，不应再发 rerank 请求。"""
        runner = _make_stub_runner(rerank_cfg={"enabled": True})
        mock_model = MagicMock()
        mock_model.device = "cuda"
        with patch.object(runner, "_ensure_rerank_model", return_value=mock_model):
            runner._warmup_reranker()
        mock_model.rerank.assert_not_called()

    def test_remote_probe_failure_is_swallowed(self) -> None:
        """探活失败（网络挂了）不阻塞 init，留给运行时 fallback。"""
        runner = _make_stub_runner(rerank_cfg={"enabled": True})
        mock_model = MagicMock()
        mock_model.device = "remote"
        mock_model.rerank.side_effect = RuntimeError("connection refused")
        with patch.object(runner, "_ensure_rerank_model", return_value=mock_model):
            # 不应抛错
            runner._warmup_reranker()
