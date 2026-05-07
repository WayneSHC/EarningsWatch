"""
Unit tests for src/core/retriever.py.

Focused on the lightweight components that don't require Qdrant:
  - _maybe_expand (HyDE gating logic, P1-7)
  - _hyde_expand (LLM call + LRU cache + failure fallback)

The heavy paths (vector_search, hybrid_search, retrieve_coverage) need
Qdrant + embedding model and are exercised by benchmark.py end-to-end.
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
