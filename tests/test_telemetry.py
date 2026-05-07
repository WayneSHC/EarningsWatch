"""
Unit tests for src/core/telemetry.py.

Coverage:
  - estimate_cost: pricing table lookup, missing-model fallback
  - LLMCall: dataclass round-trip
  - registry: thread-safe accumulation, summary aggregation, reset
  - llm_client integration: chat() records on success / on error
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.core import telemetry, llm_client as lc


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty registry."""
    telemetry.reset()
    yield
    telemetry.reset()


# ──────────────────────────────────────────────────────────────────────────
# estimate_cost
# ──────────────────────────────────────────────────────────────────────────

class TestEstimateCost:
    def test_known_model(self):
        # gpt-5o: 5/1M input, 15/1M output
        cost = telemetry.estimate_cost("openai", "gpt-5o", 1_000_000, 1_000_000)
        assert cost == pytest.approx(20.0)

    def test_partial_tokens(self):
        # 1k input, 2k output for gpt-5o-mini (0.15 / 0.60)
        cost = telemetry.estimate_cost("openai", "gpt-5o-mini", 1000, 2000)
        # 0.00015 + 0.0012 = 0.00135
        assert cost == pytest.approx(0.00135)

    def test_unknown_model_returns_zero(self):
        # Pricing table miss must not raise; just return 0
        cost = telemetry.estimate_cost("openai", "future-model-xyz", 1000, 1000)
        assert cost == 0.0

    def test_unknown_backend_returns_zero(self):
        cost = telemetry.estimate_cost("nobackend", "any", 1000, 1000)
        assert cost == 0.0


# ──────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_record_and_summary(self):
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5o-mini",
            prompt_tokens=100, completion_tokens=50, duration_ms=200.0,
            cost_usd=0.001,
        ))
        s = telemetry.summary()
        assert s["total_calls"] == 1
        assert s["successful_calls"] == 1
        assert s["prompt_tokens"] == 100
        assert s["completion_tokens"] == 50
        assert s["total_tokens"] == 150
        assert s["estimated_cost_usd"] == 0.001
        assert s["by_backend"]["openai"]["calls"] == 1
        assert s["by_backend"]["openai"]["tokens"] == 150

    def test_aggregates_across_backends(self):
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5o-mini",
            prompt_tokens=100, completion_tokens=50, cost_usd=0.001,
        ))
        telemetry.record(telemetry.LLMCall(
            backend="gemini", model="gemini-3.0-flash",
            prompt_tokens=200, completion_tokens=80, cost_usd=0.0001,
        ))
        s = telemetry.summary()
        assert s["total_calls"] == 2
        assert s["total_tokens"] == 430
        assert set(s["by_backend"].keys()) == {"openai", "gemini"}
        assert s["by_backend"]["openai"]["tokens"] == 150
        assert s["by_backend"]["gemini"]["tokens"] == 280

    def test_failed_calls_counted_separately(self):
        telemetry.record(telemetry.LLMCall(
            backend="openai", model="gpt-5o", error="RateLimitError"
        ))
        telemetry.record(telemetry.LLMCall(
            backend="gemini", model="gemini-3.0-flash",
            prompt_tokens=10, completion_tokens=5,
        ))
        s = telemetry.summary()
        assert s["total_calls"] == 2
        assert s["successful_calls"] == 1
        assert s["failed_calls"] == 1

    def test_reset_clears_state(self):
        telemetry.record(telemetry.LLMCall(backend="openai", model="m"))
        assert telemetry.summary()["total_calls"] == 1
        telemetry.reset()
        assert telemetry.summary()["total_calls"] == 0
        assert telemetry.calls() == []

    def test_thread_safe_concurrent_writes(self):
        # 50 threads × 20 records = 1000; nothing should be lost or duplicated
        def writer(_):
            for _ in range(20):
                telemetry.record(telemetry.LLMCall(
                    backend="openai", model="x", prompt_tokens=1
                ))

        with ThreadPoolExecutor(max_workers=50) as pool:
            list(pool.map(writer, range(50)))

        s = telemetry.summary()
        assert s["total_calls"] == 1000
        assert s["prompt_tokens"] == 1000


# ──────────────────────────────────────────────────────────────────────────
# llm_client integration
# ──────────────────────────────────────────────────────────────────────────

class TestLLMClientIntegration:
    def test_successful_chat_records_usage(self, monkeypatch):
        def fake_dispatch(backend, prompt, model, max_tokens):
            return "Pong", 12, 5

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("ping")
        assert out == "Pong"

        s = telemetry.summary()
        assert s["total_calls"] == 1
        assert s["prompt_tokens"] == 12
        assert s["completion_tokens"] == 5
        # gpt-5o pricing applied
        assert s["estimated_cost_usd"] > 0

    def test_fallback_records_both_failure_and_success(self, monkeypatch):
        def fake_dispatch(backend, prompt, model, max_tokens):
            if backend == "openai":
                raise RuntimeError("quota exceeded")
            return "ok", 8, 3

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        lc.chat("hi")

        s = telemetry.summary()
        assert s["total_calls"] == 2
        assert s["successful_calls"] == 1
        assert s["failed_calls"] == 1
        assert s["by_backend"]["openai"]["calls"] == 1
        assert s["by_backend"]["gemini"]["calls"] == 1
