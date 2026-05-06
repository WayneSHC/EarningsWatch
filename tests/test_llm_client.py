"""
Unit tests for src/core/llm_client.py.

Coverage targets the multi-backend cascade (post anthropic/groq removal):
  - _detect_backend: explicit override / auto-detect order / no-key error
  - chat() cascade: quota markers trigger fallback, non-quota errors raise
  - Empty prompt rejection
  - set_backend / available_backends

Active backends: openai → gemini → cohere
Removed: anthropic, groq (no API key)
"""
import os

import pytest

from src.core import llm_client as lc


# ──────────────────────────────────────────────────────────────────────────
# _detect_backend
# ──────────────────────────────────────────────────────────────────────────

class TestDetectBackend:
    def test_explicit_backend_wins(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "gemini")
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "gemini"

    def test_auto_detect_follows_order(self, monkeypatch):
        # Conftest sets openai/gemini. _AUTO_DETECT_ORDER starts with openai.
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
        result = lc._detect_backend()
        assert result in lc.BACKEND_MODELS
        assert "不在支援清單" in capsys.readouterr().out

    def test_explicit_anthropic_no_longer_supported(self, monkeypatch, capsys):
        # anthropic was removed; treat as unknown → warn + fall back
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        lc._detect_backend.cache_clear()
        result = lc._detect_backend()
        assert result in lc.BACKEND_MODELS
        assert "不在支援清單" in capsys.readouterr().out

    def test_explicit_backend_missing_key_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "cohere")  # COHERE_API_KEY not set
        lc._detect_backend.cache_clear()
        with pytest.raises(EnvironmentError, match="COHERE_API_KEY"):
            lc._detect_backend()

    def test_no_keys_at_all_raises(self, monkeypatch):
        for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "COHERE_API_KEY"):
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
                # Pure quota error (no "429" → that hits rate-limit branch instead)
                raise RuntimeError("RESOURCE_EXHAUSTED: quota exceeded")
            return f"reply from {backend}"

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("hello")
        assert out == "reply from gemini"
        assert attempts == ["openai", "gemini"]
        captured = capsys.readouterr().out
        # New friendly message format
        assert "OpenAI" in captured and ("配額" in captured or "token" in captured)

    def test_credit_balance_marker_triggers_fallback(self, monkeypatch):
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "openai":
                raise RuntimeError("Your credit balance is too low")
            return "ok"

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("hi")
        assert out == "ok"
        assert attempts[0] == "openai"
        assert len(attempts) >= 2

    def test_404_marker_triggers_fallback(self, monkeypatch):
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

    def test_rate_limit_429_triggers_fallback(self, monkeypatch):
        # Newly added: 429 / rate_limit markers
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "openai":
                raise RuntimeError("429 Too Many Requests: rate limit reached")
            return "ok"

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        assert lc.chat("hi") == "ok"
        assert len(attempts) >= 2

    def test_non_quota_error_raises_immediately(self, monkeypatch):
        # Truly non-transient, non-quota error → must NOT cascade.
        # (Avoid words like "connection"/"timeout" which are transient markers.)
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            raise RuntimeError("JSON schema validation failed: bad payload")

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        with pytest.raises(RuntimeError, match="JSON schema"):
            lc.chat("hi")
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
    def test_available_backends_lists_keys_with_env(self):
        # Conftest gives us openai + gemini
        out = lc.available_backends()
        assert set(out) == {"openai", "gemini"}

    def test_available_backends_respects_order(self):
        out = lc.available_backends()
        assert out == ["openai", "gemini"]

    def test_set_backend_clears_cache(self, monkeypatch):
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "openai"
        lc.set_backend("gemini")
        assert lc._detect_backend() == "gemini"

    def test_set_backend_rejects_unknown(self):
        with pytest.raises(ValueError, match="未知後端"):
            lc.set_backend("not_a_backend")

    def test_set_backend_rejects_removed_anthropic(self):
        # anthropic removed entirely → not in BACKEND_MODELS → "未知後端"
        with pytest.raises(ValueError, match="未知後端"):
            lc.set_backend("anthropic")

    def test_set_backend_rejects_missing_key(self, monkeypatch):
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
        with pytest.raises(ValueError, match="缺少 COHERE_API_KEY"):
            lc.set_backend("cohere")


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
    "429 Too Many Requests",
    "rate limit exceeded",
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
    assert len(attempts) >= 2, f"Marker {err_msg!r} did NOT trigger cascade"


# ──────────────────────────────────────────────────────────────────────────
# _format_quota_message — friendly Chinese translations
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("err_msg,expected_phrase", [
    # 配額類
    ("RESOURCE_EXHAUSTED quota",          "今日 token / 配額已用完"),
    ("insufficient_quota",                "今日 token / 配額已用完"),
    ("Your credit balance is too low",    "今日 token / 配額已用完"),
    ("billing required",                  "今日 token / 配額已用完"),
    # Rate limit
    ("429 Too Many Requests",             "速率限制"),
    ("rate limit exceeded",               "速率限制"),
    # 認證
    ("401 Unauthorized",                  "API Key 失效或權限不足"),
    ("403 Forbidden",                     "API Key 失效或權限不足"),
    # 模型不存在
    ("404 NOT_FOUND",                     "模型名稱不存在或已下線"),
    ("model_not_found",                   "模型名稱不存在或已下線"),
    ("does not exist",                    "模型名稱不存在或已下線"),
    # 服務不可用
    ("503 service unavailable",           "服務暫時不可用"),
    ("Provider overloaded",               "服務暫時不可用"),
    # 通用降級
    ("Strange undocumented error xyz123", "呼叫失敗"),
])
def test_format_quota_message_translation(err_msg, expected_phrase):
    """每個錯誤標記都應翻譯成正確的中文提示，且包含後端 label。"""
    out = lc._format_quota_message("openai", err_msg)
    assert "OpenAI" in out, f"Backend label missing in: {out!r}"
    assert expected_phrase in out, (
        f"Marker {err_msg!r} should produce {expected_phrase!r}, got: {out!r}"
    )


def test_format_quota_message_includes_backend_label():
    """三個後端的 label 都應正確帶出。"""
    for backend in ("openai", "gemini", "cohere"):
        out = lc._format_quota_message(backend, "quota exceeded")
        label = lc._BACKEND_LABEL[backend]
        assert label in out, f"Backend {backend} label missing: {out!r}"


def test_format_quota_message_unknown_backend_falls_back():
    """未知 backend 不應 KeyError，直接用 backend 名當 label。"""
    out = lc._format_quota_message("future_backend", "quota exceeded")
    assert "future_backend" in out
