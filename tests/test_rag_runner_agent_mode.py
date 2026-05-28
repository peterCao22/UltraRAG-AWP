"""RagRunner：agent_mode 层 A 全文扩展与 meta 契约。"""

from unittest.mock import MagicMock

import numpy as np
import pytest

from custom_app.services.rag_runner import RagRunner


@pytest.fixture()
def runner_rows():
    r = RagRunner.__new__(RagRunner)
    # Phase 7.2.A logging 需 kb_id；Phase 8.2.2 hybrid 需 _bm25_store/_retrieval_cfg；
    # Phase 11.1 修测试时统一补齐 stub 路径字段
    r.kb_id = "test_kb"
    r._chat_cfg = {"backend": "openai"}
    r._bm25_store = None
    r._bm25_load_error = None
    r._retrieval_cfg = {}
    r._rows = [
        {"id": "d1_intro", "doc": "DocA", "title": "DocA | intro", "contents": "intro"},
        {"id": "d1_s1", "doc": "DocA", "title": "DocA | STEP 1", "contents": "step1"},
        {"id": "d1_s2", "doc": "DocA", "title": "DocA | STEP 2", "contents": "step2"},
        {"id": "d2_only", "doc": "DocB", "title": "DocB | x", "contents": "other"},
        {"id": "no_doc", "doc": "", "title": "orphan", "contents": "x"},
    ]
    return r


def test_expand_quick_no_procedure_skips_full_doc(runner_rows):
    r = runner_rows
    hit_ids, expanded = r._expand_hit_ids([0], "generic question", agent_mode="quick")
    assert expanded == []
    assert hit_ids == [0]


def test_keyword_match_finds_mixed_language_alarm_name():
    r = RagRunner.__new__(RagRunner)
    r._rows = [
        {
            "id": "other",
            "doc": "Other SOP",
            "title": "Other SOP",
            "contents": "Some unrelated content",
        },
        {
            "id": "estop",
            "doc": "E-Stop SOP",
            "title": "E-Stop SOP",
            "contents": "Alarm: ID 01 E-Stop Button Active\nHow To Fix: Check both E-stop buttons.",
        },
    ]

    hits = r._keyword_match_hit_ids("E-Stop Button Active 的故障如何恢复")

    assert hits == [1]


def test_merge_preferred_hit_ids_keeps_keyword_hits_first():
    merged = RagRunner._merge_preferred_hit_ids([3, 1], [1, 2, 3, 4])

    assert merged == [3, 1, 2, 4]


def test_expand_agent_pulls_full_primary_doc(runner_rows):
    r = runner_rows
    hit_ids, expanded = r._expand_hit_ids([0], "generic question", agent_mode="agent")
    assert expanded == ["DocA"]
    assert set(hit_ids) == {0, 1, 2}


def test_expand_agent_multi_doc_narrows_to_primary(runner_rows):
    r = runner_rows
    hit_ids, expanded = r._expand_hit_ids([0, 3], "generic question", agent_mode="agent")
    assert expanded == ["DocA"]
    assert set(hit_ids) == {0, 1, 2}


def test_prepare_agent_degraded_when_no_doc_on_hits(runner_rows, monkeypatch):
    """向量仅命中无 doc 字段的 chunk 时，agent 无法做全文扩展，应标记 degraded。"""
    r = runner_rows
    r._index = MagicMock()
    r._index.search.return_value = (None, np.array([[4]], dtype="int64"))
    r._top_k = 8
    r._recall_top_k = 4
    r._final_top_k = 0
    r._rerank_cfg = {}
    r._rerank_model = None
    r._rewrite_query = lambda q: q

    monkeypatch.setattr(
        "custom_app.services.rag_runner.embed_query",
        lambda q: np.zeros((1, 4), dtype="float32"),
    )
    monkeypatch.setattr(r, "_build_prompt", lambda q, ids: "prompt")

    prep = r._prepare_chat_context("hi", agent_mode="agent")
    assert prep["degraded"] is True
    assert prep["effective_agent_mode"] == "quick"


def test_prepare_phase12_1_reference_resolution_applied(runner_rows, monkeypatch):
    """Phase 12.1: history + 含指代 query → 走 resolve_references，prep 含 reference_resolution。

    mock resolve_references 为 applied=True；验证：
      1. q 被替换为改写后的 query 进入 embed
      2. 返回 dict 含 reference_resolution.applied=True
    """
    r = runner_rows
    r._index = MagicMock()
    r._index.search.return_value = (None, np.array([[0]], dtype="int64"))
    r._top_k = 8
    r._recall_top_k = 4
    r._final_top_k = 0
    r._rerank_cfg = {}
    r._rerank_model = None

    captured_embed_text: list[str] = []
    def fake_embed(text):
        captured_embed_text.append(text)
        return np.zeros((1, 4), dtype="float32")
    monkeypatch.setattr(
        "custom_app.services.rag_runner.embed_query", fake_embed,
    )
    monkeypatch.setattr(r, "_build_prompt", lambda q, ids: "prompt")
    r._rewrite_query = lambda q: q

    # mock resolve_references 直接返回采纳的改写结果
    from custom_app.services.reference_resolver import ResolutionResult
    fake_result = ResolutionResult(
        applied=True,
        original_query="第 2 个怎么操作？",
        rewritten_query="急停按钮如何检查",
        confidence=0.92,
        resolved=[{"reference": "第 2 个", "meaning": "急停按钮"}],
        ms=120,
        model="claude-haiku-4-5-20251001",
    )
    monkeypatch.setattr(
        "custom_app.services.reference_resolver.resolve_references",
        lambda q, h: fake_result,
    )

    history = [
        {"role": "user", "content": "AGV 启动前要做什么"},
        {"role": "assistant", "content": "1. 检查电池 2. 检查急停 3. 检查导航"},
    ]
    prep = r._prepare_chat_context(
        "第 2 个怎么操作？", agent_mode="quick", history=history,
    )

    # 改写后的 query 应进入 embed
    assert captured_embed_text == ["急停按钮如何检查"]
    # prep 含指代消解 meta
    ref = prep["reference_resolution"]
    assert ref["applied"] is True
    assert ref["rewritten_query"] == "急停按钮如何检查"
    assert ref["confidence"] == 0.92


def test_prepare_phase12_1_no_history_no_resolution(runner_rows, monkeypatch):
    """无 history 时 reference_resolution.applied=False，原 query 不变。"""
    r = runner_rows
    r._index = MagicMock()
    r._index.search.return_value = (None, np.array([[0]], dtype="int64"))
    r._top_k = 8
    r._recall_top_k = 4
    r._final_top_k = 0
    r._rerank_cfg = {}
    r._rerank_model = None
    r._rewrite_query = lambda q: q

    monkeypatch.setattr(
        "custom_app.services.rag_runner.embed_query",
        lambda q: np.zeros((1, 4), dtype="float32"),
    )
    monkeypatch.setattr(r, "_build_prompt", lambda q, ids: "prompt")

    prep = r._prepare_chat_context("它怎么操作？", agent_mode="quick")  # 没传 history
    ref = prep["reference_resolution"]
    assert ref["applied"] is False
    assert ref["skip_reason"] == "no_history"


def test_build_result_merges_agent_meta(runner_rows):
    r = runner_rows
    prep = {
        "q": "q",
        "rewritten_q": "q",
        "hit_ids": [0],
        "prompt_text": "p",
        "rerank_meta": {},
        "expanded_docs": ["DocA"],
        "recall_k": 1,
        "final_k": 3,
        "final_k_cfg": 0,
        "requested_agent_mode": "agent",
        "effective_agent_mode": "agent",
        "degraded": False,
        "degrade_reason": None,
    }
    out = r._build_result_from_raw(prep, "raw")
    meta = out["meta"]
    assert meta["effective_agent_mode"] == "agent"
    assert meta["degraded"] is False


def test_quick_chat_stream_default_uses_streaming(runner_rows, monkeypatch):
    """Phase 11.1.B：quick mode 默认改流式。

    在 Phase 8 之前 quick 走非流式（注释提到 vLLM gateway 可能 hang）；
    Phase 11.1.B 评估后改成默认流式，env ULTRARAG_DISABLE_STREAM=1 可强制非流式。
    本测试验证 quick 默认走 _generate_stream，**不再调** _generate。
    """
    monkeypatch.delenv("ULTRARAG_DISABLE_STREAM", raising=False)
    r = runner_rows
    r._index = MagicMock()
    r._index.search.return_value = (None, np.array([[0]], dtype="int64"))
    r._top_k = 1
    r._recall_top_k = 1
    r._final_top_k = 0
    r._rerank_cfg = {}
    r._rerank_model = None
    r._rewrite_query = lambda q: q
    r._build_prompt = lambda q, ids: "prompt"
    r._generate = MagicMock(side_effect=AssertionError("non-stream should not be used by default"))
    r._generate_stream = MagicMock(return_value=iter(["chunk1 ", "chunk2"]))
    r._build_result_from_raw = MagicMock(return_value={
        "answer": "display answer",
        "sources": [],
        "rewrite_query": "q",
        "meta": {},
    })

    import custom_app.services.rag_runner as rag_runner_mod
    old_embed = rag_runner_mod.embed_query
    rag_runner_mod.embed_query = lambda q: np.zeros((1, 4), dtype="float32")
    try:
        events = list(r.chat_stream("q", agent_mode="quick"))
    finally:
        rag_runner_mod.embed_query = old_embed

    # 流式 yield 的 chunk 应该出现
    chunk_contents = [ev.get("content") for ev in events if ev.get("type") == "chunk"]
    assert "chunk1 " in chunk_contents
    assert "chunk2" in chunk_contents
    # Phase 11.1.B 修复 E.3 bug：流式后不再 yield 完整 display_answer
    assert "display answer" not in chunk_contents
    r._generate_stream.assert_called_once_with("prompt")
    r._generate.assert_not_called()


def test_generation_backend_accepts_backend_alias(tmp_path):
    r = RagRunner.__new__(RagRunner)
    r._chat_cfg = {}
    r._apply_ultrarag_generation_env_overrides = lambda: None
    r._gemini_model_id = lambda: "gemini-test"

    kb_dir = tmp_path / "data" / "kb" / "demo"
    (kb_dir / "corpora").mkdir(parents=True)
    (kb_dir / "index").mkdir()
    (kb_dir / "corpora" / "chunks.jsonl").write_text('{"id":"1","title":"t","contents":"c","doc":"d"}\n', encoding="utf-8")
    (kb_dir / "index" / "index.index").write_bytes(b"idx")
    gen = tmp_path / "generation.yaml"
    gen.write_text(
        """
backend: gemini
backend_configs:
  openai:
    model_name: ignored
    base_url: http://unused/v1
sampling_params:
  max_tokens: 128
""",
        encoding="utf-8",
    )
    retriever = tmp_path / "retriever.yaml"
    retriever.write_text("{}", encoding="utf-8")

    import custom_app.services.rag_runner as rag_runner_mod
    old_faiss = rag_runner_mod.faiss
    rag_runner_mod.faiss = MagicMock()
    rag_runner_mod.faiss.read_index.return_value = object()
    try:
        r.__init__(
            kb_id="demo",
            kb_base_dir=str(tmp_path / "data" / "kb"),
            generation_param_path=str(gen),
            retriever_param_path=str(retriever),
        )
        r.init()
    finally:
        rag_runner_mod.faiss = old_faiss

    assert r._chat_cfg["backend"] == "gemini"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 12.1.x：_docs_to_expand 防御性规则
#
# 旧 bug：
#   - _PROCEDURE_INTENT_RE 包含 "battery / 电池 / 换电 / 充电"，使任何含此词的
#     query（如 "Alarm Block Battery Low"）都判为流程意图，再加 _docs_to_expand
#     的 "≥2 步" 路径，让 BatteryChangeSequenceSOP 整本上位、把对的告警 SOP
#     挤出 top-10。
#   - 修：把领域名词从 _PROCEDURE_INTENT_RE 剔除；并在 _docs_to_expand 加
#     "top-3 含他人 SOP 的非 step 段则不扩展" 的防御规则。
# ─────────────────────────────────────────────────────────────────────────────


def _make_expand_rows():
    """构造 8 行：含 Alarm SOP 的 section + BatteryChange 的 11 个 step。"""
    return [
        {"id": "Alarm Block Battery Low SOP_section_1",
         "doc": "Alarm Block Battery Low SOP",
         "title": "Alarm Block Battery Low SOP",
         "contents": "ID 34: Alarm Block Battery Low"},
        {"id": "Alarm Block Battery Low SOP_section_2",
         "doc": "Alarm Block Battery Low SOP",
         "title": "To resolve the issue",
         "contents": "Raise the battery block..."},
    ] + [
        {"id": f"BatteryChangeSequenceSOP_step_{n}",
         "doc": "BatteryChangeSequenceSOP",
         "title": f"BatteryChangeSequenceSOP | STEP {n}",
         "contents": f"STEP {n}: ..."}
        for n in range(1, 12)
    ]


def test_procedure_intent_no_longer_matches_domain_nouns():
    """旧版含 'battery'/'电池'/'充电' 误判流程意图；新版不该匹配。"""
    assert RagRunner._procedure_intent("Alarm Block Battery Low") is False
    assert RagRunner._procedure_intent("电池告警") is False
    assert RagRunner._procedure_intent("battery block is lowered") is False
    assert RagRunner._procedure_intent("AGV battery alarm") is False
    # 真正的流程意图词仍应识别
    assert RagRunner._procedure_intent("AGV 怎么换电池") is True
    assert RagRunner._procedure_intent("battery replacement steps") is True
    assert RagRunner._procedure_intent("更换电池流程") is True
    assert RagRunner._procedure_intent("操作步骤是什么") is True


def test_docs_to_expand_skips_when_other_sop_in_top3():
    """top-3 含别家 SOP 的非 step 段 → 不扩展，避免覆盖告警 SOP。"""
    r = RagRunner.__new__(RagRunner)
    r._rows = _make_expand_rows()
    # 命中：top-3 是 Alarm 的 section（非 step），4-13 是 BatteryChange 多个 step
    # 没修前会因 step 数 ≥ 2 触发 BatteryChangeSequenceSOP 扩展
    hit_ids = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    docs = r._docs_to_expand(hit_ids, "Battery Block Battery Low")
    # 应不扩展：因为 top-3 有别家 SOP（Alarm Block Battery Low SOP）的非 step 段
    assert docs == set()


def test_docs_to_expand_still_works_for_clear_procedure_query():
    """显式问换电步骤时仍应扩展（修过头会 break 这种正确场景）。"""
    r = RagRunner.__new__(RagRunner)
    r._rows = _make_expand_rows()
    # 命中：只有 BatteryChangeSequenceSOP 的 step 块
    hit_ids = [2, 3, 4, 5, 6, 7, 8]  # 全是 step
    docs = r._docs_to_expand(hit_ids, "AGV 怎么换电池")
    assert docs == {"BatteryChangeSequenceSOP"}


def test_docs_to_expand_single_step_at_top1_still_expands_without_competitor():
    """单条 step 在 top-1 且没有其他 SOP 竞争 → 仍可扩展。"""
    r = RagRunner.__new__(RagRunner)
    r._rows = _make_expand_rows()
    # 仅命中 1 个 step 在 #1，没有其他非 step doc
    hit_ids = [2]
    docs = r._docs_to_expand(hit_ids, "Battery Change Step 1")
    # 单步在 top-3 且没有别家 SOP → 应扩展
    assert docs == {"BatteryChangeSequenceSOP"}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 12.1.x: _rewrite_query prompt 中性化（不再注入 AGV 领域偏见）
# ─────────────────────────────────────────────────────────────────────────────


class TestRewriteQueryPromptNeutrality:
    """改写器 prompt 必须中立：所有 KB 的 query 都走它，不能注入 AGV 语境。"""

    def _capture_prompt(self, monkeypatch) -> str:
        """劫持 self._generate 抓 prompt 文本，返回原 question 即可（_rewrite_query
        只用 _generate 返回值做 sanity check）。"""
        r = RagRunner.__new__(RagRunner)
        captured = {}

        def fake_generate(prompt: str) -> str:
            captured["prompt"] = prompt
            return "rewritten"
        r._generate = fake_generate
        r._rewrite_query("如何配置 Planners")
        return captured["prompt"]

    def test_prompt_does_not_mention_agv(self, monkeypatch):
        """不应出现 'AGV' 字眼。"""
        prompt = self._capture_prompt(monkeypatch)
        assert "AGV" not in prompt, \
            "_rewrite_query prompt 不应注入 AGV 领域语境（影响 ifs_docs 等非 AGV KB）"

    def test_prompt_does_not_mention_battery_replacement(self, monkeypatch):
        """不应出现具体领域示例 'battery replacement'。"""
        prompt = self._capture_prompt(monkeypatch)
        assert "battery replacement" not in prompt.lower()

    def test_prompt_keeps_neutral_procedure_guidance(self, monkeypatch):
        """通用的 'steps / procedure / workflow' 指引仍在。"""
        prompt = self._capture_prompt(monkeypatch).lower()
        # 至少包含通用流程意图词的提示
        assert "steps" in prompt or "procedure" in prompt or "workflow" in prompt

    def test_prompt_warns_against_introducing_domain_assumptions(self, monkeypatch):
        """新增反向约束：不要凭空添加领域假设。"""
        prompt = self._capture_prompt(monkeypatch).lower()
        # 应明确告诉模型不要乱加假设
        assert "do not introduce" in prompt or "do not add" in prompt
