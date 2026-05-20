"""
Unit tests for src/agent/nodes.py.

Coverage:
  - self_reflect: coverage-driven retry sub_queries (P1-5)
  - self_reflect: cost guard sets cost_guard_triggered when budget exceeded (P1-6)
  - should_continue: respects cost_guard_triggered flag
  - parallel_retrieval: target_quarter overrides quarters_filter
  - intent_classifier: free-form auto-detected topic when UI didn't pick one (P2)
"""
import pytest

from src.agent import nodes
from src.core import telemetry


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset()
    yield
    telemetry.reset()


# ──────────────────────────────────────────────────────────────────────────
# intent_classifier — auto-detect topic when UI omits it (P2)
# ──────────────────────────────────────────────────────────────────────────

class TestIntentClassifierAutoTopic:
    def _state(self, query="台積電 2024 CoWoS 產能瓶頸怎麼說？", company="", topic=""):
        return {
            "query": query,
            "company": company,
            "topic": topic,
            "quarters": [],
        }

    def test_empty_topic_triggers_llm_extract(self, monkeypatch):
        """UI 沒選主題 → _llm 被呼叫，回傳的 topic 被採用。"""
        calls = {"n": 0}
        def fake_llm(prompt, max_tokens=200):
            calls["n"] += 1
            return '{"company": "台積電", "topic": "CoWoS 產能", "quarters": []}'
        monkeypatch.setattr(nodes, "_llm", fake_llm)

        out = nodes.intent_classifier(self._state())
        assert calls["n"] == 1, "未選 topic 時 _llm 必須被呼叫"
        assert out["topic"] == "CoWoS 產能"
        assert out["company"] == "台積電"

    def test_freeform_topic_passes_through(self, monkeypatch):
        """LLM 回傳非白名單的 free-form topic 也應原樣保留（P2 解除限定）。"""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"company": "台積電", "topic": "美國亞利桑那建廠進度", "quarters": []}'
        )
        out = nodes.intent_classifier(self._state(topic=""))
        assert out["topic"] == "美國亞利桑那建廠進度"

    def test_ui_topic_skips_llm(self, monkeypatch):
        """UI 已指定 topic → _llm 不應被呼叫（既有行為，避免回歸）。"""
        called = {"n": 0}
        def fake_llm(*a, **kw):
            called["n"] += 1
            return '{"company": "x", "topic": "y", "quarters": []}'
        monkeypatch.setattr(nodes, "_llm", fake_llm)

        out = nodes.intent_classifier(self._state(company="台積電", topic="毛利率"))
        assert called["n"] == 0, "UI 已給 topic 時 _llm 不應被呼叫"
        assert out["topic"] == "毛利率"


# ──────────────────────────────────────────────────────────────────────────
# self_reflect — coverage-driven retry queries (P1-5)
# ──────────────────────────────────────────────────────────────────────────

class TestSelfReflectCoverageFeedback:
    def _state(self, retrieved=None, contradictions=None, iteration=0):
        return {
            "query": "台積電 AI 各季發言",
            "company": "台積電",
            "topic": "AI需求",
            "iteration": iteration,
            "retrieved": retrieved or {},
            "contradictions": contradictions or [],
            "promises": [],
            "sub_queries": [],
            "cost_baseline_usd": 0.0,
        }

    def test_weak_quarter_generates_target_query(self, monkeypatch):
        """A quarter with chunk_count<2 must produce a coverage_fill sub_query."""
        # Force LLM judge to say retry needed but emit no gaps
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.5, "issues": [], "gaps": [], "should_retry": true}'
        )

        state = self._state(retrieved={
            # Q1 strong, Q2 weak (single low-score chunk)
            "2024Q1": [
                {"score": 0.85, "payload": {"content": "AI demand strong", "source_page": 5}},
                {"score": 0.78, "payload": {"content": "good", "source_page": 6}},
            ],
            "2024Q2": [
                {"score": 0.30, "payload": {"content": "boilerplate", "source_page": 7}},
            ],
        })

        out = nodes.self_reflect(state)
        sqs = out["sub_queries"]

        # Must have a target_quarter for 2024Q2
        weak_sqs = [s for s in sqs if s.get("tool_hint") == "coverage_fill"]
        assert any(s.get("target_quarter") == "2024Q2" for s in weak_sqs), \
            f"Expected coverage_fill targeting 2024Q2, got {sqs}"

    def test_low_score_quarter_flagged_weak(self, monkeypatch):
        """A quarter with max_score<0.4 must be flagged as weak."""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.5, "should_retry": true}'
        )

        state = self._state(retrieved={
            "2024Q1": [
                {"score": 0.85, "payload": {"content": "x", "source_page": 1}},
                {"score": 0.78, "payload": {"content": "y", "source_page": 2}},
            ],
            "2024Q2": [
                # 3 chunks but all low-scoring → max_score<0.4 → weak
                {"score": 0.35, "payload": {"content": "a", "source_page": 1}},
                {"score": 0.30, "payload": {"content": "b", "source_page": 2}},
                {"score": 0.25, "payload": {"content": "c", "source_page": 3}},
            ],
        })

        out = nodes.self_reflect(state)
        sqs = out["sub_queries"]
        targets = [s.get("target_quarter") for s in sqs if s.get("tool_hint") == "coverage_fill"]
        assert "2024Q2" in targets

    def test_strong_quarters_no_coverage_query(self, monkeypatch):
        """All quarters strong → no coverage_fill queries."""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.5, "should_retry": true}'
        )

        state = self._state(retrieved={
            "2024Q1": [
                {"score": 0.85, "payload": {"content": "x", "source_page": 1}},
                {"score": 0.78, "payload": {"content": "y", "source_page": 2}},
            ],
            "2024Q2": [
                {"score": 0.82, "payload": {"content": "a", "source_page": 1}},
                {"score": 0.71, "payload": {"content": "b", "source_page": 2}},
            ],
        })

        out = nodes.self_reflect(state)
        coverage_fills = [s for s in out["sub_queries"]
                          if s.get("tool_hint") == "coverage_fill"]
        assert coverage_fills == [], "All-strong should not trigger coverage_fill"

    def test_caps_weak_quarters_at_three(self, monkeypatch):
        """Even with 5+ weak quarters, only top 3 get coverage_fill queries."""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.5, "should_retry": true}'
        )

        # 5 weak quarters
        retrieved = {
            f"2024Q{i}": [{"score": 0.20, "payload": {"content": "x", "source_page": 1}}]
            for i in range(1, 6)
        }
        state = self._state(retrieved=retrieved)
        out = nodes.self_reflect(state)
        coverage_fills = [s for s in out["sub_queries"]
                          if s.get("tool_hint") == "coverage_fill"]
        assert len(coverage_fills) == 3


# ──────────────────────────────────────────────────────────────────────────
# self_reflect — cost guard (P1-6)
# ──────────────────────────────────────────────────────────────────────────

class TestSelfReflectCostGuard:
    def _state(self, **overrides):
        base = {
            "query": "q", "company": "台積電", "topic": "AI需求",
            "iteration": 1,
            "retrieved": {
                "2024Q1": [{"score": 0.85, "payload": {"content": "ok", "source_page": 1}}],
            },
            "contradictions": [],
            "promises": [],
            "sub_queries": [],
            "cost_baseline_usd": 0.0,
        }
        base.update(overrides)
        return base

    def test_within_budget_no_guard(self, monkeypatch):
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.5, "should_retry": true}'
        )
        # Spend $0.10 (well under default $0.50 budget)
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5-mini",
            prompt_tokens=10, completion_tokens=10, cost_usd=0.10,
        ))

        out = nodes.self_reflect(self._state())
        assert out["cost_guard_triggered"] is False

    def test_exceeds_budget_triggers_guard(self, monkeypatch):
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.5, "should_retry": true}'
        )
        # Spend $0.60 (exceeds default $0.50 budget)
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5",
            prompt_tokens=100, completion_tokens=200, cost_usd=0.60,
        ))

        out = nodes.self_reflect(self._state())
        assert out["cost_guard_triggered"] is True

    def test_baseline_isolates_query_cost(self, monkeypatch):
        """Pre-existing telemetry from prior queries must not trip the guard."""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.5, "should_retry": true}'
        )
        # $0.45 already spent BEFORE this query (sibling/prior queries)
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5", cost_usd=0.45,
        ))
        # Capture baseline as if intent_classifier just ran
        baseline = telemetry.summary()["estimated_cost_usd"]
        # This query adds only $0.10 → total $0.55 but query cost = $0.10 < budget
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5", cost_usd=0.10,
        ))

        out = nodes.self_reflect(self._state(cost_baseline_usd=baseline))
        assert out["cost_guard_triggered"] is False

    def test_no_retry_no_guard_check(self, monkeypatch):
        """If do_retry is false (high score), cost_guard is False regardless."""
        # High score + healthy data so hard floor doesn't drag score down
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.95, "should_retry": false}'
        )
        # Even if budget would be exceeded
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5", cost_usd=10.0,
        ))

        # Strong data — 2+ quarters, 3+ chunks → no hard floor penalty
        rich_state = {
            "query": "q", "company": "台積電", "topic": "AI需求",
            "iteration": 1,
            "retrieved": {
                "2024Q1": [
                    {"score": 0.85, "payload": {"content": "x", "source_page": 1}},
                    {"score": 0.78, "payload": {"content": "y", "source_page": 2}},
                ],
                "2024Q2": [
                    {"score": 0.82, "payload": {"content": "a", "source_page": 1}},
                    {"score": 0.71, "payload": {"content": "b", "source_page": 2}},
                ],
            },
            "contradictions": [],
            "promises": [],
            "sub_queries": [],
            "cost_baseline_usd": 0.0,
        }
        out = nodes.self_reflect(rich_state)
        # do_retry should be False (score=0.95 high), so cost_guard untouched
        assert out["cost_guard_triggered"] is False


# ──────────────────────────────────────────────────────────────────────────
# should_continue — purely reads cost_guard_triggered
# ──────────────────────────────────────────────────────────────────────────

class TestShouldContinue:
    def test_high_confidence_ends(self):
        state = {"confidence": 0.9, "iteration": 1}
        assert nodes.should_continue(state) == "end"

    def test_low_confidence_retries(self):
        state = {"confidence": 0.5, "iteration": 1}
        assert nodes.should_continue(state) == "retry"

    def test_max_iteration_ends(self):
        # Even with low confidence, iteration cap stops retry
        state = {"confidence": 0.3, "iteration": 3}
        assert nodes.should_continue(state) == "end"

    def test_cost_guard_overrides_retry(self):
        # Low confidence would normally retry, but cost_guard forces end
        state = {"confidence": 0.3, "iteration": 1, "cost_guard_triggered": True}
        assert nodes.should_continue(state) == "end"


# ──────────────────────────────────────────────────────────────────────────
# Regression: should_retry_llm honored end-to-end (spec audit fix)
# ──────────────────────────────────────────────────────────────────────────

class TestSelfReflectRetryHintHonored:
    """Spec self-reflection-loop says retry triggers when
       (confidence < 0.75 OR should_retry_llm) AND iteration < 3.

    Previously self_reflect honored should_retry_llm internally (rebuilt
    sub_queries, bumped iteration) but should_continue only saw the
    numeric confidence — so when LLM judged score >= 0.75 with
    should_retry=true, retry prep was done but the graph routed to report.

    Fix: when do_retry is true purely because should_retry_llm, clamp the
    `confidence` reported in state to 0.74 so should_continue routes back.
    """

    def _strong_state(self, iteration=0):
        return {
            "query": "q", "company": "X", "topic": "AI",
            "iteration": iteration,
            "retrieved": {
                "2024Q1": [
                    {"score": 0.85, "payload": {"content": "x", "source_page": 1}},
                    {"score": 0.78, "payload": {"content": "y", "source_page": 2}},
                ],
                "2024Q2": [
                    {"score": 0.82, "payload": {"content": "a", "source_page": 1}},
                    {"score": 0.71, "payload": {"content": "b", "source_page": 2}},
                ],
            },
            "contradictions": [],
            "promises": [],
            "sub_queries": [],
            "cost_baseline_usd": 0.0,
        }

    def test_high_score_with_retry_hint_routes_to_retry(self, monkeypatch):
        """LLM gives high score but explicit should_retry=true → graph must retry."""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.95, "should_retry": true}',
        )

        out = nodes.self_reflect(self._strong_state())

        # Clamped confidence so should_continue picks retry
        assert out["confidence"] < 0.75
        # And should_continue agrees
        next_step = nodes.should_continue({**out, "cost_guard_triggered": False})
        assert next_step == "retry"

    def test_high_score_without_retry_hint_routes_to_end(self, monkeypatch):
        """High score with should_retry=false should NOT clamp; routes to end."""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.95, "should_retry": false}',
        )

        out = nodes.self_reflect(self._strong_state())

        # Unchanged confidence
        assert out["confidence"] >= 0.75
        next_step = nodes.should_continue(out)
        assert next_step == "end"

    def test_retry_hint_ignored_when_cost_guard_trips(self, monkeypatch):
        """If cost guard triggers, do not clamp — cost_guard_triggered drives end."""
        from src.core import telemetry as _tele
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.95, "should_retry": true}',
        )
        # Spend over default $0.50 budget
        _tele.record(_tele.LLMCall(
            backend="openai", model="gpt-5", cost_usd=0.60,
        ))

        out = nodes.self_reflect(self._strong_state(iteration=1))

        # cost guard wins; confidence is NOT clamped
        assert out["cost_guard_triggered"] is True
        assert out["confidence"] >= 0.75

    def test_retry_hint_ignored_at_iteration_cap(self, monkeypatch):
        """At iteration=3, do_retry is False → no clamp regardless of LLM hint."""
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: '{"score": 0.95, "should_retry": true}',
        )

        out = nodes.self_reflect(self._strong_state(iteration=3))

        # Iteration cap stops retry path → confidence NOT clamped
        assert out["confidence"] >= 0.75
        next_step = nodes.should_continue(out)
        assert next_step == "end"


# ══════════════════════════════════════════════════════════════════════════
# query_decomposer — spec: query-decomposition
# ══════════════════════════════════════════════════════════════════════════

class TestQueryDecomposer:
    def _state(self, query="台積電 AI 需求各季變化", company="台積電",
               topic="AI需求", quarters=None):
        return {
            "query": query, "company": company, "topic": topic,
            "quarters": quarters or [],
        }

    def test_llm_subqueries_used(self, monkeypatch):
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: (
                '{"sub_queries": ['
                '{"id": "a", "query": "台積電 AI 跨季", "purpose": "比對", "tool": "bigquery"},'
                '{"id": "b", "query": "台積電 AI 新聞", "purpose": "背景", "tool": "tavily"}'
                ']}'
            ),
        )
        out = nodes.query_decomposer(self._state())
        sqs = out["sub_queries"]
        assert len(sqs) == 2
        assert all("query" in s and "tool" in s for s in sqs)

    def test_non_dict_candidate_skipped(self, monkeypatch):
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: (
                '{"sub_queries": ['
                '"a bare string",'
                '{"id": "ok", "query": "台積電 AI 跨季比對", "purpose": "p", "tool": "bigquery"}'
                ']}'
            ),
        )
        out = nodes.query_decomposer(self._state())
        # Only the dict candidate survives
        assert len(out["sub_queries"]) == 1
        assert out["sub_queries"][0]["id"] == "ok"

    def test_bad_tool_skipped(self, monkeypatch):
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: (
                '{"sub_queries": ['
                '{"id": "x", "query": "q1 enough length", "purpose": "p", "tool": "yfinance"},'
                '{"id": "y", "query": "q2 enough length", "purpose": "p", "tool": "bigquery"}'
                ']}'
            ),
        )
        out = nodes.query_decomposer(self._state())
        tools = [s["tool"] for s in out["sub_queries"]]
        assert "yfinance" not in tools
        assert "bigquery" in tools

    def test_long_query_truncated_to_120(self, monkeypatch):
        long_q = "台" * 300
        monkeypatch.setattr(
            nodes, "_llm",
            lambda *a, **kw: (
                '{"sub_queries": [{"id": "x", "query": "' + long_q
                + '", "purpose": "p", "tool": "bigquery"}]}'
            ),
        )
        out = nodes.query_decomposer(self._state())
        assert len(out["sub_queries"][0]["query"]) <= 120

    def test_llm_failure_falls_back_to_template(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("LLM down")
        monkeypatch.setattr(nodes, "_llm", boom)

        out = nodes.query_decomposer(self._state())
        # 3-entry fallback template
        assert len(out["sub_queries"]) == 3
        assert any("降級" in line for line in out["steps_log"])

    def test_fallback_template_covers_three_angles(self):
        fb = nodes._fallback_sub_queries("台積電", "AI需求")
        assert len(fb) == 3
        tools = [s["tool"] for s in fb]
        assert tools.count("tavily") == 1
        assert tools.count("bigquery") == 2
        assert any(s.get("section_filter") == "guidance" for s in fb)

    def test_quarter_scope_injected_into_prompt(self, monkeypatch):
        captured = {}
        def fake_llm(prompt, *a, **kw):
            captured["prompt"] = prompt
            return '{"sub_queries": []}'  # forces fallback, fine
        monkeypatch.setattr(nodes, "_llm", fake_llm)

        nodes.query_decomposer(self._state(quarters=["2024Q1", "2024Q3"]))
        assert "2024Q1" in captured["prompt"]
        assert "2024Q3" in captured["prompt"]


# ══════════════════════════════════════════════════════════════════════════
# parallel_retrieval — spec: parallel-retrieval
# ══════════════════════════════════════════════════════════════════════════

def _chunk(cid, quarter, content="x"):
    return {"id": cid, "score": 0.8,
            "payload": {"quarter": quarter, "content": content}}


class TestParallelRetrieval:
    def _state(self, sub_queries, quarters=None, iteration=0,
               tool_plan=None, retrieved=None):
        return {
            "query": "q", "company": "台積電",
            "quarters": quarters or [],
            "tool_plan": tool_plan or ["bigquery"],
            "sub_queries": sub_queries,
            "iteration": iteration,
            "retrieved": retrieved or {},
            "news_context": [],
            "stock_data": {},
        }

    def test_adaptive_top_k_guidance_is_4(self, monkeypatch):
        captured = []
        def fake_retrieve(*, query, company, quarters, section, top_k):
            captured.append(top_k)
            return []
        monkeypatch.setattr(nodes, "retrieve", fake_retrieve)

        sq = {"id": "g", "query": "q", "tool": "bigquery",
              "section_filter": "guidance"}
        nodes.parallel_retrieval(self._state([sq], quarters=["2024Q1"]))
        assert captured == [4]

    def test_adaptive_top_k_coverage_fill_is_8(self, monkeypatch):
        captured = []
        def fake_retrieve(*, query, company, quarters, section, top_k):
            captured.append(top_k)
            return []
        monkeypatch.setattr(nodes, "retrieve", fake_retrieve)

        sq = {"id": "w", "query": "q", "tool": "bigquery",
              "section_filter": "guidance", "tool_hint": "coverage_fill"}
        nodes.parallel_retrieval(self._state([sq], quarters=["2024Q1"]))
        assert captured == [8]

    def test_target_quarter_overrides_quarters_filter(self, monkeypatch):
        captured = {}
        def fake_retrieve(*, query, company, quarters, section, top_k):
            captured["quarters"] = quarters
            return []
        monkeypatch.setattr(nodes, "retrieve", fake_retrieve)

        sq = {"id": "w", "query": "q", "tool": "bigquery",
              "tool_hint": "coverage_fill", "target_quarter": "2024Q2"}
        nodes.parallel_retrieval(
            self._state([sq], quarters=["2024Q1", "2024Q2", "2024Q3"])
        )
        assert captured["quarters"] == ["2024Q2"]

    def test_tool_failure_isolated(self, monkeypatch):
        def fake_retrieve(*, query, company, quarters, section, top_k):
            return [_chunk("c1", "2024Q1")]
        def boom_news(*a, **kw):
            raise RuntimeError("tavily down")
        monkeypatch.setattr(nodes, "retrieve", fake_retrieve)
        monkeypatch.setattr(nodes, "search_news", boom_news)

        sq = {"id": "a", "query": "q", "tool": "bigquery"}
        out = nodes.parallel_retrieval(
            self._state([sq], quarters=["2024Q1"],
                        tool_plan=["bigquery", "tavily"])
        )
        # BigQuery result survives despite Tavily failure
        assert "2024Q1" in out["retrieved"]
        assert any("查詢失敗" in line for line in out["steps_log"])

    def test_retry_preserves_prior_retrieved(self, monkeypatch):
        def fake_retrieve(*, query, company, quarters, section, top_k):
            return [_chunk("new", "2024Q2")]
        monkeypatch.setattr(nodes, "retrieve", fake_retrieve)

        prior = {"2024Q1": [_chunk("old", "2024Q1")]}
        sq = {"id": "a", "query": "q", "tool": "bigquery"}
        out = nodes.parallel_retrieval(
            self._state([sq], quarters=["2024Q1", "2024Q2"],
                        iteration=1, retrieved=prior)
        )
        # Prior Q1 chunk retained, new Q2 chunk added
        assert "2024Q1" in out["retrieved"]
        assert out["retrieved"]["2024Q1"][0]["id"] == "old"
        assert "2024Q2" in out["retrieved"]


# ══════════════════════════════════════════════════════════════════════════
# report_generator — spec: report-generation
# ══════════════════════════════════════════════════════════════════════════

class TestReportGenerator:
    def _state(self, **overrides):
        base = {
            "company": "台積電", "topic": "AI需求", "query": "AI 需求如何",
            "contradictions": [], "promises": [], "retrieved": {},
            "news_context": [], "stock_data": {}, "confidence": 0.8,
            "abstain": False, "cost_guard_triggered": False,
            "reflection_issues": [], "reflection_gaps": [],
        }
        base.update(overrides)
        return base

    def test_abstain_outputs_insufficient_data_report(self):
        out = nodes.report_generator(self._state(
            abstain=True, confidence=0.3,
            reflection_issues=["只找到 1 季資料"],
        ))
        assert "資料不足" in out["final_report"]
        assert "只找到 1 季資料" in out["final_report"]
        assert "跨季發言比對" not in out["final_report"]

    def test_cost_guard_notice_present(self, monkeypatch):
        monkeypatch.setattr(nodes, "llm_chat", lambda *a, **kw: "回答內容")
        out = nodes.report_generator(self._state(
            cost_guard_triggered=True,
            retrieved={"2024Q1": [{"payload": {"content": "AI 需求強勁"}}]},
        ))
        assert "預算保護觸發" in out["final_report"]

    def test_zero_chunks_triggers_off_topic(self, monkeypatch):
        monkeypatch.setattr(nodes, "search_news", lambda *a, **kw: [])
        # retrieved empty → total_chunks == 0 → off-topic
        out = nodes.report_generator(self._state(retrieved={}))
        assert "未在法說會逐字稿中找到相關內容" in out["final_report"]
        assert "## 一、跨季發言比對" not in out["final_report"]

    def test_off_topic_phrase_triggers_news_mode(self, monkeypatch):
        monkeypatch.setattr(nodes, "llm_chat",
                            lambda *a, **kw: "資料中並未提及此主題")
        monkeypatch.setattr(nodes, "search_news", lambda *a, **kw: [])
        out = nodes.report_generator(self._state(
            retrieved={"2024Q1": [{"payload": {"content": "some content"}}]},
        ))
        assert "未在法說會逐字稿中找到相關內容" in out["final_report"]

    def test_llm_xss_escaped_in_report(self, monkeypatch):
        monkeypatch.setattr(nodes, "llm_chat", lambda *a, **kw: "正常回答內容")
        contradiction = {
            "quarter_a": "2024Q1", "quarter_b": "2024Q2",
            "analysis": {
                "stance_change": "更樂觀",
                "change_detail": "<script>alert(1)</script>",
                "has_contradiction": False,
            },
            "sources_a": [], "sources_b": [],
        }
        out = nodes.report_generator(self._state(
            contradictions=[contradiction],
            retrieved={"2024Q1": [{"payload": {"content": "AI 需求"}}],
                       "2024Q2": [{"payload": {"content": "AI 持續"}}]},
        ))
        assert "&lt;script&gt;" in out["final_report"]
        assert "<script>" not in out["final_report"]

    def test_returns_final_report_and_steps_log(self, monkeypatch):
        monkeypatch.setattr(nodes, "search_news", lambda *a, **kw: [])
        out = nodes.report_generator(self._state(retrieved={}))
        assert isinstance(out["final_report"], str)
        assert isinstance(out["steps_log"], list)


# ══════════════════════════════════════════════════════════════════════════
# _clean_news_snippet — spec: report-generation
# ══════════════════════════════════════════════════════════════════════════

class TestCleanNewsSnippet:
    def test_spa_boilerplate_emptied(self):
        assert nodes._clean_news_snippet("請啟用 JavaScript 以繼續瀏覽") == ""

    def test_markdown_chars_removed(self):
        out = nodes._clean_news_snippet("## 標題 **粗體** [連結](url)")
        for ch in "#*[]":
            assert ch not in out

    def test_truncated_to_max_len(self):
        out = nodes._clean_news_snippet("乾淨內容" * 100, max_len=180)
        assert len(out) <= 180

    def test_empty_input(self):
        assert nodes._clean_news_snippet("") == ""
