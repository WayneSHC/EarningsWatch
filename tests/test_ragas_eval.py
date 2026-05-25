"""
Unit tests for src/core/ragas_eval.py.

Don't actually call ragas (heavy + costs money). Only tests:
  - is_available() returns False when ragas not installed (or True if installed)
  - aggregate() correctly averages per-query metric dicts
  - state_to_contexts() flattens AgentState.retrieved → list[str]
  - evaluate_query() gracefully returns {} when ragas unavailable
  - evaluate_query() gracefully returns {} when contexts empty / no API key
  - evaluate_query() full evaluation path (ragas mocked via sys.modules)
"""
import sys
import types

import pandas as pd
import pytest

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


# ──────────────────────────────────────────────────────────────────────────
# is_available — True path (mock modules in sys.modules)
# ──────────────────────────────────────────────────────────────────────────

class TestIsAvailableTrue:
    def test_returns_true_when_both_packages_present(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ragas", types.ModuleType("ragas"))
        monkeypatch.setitem(sys.modules, "langchain_openai", types.ModuleType("langchain_openai"))
        assert ragas_eval.is_available() is True


# ──────────────────────────────────────────────────────────────────────────
# evaluate_query — full evaluation path (ragas mocked via sys.modules)
# ──────────────────────────────────────────────────────────────────────────

def _make_metric_modules(*, include_context_recall: bool = True) -> tuple:
    """Return (fake_ragas, fake_ragas_metrics, fake_datasets) module stubs."""
    faithfulness_obj = object()
    answer_relevancy_obj = object()
    context_precision_obj = object()
    context_recall_obj = object()

    fake_ragas_metrics = types.ModuleType("ragas.metrics")
    fake_ragas_metrics.faithfulness = faithfulness_obj
    fake_ragas_metrics.answer_relevancy = answer_relevancy_obj
    fake_ragas_metrics.context_precision = context_precision_obj
    if include_context_recall:
        fake_ragas_metrics.context_recall = context_recall_obj

    fake_ragas = types.ModuleType("ragas")

    class _FakeDataset:
        @staticmethod
        def from_dict(data):
            return _FakeDataset()

    fake_datasets = types.ModuleType("datasets")
    fake_datasets.Dataset = _FakeDataset

    return fake_ragas, fake_ragas_metrics, fake_datasets


def _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets):
    monkeypatch.setitem(sys.modules, "ragas", fake_ragas)
    monkeypatch.setitem(sys.modules, "ragas.metrics", fake_ragas_metrics)
    monkeypatch.setitem(sys.modules, "datasets", fake_datasets)
    monkeypatch.setitem(sys.modules, "langchain_openai", types.ModuleType("langchain_openai"))
    monkeypatch.setattr(ragas_eval, "is_available", lambda: True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


class TestEvaluateQueryFull:
    def _standard_df(self):
        return pd.DataFrame({
            "faithfulness": [0.9],
            "answer_relevancy": [0.8],
            "context_precision": [0.7],
            "question": ["Q?"],
            "answer": ["A."],
            "contexts": [["ctx"]],
        })

    def _result_obj(self, df):
        class _FakeResult:
            def to_pandas(self_):
                return df
        return _FakeResult()

    def test_returns_numeric_metric_scores(self, monkeypatch):
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules()
        fake_ragas.evaluate = lambda dataset, metrics: self._result_obj(self._standard_df())
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"])
        assert out["faithfulness"] == pytest.approx(0.9)
        assert out["answer_relevancy"] == pytest.approx(0.8)
        assert out["context_precision"] == pytest.approx(0.7)

    def test_non_metric_columns_filtered_out(self, monkeypatch):
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules()
        fake_ragas.evaluate = lambda dataset, metrics: self._result_obj(self._standard_df())
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"])
        assert "question" not in out
        assert "answer" not in out
        assert "contexts" not in out

    def test_empty_df_returns_empty_dict(self, monkeypatch):
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules()
        fake_ragas.evaluate = lambda dataset, metrics: self._result_obj(pd.DataFrame())
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"])
        assert out == {}

    def test_exception_in_evaluate_returns_empty_and_prints(self, monkeypatch, capsys):
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules()
        def _boom(dataset, metrics):
            raise RuntimeError("API failure")
        fake_ragas.evaluate = _boom
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"])
        assert out == {}
        assert "[RAGAS]" in capsys.readouterr().out

    def test_unknown_metrics_arg_returns_empty(self, monkeypatch):
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules()
        fake_ragas.evaluate = lambda dataset, metrics: self._result_obj(self._standard_df())
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"],
                                         metrics=["nonexistent_metric"])
        assert out == {}

    def test_ground_truth_adds_context_recall_to_row(self, monkeypatch):
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules(
            include_context_recall=True
        )
        df = pd.DataFrame({
            "faithfulness": [0.85],
            "context_recall": [0.75],
            "question": ["Q?"],
        })
        fake_ragas.evaluate = lambda dataset, metrics: self._result_obj(df)
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"],
                                         ground_truth="expected answer")
        assert "faithfulness" in out
        assert "context_recall" in out
        assert out["context_recall"] == pytest.approx(0.75)

    def test_context_recall_import_error_silenced(self, monkeypatch):
        # context_recall attr absent → ImportError is caught silently
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules(
            include_context_recall=False
        )
        fake_ragas.evaluate = lambda dataset, metrics: self._result_obj(self._standard_df())
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"],
                                         ground_truth="GT")
        assert isinstance(out, dict)

    def test_non_numeric_cell_skipped_without_error(self, monkeypatch):
        fake_ragas, fake_ragas_metrics, fake_datasets = _make_metric_modules()
        df = pd.DataFrame({
            "faithfulness": [0.9],
            "answer_relevancy": ["not-a-number"],
        })
        fake_ragas.evaluate = lambda dataset, metrics: self._result_obj(df)
        _patch_ragas(monkeypatch, fake_ragas, fake_ragas_metrics, fake_datasets)

        out = ragas_eval.evaluate_query("Q?", "A.", contexts=["ctx"])
        assert "faithfulness" in out
        assert "answer_relevancy" not in out
