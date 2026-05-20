"""
Unit tests for src/core/llm_client.py.

Coverage targets the multi-backend cascade:
  - _detect_backend: explicit override / auto-detect order / no-key error
  - chat() cascade: quota markers trigger fallback, non-quota errors raise
  - Empty prompt rejection
  - set_backend / available_backends

Active backends: gemini → openai → anthropic → cohere
Removed: groq (no API key)

Tests leave ANTHROPIC_API_KEY unset by default (conftest) so the auto-detect
order isn't affected. Tests that explicitly need it set it via monkeypatch.
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
        # Conftest sets openai/gemini. _AUTO_DETECT_ORDER starts with gemini.
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "gemini"

    def test_auto_detect_skips_missing_keys(self, monkeypatch):
        # Wipe gemini → openai wins (next in order)
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "openai"

    def test_explicit_invalid_falls_back(self, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BACKEND", "no_such_backend")
        lc._detect_backend.cache_clear()
        result = lc._detect_backend()
        assert result in lc.BACKEND_MODELS
        assert "不在支援清單" in capsys.readouterr().out

    def test_explicit_anthropic_supported(self, monkeypatch):
        # anthropic is now a supported backend; with a key set, _detect_backend honors it.
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "anthropic"

    def test_explicit_anthropic_missing_key_raises(self, monkeypatch):
        # Selecting anthropic without a key → same error path as any other backend.
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        lc._detect_backend.cache_clear()
        with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
            lc._detect_backend()

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
            return "Pong.", 0, 0

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("ping")
        assert out == "Pong."
        assert calls == ["gemini"]   # only the primary, no cascade

    def test_quota_marker_triggers_fallback(self, monkeypatch, capsys):
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "gemini":
                # Pure quota error (no "429" → that hits rate-limit branch instead)
                raise RuntimeError("RESOURCE_EXHAUSTED: quota exceeded")
            return f"reply from {backend}", 0, 0

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("hello")
        assert out == "reply from openai"
        assert attempts == ["gemini", "openai"]
        captured = capsys.readouterr().out
        # New friendly message format
        assert "Gemini" in captured and ("配額" in captured or "token" in captured)

    def test_credit_balance_marker_triggers_fallback(self, monkeypatch):
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "gemini":
                raise RuntimeError("Your credit balance is too low")
            return "ok", 0, 0

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("hi")
        assert out == "ok"
        assert attempts[0] == "gemini"
        assert len(attempts) >= 2

    def test_404_marker_triggers_fallback(self, monkeypatch):
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "gemini":
                raise RuntimeError("404 NOT_FOUND model_not_found")
            return "ok", 0, 0

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        out = lc.chat("hi")
        assert out == "ok"
        assert attempts[0] == "gemini"
        assert len(attempts) >= 2

    def test_503_marker_triggers_fallback(self, monkeypatch):
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "gemini":
                raise RuntimeError("503 service unavailable")
            return "ok", 0, 0

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        assert lc.chat("hi") == "ok"
        assert len(attempts) >= 2

    def test_rate_limit_429_triggers_fallback(self, monkeypatch):
        # Newly added: 429 / rate_limit markers
        attempts = []

        def fake_dispatch(backend, prompt, model, max_tokens):
            attempts.append(backend)
            if backend == "gemini":
                raise RuntimeError("429 Too Many Requests: rate limit reached")
            return "ok", 0, 0

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
        assert attempts == ["gemini"]

    def test_all_backends_fail_raises_llm_unavailable(self, monkeypatch):
        """
        當所有後端皆失敗，chat() 應 raise LLMUnavailableError（仍是 RuntimeError 子類），
        並把原始例外存到 .root_cause，避免把含 HTTP body 的 raw SDK 例外洩漏到 UI。
        """
        def fake_dispatch(backend, prompt, model, max_tokens):
            raise RuntimeError(f"{backend} quota exceeded")

        monkeypatch.setattr(lc, "_dispatch", fake_dispatch)
        with pytest.raises(lc.LLMUnavailableError) as exc_info:
            lc.chat("hi")

        # 顯示給使用者的訊息必須乾淨（不含 raw 後端錯誤字串）
        assert "quota exceeded" not in str(exc_info.value)
        # root_cause 留給 logger 使用，必須仍是原始例外
        assert isinstance(exc_info.value.root_cause, RuntimeError)
        assert "quota exceeded" in str(exc_info.value.root_cause)

    def test_friendly_error_message_categorizes(self):
        """friendly_error_message 應該把常見錯誤分類為乾淨的中文摘要。"""
        rate = lc.friendly_error_message(RuntimeError("HTTP 429 too many requests"))
        assert "速率限制" in rate
        quota = lc.friendly_error_message(RuntimeError("You are using a Trial key"))
        assert "配額" in quota
        # LLMUnavailableError 應該回傳自己的 friendly_message，不再二次分類
        wrapped = lc.LLMUnavailableError("custom clean msg", root_cause=None)
        assert lc.friendly_error_message(wrapped) == "custom clean msg"


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
        assert out == ["gemini", "openai"]

    def test_set_backend_clears_cache(self, monkeypatch):
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "gemini"
        lc.set_backend("openai")
        assert lc._detect_backend() == "openai"

    def test_set_backend_rejects_unknown(self):
        with pytest.raises(ValueError, match="未知後端"):
            lc.set_backend("not_a_backend")

    def test_set_backend_accepts_anthropic_with_key(self, monkeypatch):
        # anthropic is supported again; set_backend should accept it when a key exists.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
        lc.set_backend("anthropic")
        lc._detect_backend.cache_clear()
        assert lc._detect_backend() == "anthropic"

    def test_set_backend_rejects_missing_key(self, monkeypatch):
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
        with pytest.raises(ValueError, match="COHERE_API_KEY"):
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
        if backend == "gemini":
            raise RuntimeError(err_msg)
        return "ok", 0, 0

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
    # OpenAI real-world quota-exhausted message: 429 + quota + billing 同時出現
    # 必須優先匹配 quota 而非 429，否則使用者會誤以為「等一下就好」
    ("Error code: 429 - You exceeded your current quota, please check your "
     "plan and billing details.",         "今日 token / 配額已用完"),
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


# ──────────────────────────────────────────────────────────────────────────
# _call_openai_compat — GPT-5 reasoning_effort (spec: llm-backend-cascade)
# ──────────────────────────────────────────────────────────────────────────

class _FakeCompletions:
    """Records create() kwargs; returns a configurable fake chat completion."""

    def __init__(self, content="ok"):
        self._content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        msg = type("M", (), {"content": self._content})()
        choice = type("C", (), {"message": msg, "finish_reason": "stop"})()
        usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 20})()
        return type("R", (), {"choices": [choice], "usage": usage})()


class _FakeOpenAIClient:
    def __init__(self, content="ok"):
        completions = _FakeCompletions(content)
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


class TestOpenAIReasoningEffort:
    """Regression: GPT-5 is a reasoning model — max_completion_tokens covers
    both reasoning AND output. Without reasoning_effort the model can spend
    the whole budget reasoning and return empty content. _call_openai_compat
    must pass reasoning_effort for gpt-5* models (and NOT for others).
    """

    def test_gpt5_passes_reasoning_effort(self, monkeypatch):
        fake = _FakeOpenAIClient(content='{"ok": true}')
        monkeypatch.setattr(lc, "_get_openai_client", lambda *a, **kw: fake)

        lc._call_openai_compat("prompt", "gpt-5", 600)

        kwargs = fake.completions.last_kwargs
        assert kwargs.get("reasoning_effort") == "minimal"

    def test_gpt5_mini_passes_reasoning_effort(self, monkeypatch):
        fake = _FakeOpenAIClient(content='{"ok": true}')
        monkeypatch.setattr(lc, "_get_openai_client", lambda *a, **kw: fake)

        lc._call_openai_compat("prompt", "gpt-5-mini", 600)

        assert fake.completions.last_kwargs.get("reasoning_effort") == "minimal"

    def test_non_gpt5_model_omits_reasoning_effort(self, monkeypatch):
        """gpt-4o is not a reasoning model — passing reasoning_effort would 400."""
        fake = _FakeOpenAIClient(content="answer")
        monkeypatch.setattr(lc, "_get_openai_client", lambda *a, **kw: fake)

        lc._call_openai_compat("prompt", "gpt-4o", 600)

        assert "reasoning_effort" not in fake.completions.last_kwargs

    def test_none_content_returns_empty_string_not_crash(self, monkeypatch):
        """If a reasoning model exhausts its budget, content can be None.
        _call_openai_compat must coerce to '' rather than raise AttributeError."""
        fake = _FakeOpenAIClient(content=None)
        monkeypatch.setattr(lc, "_get_openai_client", lambda *a, **kw: fake)

        text, p_tok, c_tok = lc._call_openai_compat("prompt", "gpt-5", 600)

        assert text == ""
        assert p_tok == 10 and c_tok == 20

    def test_reasoning_effort_is_configurable(self, monkeypatch):
        """The effort value comes from module constant `_GPT5_REASONING_EFFORT`
        (populated from LLM_REASONING_EFFORT at import). Patch the constant
        directly — reloading the module would corrupt the lru_cache singletons
        other tests depend on."""
        monkeypatch.setattr(lc, "_GPT5_REASONING_EFFORT", "low")
        fake = _FakeOpenAIClient(content="x")
        monkeypatch.setattr(lc, "_get_openai_client", lambda *a, **kw: fake)

        lc._call_openai_compat("prompt", "gpt-5", 600)

        assert fake.completions.last_kwargs.get("reasoning_effort") == "low"

    def test_reasoning_effort_default_is_minimal(self):
        """Module default (LLM_REASONING_EFFORT unset) is 'minimal'."""
        assert lc._GPT5_REASONING_EFFORT == "minimal"
