"""
Unit tests for src/ui/quarters.py.

Covers the ui-quarters capability spec:
  - get_available_quarters: sorts by quarter
  - BQ failure / empty result → fallback list (independent copy)
  - get_available_quarters_union: empty tuple → all-companies path
  - union dedupes + sorts
"""
import pytest


# The Streamlit @cache_data decorator caches across tests. Clear before each.
@pytest.fixture(autouse=True)
def _clear_quarters_cache():
    from src.ui import quarters
    # st.cache_data has .clear() per decorated function
    try:
        quarters.get_available_quarters.clear()
        quarters.get_available_quarters_union.clear()
    except AttributeError:
        # If the decorator API changes, fall back silently
        pass
    yield


# ──────────────────────────────────────────────────────────────────────────
# get_available_quarters — sort + fallback
# ──────────────────────────────────────────────────────────────────────────

class _FakeBqRow:
    def __init__(self, quarter):
        self.quarter = quarter


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeBqClient:
    def __init__(self, rows=None, *, raise_on_query=False):
        self._rows = rows or []
        self._raise = raise_on_query

    def query(self, sql, job_config=None):
        if self._raise:
            raise RuntimeError("BQ unavailable")
        return _FakeQueryJob(self._rows)


class TestGetAvailableQuarters:
    def test_returns_sorted_quarters(self, monkeypatch):
        from src.ui import quarters
        from src.core import bq_client

        monkeypatch.setattr(
            bq_client, "get_bq_client",
            lambda: _FakeBqClient([
                _FakeBqRow("2024Q3"),
                _FakeBqRow("2023Q4"),
                _FakeBqRow("2024Q1"),
            ])
        )

        out = quarters.get_available_quarters("台積電")
        assert out == ["2023Q4", "2024Q1", "2024Q3"]

    def test_bq_failure_falls_back(self, monkeypatch, capsys):
        from src.ui import quarters
        from src.core import bq_client

        monkeypatch.setattr(
            bq_client, "get_bq_client",
            lambda: _FakeBqClient(raise_on_query=True)
        )

        out = quarters.get_available_quarters("台積電")
        assert out == list(quarters._FALLBACK_QUARTERS)
        captured = capsys.readouterr()
        assert "get_available_quarters 意外失敗" in captured.out

    def test_empty_result_falls_back(self, monkeypatch):
        from src.ui import quarters
        from src.core import bq_client

        monkeypatch.setattr(
            bq_client, "get_bq_client",
            lambda: _FakeBqClient(rows=[])
        )

        out = quarters.get_available_quarters("X")
        assert out == list(quarters._FALLBACK_QUARTERS)

    def test_fallback_is_independent_copy(self, monkeypatch):
        from src.ui import quarters
        from src.core import bq_client

        monkeypatch.setattr(
            bq_client, "get_bq_client",
            lambda: _FakeBqClient(raise_on_query=True)
        )

        out = quarters.get_available_quarters("X")
        out.append("MUTATED")

        # Module constant must be untouched
        assert "MUTATED" not in quarters._FALLBACK_QUARTERS


# ──────────────────────────────────────────────────────────────────────────
# get_available_quarters_union
# ──────────────────────────────────────────────────────────────────────────

class TestGetAvailableQuartersUnion:
    def test_empty_tuple_falls_to_all_path(self, monkeypatch):
        from src.ui import quarters

        called = {"with_none": False}

        def fake_get(c=None):
            called["with_none"] = c is None
            return ["2024Q1"]

        monkeypatch.setattr(quarters, "get_available_quarters", fake_get)

        out = quarters.get_available_quarters_union(())
        assert out == ["2024Q1"]
        assert called["with_none"] is True

    def test_union_dedupes_and_sorts(self, monkeypatch):
        from src.ui import quarters

        per_company = {
            "台積電": ["2024Q1", "2024Q2"],
            "聯發科": ["2024Q2", "2024Q3"],
        }
        monkeypatch.setattr(
            quarters, "get_available_quarters",
            lambda c: per_company[c]
        )

        out = quarters.get_available_quarters_union(("台積電", "聯發科"))
        assert out == ["2024Q1", "2024Q2", "2024Q3"]

    def test_cross_year_sort(self, monkeypatch):
        from src.ui import quarters

        monkeypatch.setattr(
            quarters, "get_available_quarters",
            lambda c: ["2024Q1", "2023Q4"]
        )

        out = quarters.get_available_quarters_union(("X",))
        assert out == ["2023Q4", "2024Q1"]
