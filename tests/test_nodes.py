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
