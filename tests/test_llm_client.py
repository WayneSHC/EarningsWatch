"""
Unit tests for src/core/llm_client.py.

Coverage targets the v1.1 multi-backend cascade:
  - _detect_backend: explicit override / auto-detect order / no-key error
  - chat() cascade: quota markers trigger fallback, non-quota errors raise
  - Empty prompt rejection
  - set_backend / available_backends
"""
import os

import pytest

from src.core import llm_client as lc


# ──────────────────────────────────────────────────────────────────────────
# _detect_backend
# ──────────────────────────────────────────────────────────────────────────

class TestDetectBackend:
    def test_explicit_backend_wins(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "anthropic"

    def test_auto_detect_follows_order(self, monkeypatch):
        # Conftest sets openai/gemini/anthropic. _AUTO_DETECT_ORDER starts with openai.
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "openai"

    def test_auto_detect_skips_missing_keys(self, monkeypatch):
        # Wipe openai → gemini wins (next in order)
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "gemini"

    def test_explicit_invalid_falls_back(self, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BACKEND", "no_such_backend")
        lc._detect_backend.cache_clear()
        # Must not crash; should warn and fall through to auto-detect
        result = lc._detect_backend()
        assert result in lc.BACKEND_MODELS
        assert "不在支援清單" in capsys.readouterr().out

    def test_explicit_backend_missing_key_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "groq")  # GROQ_API_KEY not set in conftest
        lc._detect_backend.cache_clear()
        with pytest.raises(EnvironmentError, match="GROQ_API_KEY"):
            lc._detect_backend()

    def test_no_keys_at_all_raises(self, monkeypatch):
        for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
                  "GROQ_API_KEY", "COHERE_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        lc._detect_backend.cache_clear()
        with pytest.raises(EnvironmentError, match="找不到任何 LLM API Key"):
            lc._detect_backend()


# ──────────────────────────────────────────────────────────────────────────
# chat() — cascade behavior
# ──────────────────────────────────────────────────────────────────────────

class TestChatCascade:
    def test_empty_prompt_rejected(self):
        with pytest.raises(ValueError, match="不可為空"):
            lc.chat("")
        with pytest.raises(ValueError, match="不可為空"):
            lc.chat("   \n  ")

    def test_primary_succeeds_no_fallback(self, monkeypatch):
        calls = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            calls.append(backend)
            return "Pong."

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("ping")
        assert out == "Pong."
        assert calls == ["openai"]   # only the primary, no cascade

    def test_quota_marker_triggers_fallback(self, monkeypatch, capsys):
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "openai":
                raise RuntimeError("429: RESOURCE_EXHAUSTED quota exceeded")
            if backend == "gemini":
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return f"reply from {backend}"

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("hello")
        assert out == "reply from anthropic"
        assert attempts == ["openai", "gemini", "anthropic"]
        captured = capsys.readouterr().out
        assert "openai 配額/認證失敗" in captured
        assert "gemini 配額/認證失敗" in captured

    def test_credit_balance_marker_triggers_fallback(self, monkeypatch):
        # Anthropic 400 BadRequestError when account has no credits
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "openai":
                return "ok"
            raise RuntimeError("Your credit balance is too low")

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        # Force primary=anthropic so we hit the credit-balance path
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        lc._detect_backend.cache_clear()

        out = lc.chat("hi")
        assert out == "ok"
        # First anthropic (credit out) → fallback chain reaches openai
        assert "anthropic" in attempts
        assert attempts[-1] == "openai"

    def test_404_marker_triggers_fallback(self, monkeypatch):
        # Wrong model name (e.g. gemini-3-pro vs gemini-3-pro-preview) returns 404
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "openai":
                raise RuntimeError("404 NOT_FOUND model_not_found")
            return "ok"

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("hi")
        assert out == "ok"
        assert attempts[0] == "openai"
        assert len(attempts) >= 2

    def test_503_marker_triggers_fallback(self, monkeypatch):
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "openai":
                raise RuntimeError("503 service unavailable")
            return "ok"

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        assert lc.chat("hi") == "ok"
        assert len(attempts) >= 2

    def test_non_quota_error_raises_immediately(self, monkeypatch):
        # Network/programming errors should NOT cascade — we want the real error fast
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            raise RuntimeError("Connection refused by remote host")

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        with pytest.raises(RuntimeError, match="Connection refused"):
            lc.chat("hi")
        # Should fail on first attempt only — no cascade for non-quota errors
        assert attempts == ["openai"]

    def test_all_backends_fail_raises_last_error(self, monkeypatch):
        def fake_dispatch(backend, prompt, model, max_tokens):
            raise RuntimeError(f"{backend} quota exceeded")

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        with pytest.raises(RuntimeError, match="quota exceeded"):
            lc.chat("hi")


# ──────────────────────────────────────────────────────────────────────────
# available_backends / set_backend / which_backend
# ──────────────────────────────────────────────────────────────────────────

class TestBackendManagement:
    def test_available_backends_lists_keys_with_env(self, monkeypatch):
        # Conftest gives us openai/gemini/anthropic
        out = lc.available_backends()
        assert set(out) == {"openai", "gemini", "anthropic"}

    def test_available_backends_respects_order(self, monkeypatch):
        out = lc.available_backends()
        # _AUTO_DETECT_ORDER is openai → gemini → anthropic → groq → cohere
        assert out == ["openai", "gemini", "anthropic"]

    def test_set_backend_clears_cache(self, monkeypatch):
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "openai"
        lc.set_backend("gemini")
        assert lc._detect_backend() == "gemini"

    def test_set_backend_rejects_unknown(self):
        with pytest.raises(ValueError, match="未知後端"):
            lc.set_backend("not_a_backend")

    def test_set_backend_rejects_missing_key(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(ValueError, match="缺少 GROQ_API_KEY"):
            lc.set_backend("groq")


# ──────────────────────────────────────────────────────────────────────────
# Quota marker matrix — guard against future regressions
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("err_msg", [
    "RESOURCE_EXHAUSTED",
    "quota exceeded",
    "insufficient_quota",
    "billing required",
    "Your credit balance is too low",
    "low balance",
    "401 Unauthorized",
    "403 Forbidden",
    "404 NOT_FOUND",
    "model_not_found",
    "is not found",
    "does not exist",
    "503 service unavailable",
    "Provider overloaded",
    "529 overloaded",
])
def test_quota_marker_recognized(monkeypatch, err_msg):
    """All these strings must trigger backend fallback, not raise immediately."""
    attempts = []

    def fake_dispatch(backend, prompt, model, max_tokens):
        attempts.append(backend)
        if backend == "openai":
            raise RuntimeError(err_msg)
        return "ok"

    monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
    out = lc.chat("hi")
    assert out == "ok"
    # If marker was recognized → cascade ran → at least 2 attempts
    assert len(attempts) >= 2, f"Marker {err_msg!r} did NOT trigger cascade"
