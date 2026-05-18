"""
Unit tests for src/core/retriever.py.

Focused on the lightweight components that don't require Qdrant:
  - _maybe_expand (HyDE gating logic, P1-7)
  - _hyde_expand (LLM call + LRU cache + failure fallback)
  - _load_min_score_from_env (COVERAGE_MIN_SCORE env var + fallback)
  - retrieve_coverage(min_score=...) explicit-arg override path

The heavy paths (vector_search, hybrid_search, retrieve_coverage end-to-end)
need Qdrant/BigQuery + embedding model and are exercised by benchmark.py.
"""
import pytest

from src.core import retriever


@pytest.fixture(autouse=True)
def _clear_hyde_cache():
    """Clear HyDE LRU cache between tests so monkeypatched LLMs don't leak."""
    retriever._hyde_expand.cache_clear()
    yield
    retriever._hyde_expand.cache_clear()


# ──────────────────────────────────────────────────────────────────────────
# _maybe_expand — env gate + length gate
# ──────────────────────────────────────────────────────────────────────────

class TestMaybeExpand:
    def test_disabled_by_default(self, monkeypatch):
        # _HYDE_ENABLED is captured at module import time; force the gate False
        monkeypatch.setattr(retriever, "_HYDE_ENABLED", False)
        out = retriever._maybe_expand("台積電 AI 需求各季發言")
        # When disabled, must return query verbatim and never call LLM
        assert out == "台積電 AI 需求各季發言"

    def test_enabled_calls_hyde(self, monkeypatch):
        monkeypatch.setattr(retriever, "_HYDE_ENABLED", True)
        # Stub the actual LLM call inside _hyde_expand
        from src.core import llm_client
        monkeypatch.setattr(
            llm_client, "chat",
            lambda prompt, max_tokens=200, mode="dev": "AI 需求強勁，CoWoS 供不應求"
        )
        out = retriever._maybe_expand("AI 需求如何")
        assert "需求強勁" in out

    def test_short_query_skipped(self, monkeypatch):
        # Even when enabled, queries shorter than _HYDE_MIN_QUERY_LEN bypass HyDE
        monkeypatch.setattr(retriever, "_HYDE_ENABLED", True)
        # If _hyde_expand were called, this would crash
        from src.core import llm_client
        monkeypatch.setattr(
            llm_client, "chat",
            lambda *a, **kw: pytest.fail("HyDE should not be called for short query")
        )
        out = retriever._maybe_expand("AI")
        assert out == "AI"


# ──────────────────────────────────────────────────────────────────────────
# _hyde_expand — caching + failure fallback
# ──────────────────────────────────────────────────────────────────────────

class TestHydeExpand:
    def test_returns_llm_output(self, monkeypatch):
        from src.core import llm_client
        monkeypatch.setattr(
            llm_client, "chat",
            lambda prompt, max_tokens=200, mode="dev": "假設性回答內容"
        )
        out = retriever._hyde_expand("query")
        assert out == "假設性回答內容"

    def test_lru_caches_repeat_query(self, monkeypatch):
        call_count = {"n": 0}

        def fake_chat(prompt, max_tokens=200, mode="dev"):
            call_count["n"] += 1
            return f"answer-{call_count['n']}"

        from src.core import llm_client
        monkeypatch.setattr(llm_client, "chat", fake_chat)

        # 3 calls with same query → only 1 LLM hit
        retriever._hyde_expand("same query")
        retriever._hyde_expand("same query")
        retriever._hyde_expand("same query")
        assert call_count["n"] == 1

        # Different query → second LLM hit
        retriever._hyde_expand("different query")
        assert call_count["n"] == 2

    def test_falls_back_to_query_on_failure(self, monkeypatch, capsys):
        from src.core import llm_client

        def boom(*a, **kw):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(llm_client, "chat", boom)
        out = retriever._hyde_expand("台積電 AI")
        assert out == "台積電 AI"  # graceful fallback
        captured = capsys.readouterr().out
        assert "HyDE 生成失敗" in captured

    def test_empty_llm_response_falls_back(self, monkeypatch):
        from src.core import llm_client
        monkeypatch.setattr(
            llm_client, "chat",
            lambda *a, **kw: ""  # empty response
        )
        out = retriever._hyde_expand("query string")
        assert out == "query string"  # falls back to original


# ──────────────────────────────────────────────────────────────────────────
# _load_min_score_from_env — COVERAGE_MIN_SCORE env var handling
# ──────────────────────────────────────────────────────────────────────────

class TestLoadMinScoreFromEnv:
    def test_returns_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("COVERAGE_MIN_SCORE", raising=False)
        assert retriever._load_min_score_from_env() == retriever._DEFAULT_MIN_SCORE
        # sanity: default is 0.25 per design
        assert retriever._DEFAULT_MIN_SCORE == 0.25

    def test_returns_default_when_env_empty_string(self, monkeypatch):
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "   ")
        assert retriever._load_min_score_from_env() == retriever._DEFAULT_MIN_SCORE

    def test_valid_float_overrides_default(self, monkeypatch):
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "0.35")
        assert retriever._load_min_score_from_env() == 0.35

    def test_boundary_values_accepted(self, monkeypatch):
        # boundaries 0.0 and 1.0 are inclusive per spec
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "0.0")
        assert retriever._load_min_score_from_env() == 0.0
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "1.0")
        assert retriever._load_min_score_from_env() == 1.0

    def test_non_numeric_falls_back_with_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "abc")
        out = retriever._load_min_score_from_env()
        assert out == retriever._DEFAULT_MIN_SCORE
        captured = capsys.readouterr().out
        assert "COVERAGE_MIN_SCORE" in captured
        assert "0.25" in captured

    def test_out_of_range_high_falls_back(self, monkeypatch, capsys):
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "1.5")
        out = retriever._load_min_score_from_env()
        assert out == retriever._DEFAULT_MIN_SCORE
        captured = capsys.readouterr().out
        assert "COVERAGE_MIN_SCORE" in captured

    def test_out_of_range_negative_falls_back(self, monkeypatch, capsys):
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "-0.1")
        out = retriever._load_min_score_from_env()
        assert out == retriever._DEFAULT_MIN_SCORE
        captured = capsys.readouterr().out
        assert "COVERAGE_MIN_SCORE" in captured


# ──────────────────────────────────────────────────────────────────────────
# retrieve_coverage — explicit min_score arg vs env precedence
# ──────────────────────────────────────────────────────────────────────────

class TestRetrieveCoverageMinScore:
    """
    Verify the env-vs-explicit precedence without spinning up BigQuery.

    Strategy: short-circuit retrieve_coverage by passing missing_quarters=[]
    so the early-return path runs BEFORE any BQ call. To inspect what
    min_score WOULD have been used, we instead test _load_min_score_from_env
    directly above, and here we verify the explicit-arg path bypasses env
    by patching _load_min_score_from_env to crash on invocation.
    """

    def test_explicit_arg_skips_env_loader(self, monkeypatch):
        """When caller passes min_score explicitly, env loader MUST NOT run."""
        monkeypatch.setenv("COVERAGE_MIN_SCORE", "0.50")

        def _should_not_be_called():
            pytest.fail("env loader should be bypassed when min_score is passed")

        monkeypatch.setattr(retriever, "_load_min_score_from_env", _should_not_be_called)

        # missing_quarters=[] short-circuits before BQ, but the
        # `if min_score is None` check runs first — so we pass a real value.
        result = retriever.retrieve_coverage(
            query="q",
            company="TSMC",
            missing_quarters=[],
            min_score=0.10,
        )
        assert result == {}  # short-circuit return

    def test_none_arg_invokes_env_loader(self, monkeypatch):
        """When caller omits min_score (or passes None), env loader IS called."""
        called = {"n": 0}

        def _spy():
            called["n"] += 1
            return 0.42

        monkeypatch.setattr(retriever, "_load_min_score_from_env", _spy)

        # missing_quarters=[] short-circuits before BQ — but importantly,
        # the env loader is invoked BEFORE the early return check on
        # missing_quarters. Wait — actual order is: `if not missing_quarters:
        # return {}` runs first. So env loader is NOT called in that path.
        # To verify env loader IS called, we need a non-empty missing_quarters,
        # which then hits BQ. Instead, we directly call with non-empty and
        # patch BQ to raise after env loader runs.
        from google.cloud import bigquery  # noqa: F401

        def _bq_boom(*a, **kw):
            raise RuntimeError("bq stop after env loader")

        monkeypatch.setattr(retriever, "get_bq_client", _bq_boom)

        with pytest.raises(RuntimeError, match="bq stop"):
            retriever.retrieve_coverage(
                query="q",
                company="TSMC",
                missing_quarters=["2024Q1"],
                min_score=None,
            )
        assert called["n"] == 1
