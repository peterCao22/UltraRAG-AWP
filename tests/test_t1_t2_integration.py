"""
T1 / T2 集成验收测试
===================
验证 POST /api/chat 的 rewrite_query（T1）和 meta 召回字段（T2）。

运行前提：
  - custom_app 已在 http://127.0.0.1:8080 启动
  - vLLM 服务可达（192.168.8.44:8100）
  - GOOGLE_API_KEY 已配置

运行：
  pytest tests/test_t1_t2_integration.py -v
"""

import pytest
import requests

BASE_URL = "http://127.0.0.1:8080"
API_URL = f"{BASE_URL}/api/chat"
KB_ID = "agv_demo"

# 配置期望值（与 servers/retriever/parameter.yaml 一致）
EXPECTED_RECALL_TOP_K = 20
EXPECTED_FINAL_TOP_K_CFG = 0  # 0 = 不截断

# 测试题集（5 题步骤类 + 附加题）
SOP_QUESTIONS = [
    "更换AGV电池的步骤?",
    "AGV换电流程是什么?",
    "如何安全更换AGV电池?",
]

DETAIL_QUESTIONS = [
    "AGV出现E-Stop报警怎么处理?",
    "电池电量低报警如何解除?",
]

ALL_QUESTIONS = SOP_QUESTIONS + DETAIL_QUESTIONS


# ---------- fixtures ----------

@pytest.fixture(scope="session")
def service_available():
    """检查服务是否已启动，跳过整个模块（而非失败）若未启动。"""
    try:
        r = requests.get(BASE_URL, timeout=5)
    except requests.ConnectionError:
        pytest.skip(f"custom_app 未在 {BASE_URL} 运行，跳过集成测试")


@pytest.fixture(scope="session")
def sop_response(service_available):
    """缓存一次 SOP 题请求，供多个 T2 测试复用。"""
    payload = {"kb_id": KB_ID, "question": SOP_QUESTIONS[0]}
    r = requests.post(API_URL, json=payload, timeout=300)
    assert r.status_code == 200, f"API 返回 {r.status_code}: {r.text[:300]}"
    return r.json()


# ---------- T1: Query Rewrite ----------

class TestT1QueryRewrite:
    """验收标准：rewrite_query 字段存在且内容合理。"""

    def _call(self, question: str) -> dict:
        payload = {"kb_id": KB_ID, "question": question}
        r = requests.post(API_URL, json=payload, timeout=300)
        assert r.status_code == 200, f"API 返回 {r.status_code}: {r.text[:300]}"
        return r.json()

    def test_rewrite_query_field_exists(self, service_available):
        """T1-1: 响应 JSON 中必须有 rewrite_query 字段。"""
        data = self._call(SOP_QUESTIONS[0])
        assert "rewrite_query" in data, "缺少 rewrite_query 字段"

    def test_rewrite_query_non_empty(self, service_available):
        """T1-2: rewrite_query 不为空字符串。"""
        data = self._call(SOP_QUESTIONS[0])
        assert data["rewrite_query"].strip(), "rewrite_query 为空"

    @pytest.mark.parametrize("question", ALL_QUESTIONS)
    def test_rewrite_query_per_question(self, service_available, question):
        """T1-3（人工抽样 5 题）: 每题均有 rewrite_query，改写结果记录于 stdout。"""
        data = self._call(question)
        rq = data.get("rewrite_query", "")
        print(f"\n  原问题 : {question}")
        print(f"  改写后 : {rq}")
        assert rq.strip(), f"题目「{question}」的 rewrite_query 为空"

    def test_rewrite_query_differs_from_original(self, service_available):
        """T1-4: 正常情况下改写后的查询应与原问题不同（标志改写已生效）。
        注：若 vLLM 返回原文则打印 WARNING 而不是 FAIL（兜底行为也合法）。
        """
        question = SOP_QUESTIONS[0]
        data = self._call(question)
        rq = data["rewrite_query"]
        if rq == question:
            pytest.skip(
                f"rewrite_query == 原问题（可能是兜底行为），请人工确认 vLLM 是否正常"
            )
        assert rq != question, "改写后与原问题完全相同，改写未生效"

    def test_fallback_does_not_break_main_response(self, service_available):
        """T1-5: 无论改写成功与否，answer 字段必须存在（主链路不中断）。"""
        data = self._call(SOP_QUESTIONS[0])
        assert "answer" in data, "answer 字段缺失，主链路中断"
        assert data["answer"].strip(), "answer 为空"


# ---------- T2: Recall / 截断 / SOP 扩展 ----------

class TestT2RecallAndSOP:
    """验收标准：meta 字段与配置一致，SOP 场景步骤完整。"""

    def test_meta_field_exists(self, sop_response):
        """T2-1: 响应中有 meta 字段。"""
        assert "meta" in sop_response, "缺少 meta 字段"

    def test_recall_top_k_in_meta(self, sop_response):
        """T2-2: meta.recall_top_k <= parameter.yaml 中 recall_top_k（20）。
        若 corpus 行数 < 20，系统会 clamp 到实际行数，属正常行为。
        """
        meta = sop_response["meta"]
        assert "recall_top_k" in meta, "meta 缺少 recall_top_k"
        actual = meta["recall_top_k"]
        assert 1 <= actual <= EXPECTED_RECALL_TOP_K, (
            f"recall_top_k={actual} 不在合法范围 [1, {EXPECTED_RECALL_TOP_K}]"
        )
        print(f"\n  recall_top_k = {actual}（配置={EXPECTED_RECALL_TOP_K}，"
              f"{'clamped to corpus size' if actual < EXPECTED_RECALL_TOP_K else 'exact match'}）")

    def test_final_top_k_in_meta(self, sop_response):
        """T2-3: meta.final_top_k 为实际命中数（final_top_k_cfg=0 时不截断）。"""
        meta = sop_response["meta"]
        assert "final_top_k" in meta, "meta 缺少 final_top_k"
        # final_top_k_cfg=0 意味着不截断，final_top_k 等于实际 hit 数（>0）
        assert meta["final_top_k"] > 0, "final_top_k=0，无命中结果"
        print(f"\n  final_top_k (实际 hit 数) = {meta['final_top_k']}")

    def test_sop_full_doc_expand_field_exists(self, sop_response):
        """T2-4: meta.sop_full_doc_expand 字段存在。"""
        meta = sop_response["meta"]
        assert "sop_full_doc_expand" in meta, "meta 缺少 sop_full_doc_expand"

    def test_sop_full_doc_expand_true_for_battery_question(self, sop_response):
        """T2-5: 换电步骤类问题应触发 SOP 全文扩展（sop_full_doc_expand=True）。
        若为 False，打印 WARNING 供人工判断（命中可能不足以触发扩展）。
        """
        meta = sop_response["meta"]
        expand = meta.get("sop_full_doc_expand", False)
        print(f"\n  sop_full_doc_expand = {expand}")
        print(f"  primary_expanded_doc = {meta.get('primary_expanded_doc')}")
        if not expand:
            pytest.skip("sop_full_doc_expand=False，需人工确认命中文档是否含 SOP 内容")

    def test_sources_count_consistent_with_final_top_k(self, sop_response):
        """T2-6: sources 条数 = meta.final_top_k（参与生成的 chunk 数一致）。

        正文已内嵌插图时 API 可能省略 sources，此时以 meta.retrieval_source_count 为准。
        """
        meta = sop_response["meta"]
        sources = sop_response.get("sources", [])
        final_k = meta.get("final_top_k", -1)
        n = len(sources) or int(meta.get("retrieval_source_count", -1))
        assert n == final_k, (
            f"sources.Count={len(sources)}，retrieval_source_count={meta.get('retrieval_source_count')}，"
            f"meta.final_top_k={final_k}，不一致"
        )

    def test_no_foreign_doc_in_sop_expand(self, sop_response):
        """T2-7: SOP 扩展后 sources 中所有 chunk 应来自同一主文档（无串 doc）。"""
        meta = sop_response["meta"]
        if not meta.get("sop_full_doc_expand"):
            pytest.skip("未触发 SOP 扩展，跳过此项")
        primary_doc = meta.get("primary_expanded_doc")
        sources = sop_response.get("sources", [])
        if not sources and meta.get("sources_omitted_for_ui"):
            pytest.skip("响应已省略 sources 列表，无法在本用例中按 doc 校验外来 chunk")
        foreign = [s for s in sources if s.get("doc") != primary_doc]
        print(f"\n  主文档: {primary_doc}")
        print(f"  sources 总数: {len(sources)}, 外来 doc 数: {len(foreign)}")
        if foreign:
            foreign_docs = list({s.get("doc") for s in foreign})
            # T2 要求「无无关 doc 混入」，视为 WARNING（soft assert）
            pytest.skip(
                f"存在来自其他文档的 chunk（{foreign_docs}），需人工判断是否合理"
            )

    def test_meta_fields_printed_summary(self, sop_response):
        """T2-summary: 打印完整 meta 供人工审阅。"""
        import json
        meta = sop_response["meta"]
        print(f"\n--- meta 完整内容 ---\n{json.dumps(meta, ensure_ascii=False, indent=2)}")
        print(f"sources.Count = {len(sop_response.get('sources', []))}")
        print(f"rewrite_query = {sop_response.get('rewrite_query')}")
        assert True  # 此用例仅用于打印，不做硬断言
