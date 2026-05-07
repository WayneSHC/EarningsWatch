"""
Unit tests for src/core/ragas_eval.py.

Don't actually call ragas (heavy + costs money). Only tests:
  - is_available() returns False when ragas not installed (or True if installed)
  - aggregate() correctly averages per-query metric dicts
  - state_to_contexts() flattens AgentState.retrieved → list[str]
  - evaluate_query() gracefully returns {} when ragas unavailable
  - evaluate_query() gracefully returns {} when contexts empty / no API key
"""
from src.core import ragas_eval


# ──────────────────────────────────────────────────────────────────────────
# is_available
# ──────────────────────────────────────────────────────────────────────────

class TestIsAvailable:
    def test_returns_bool(self):
        # Either True (ragas installed) or False — both valid; just ensure no crash
        assert isinstance(ragas_eval.is_available(), bool)


# ──────────────────────────────────────────────────────────────────────────
# aggregate
# ──────────────────────────────────────────────────────────────────────────

class TestAggregate:
    def test_empty_input(self):
        assert ragas_eval.aggregate([]) == {}

    def test_single_query(self):
        out = ragas_eval.aggregate([{"faithfulness": 0.8, "answer_relevancy": 0.9}])
        assert out == {"faithfulness": 0.8, "answer_relevancy": 0.9}

    def test_multiple_queries_averaged(self):
        out = ragas_eval.aggregate([
            {"faithfulness": 0.8, "answer_relevancy": 0.9},
            {"faithfulness": 0.6, "answer_relevancy": 0.7},
        ])
        assert out["faithfulness"] == 0.7
        assert out["answer_relevancy"] == 0.8

    def test_handles_partial_metrics(self):
        # Some queries have context_recall, others don't
        out = ragas_eval.aggregate([
            {"faithfulness": 0.8, "context_recall": 0.9},
            {"faithfulness": 0.6},  # missing context_recall
        ])
        assert out["faithfulness"] == 0.7
        assert out["context_recall"] == 0.9  # only one sample

    def test_rounds_to_4_decimals(self):
        out = ragas_eval.aggregate([
            {"faithfulness": 0.123456789},
            {"faithfulness": 0.987654321},
        ])
        # (0.123456789 + 0.987654321) / 2 = 0.555555555
        assert out["faithfulness"] == 0.5556


# ──────────────────────────────────────────────────────────────────────────
# state_to_contexts
# ──────────────────────────────────────────────────────────────────────────

class TestStateToContexts:
    def test_empty_state(self):
        assert ragas_eval.state_to_contexts({}) == []

    def test_no_retrieved(self):
        assert ragas_eval.state_to_contexts({"retrieved": None}) == []

    def test_flattens_quarters(self):
        state = {
            "retrieved": {
                "2024Q1": [
                    {"content": "AI demand strong", "quarter": "2024Q1"},
                    {"content": "Margin improving", "quarter": "2024Q1"},
                ],
                "2024Q2": [
                    {"content": "CoWoS supply tight", "quarter": "2024Q2"},
                ],
            }
        }
        out = ragas_eval.state_to_contexts(state)
        assert len(out) == 3
        assert "AI demand strong" in out
        assert "CoWoS supply tight" in out

    def test_skips_chunks_without_content(self):
        state = {
            "retrieved": {
                "2024Q1": [
                    {"content": "valid", "quarter": "2024Q1"},
                    {"quarter": "2024Q1"},  # no content key
                    {"content": "", "quarter": "2024Q1"},  # empty
                ]
            }
        }
        out = ragas_eval.state_to_contexts(state)
        assert out == ["valid"]

    def test_handles_non_dict_chunks(self):
        # Robust against malformed data
        state = {"retrieved": {"2024Q1": ["string", None, 123]}}
        out = ragas_eval.state_to_contexts(state)
        assert out == []


# ──────────────────────────────────────────────────────────────────────────
# evaluate_query — graceful degradation paths
# ──────────────────────────────────────────────────────────────────────────

class TestEvaluateQueryGuards:
    def test_empty_contexts_returns_empty(self, monkeypatch):
        # Force ragas as available so we hit the contexts-empty guard, not import guard
        monkeypatch.setattr(ragas_eval, "is_available", lambda: True)
        out = ragas_eval.evaluate_query("Q?", "A.", contexts=[])
        assert out == {}

    def test_missing_openai_key_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ragas_eval, "is_available", lambda: True)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"])
        assert out == {}

    def test_unavailable_ragas_returns_empty(self, monkeypatch):
        # Pretend ragas not installed
        monkeypatch.setattr(ragas_eval, "is_available", lambda: False)
        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"])
        assert out == {}
