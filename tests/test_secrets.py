"""
Unit tests for src/core/secrets.py.

Covers the secrets-management capability spec:
  - get_secret: SM → env → default resolution order
  - Placeholder detection (multiple marker forms, case-insensitive)
  - SDK import / auth failure → graceful degrade to env
  - LRU cache + clear_cache for rotation
  - is_gcp_enabled requires both project and SDK
  - bridge_to_env preserves existing env, fills empty from SM
"""
import pytest

from src.core import secrets as secrets_mod


@pytest.fixture(autouse=True)
def _reset_secret_caches():
    """Force a clean cache at setup so monkeypatched _sm_client / _fetch_from_gcp
    actually run each time. We don't clear on teardown because monkeypatch may
    not yet have restored the original lru_cache-decorated functions —
    clear_cache() depends on cache_clear which doesn't exist on a plain lambda."""
    # Clear before — original lru_cache functions still in place.
    secrets_mod.clear_cache()
    yield


# ──────────────────────────────────────────────────────────────────────────
# get_secret — resolution order
# ──────────────────────────────────────────────────────────────────────────

class TestGetSecretResolution:
    def test_no_gcp_project_uses_env_directly(self, monkeypatch):
        """Without GCP_SECRET_PROJECT, _fetch_from_gcp must not be called."""
        monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
        monkeypatch.setenv("FOO_KEY", "real-env-value")

        called = {"n": 0}

        def fake_fetch(name, project):
            called["n"] += 1
            return "sm-value"

        monkeypatch.setattr(secrets_mod, "_fetch_from_gcp", fake_fetch)

        out = secrets_mod.get_secret("FOO_KEY")

        assert out == "real-env-value"
        assert called["n"] == 0

    def test_gcp_success_returns_sm_value(self, monkeypatch):
        monkeypatch.setenv("GCP_SECRET_PROJECT", "my-proj")
        monkeypatch.setenv("FOO_KEY", "env-fallback")  # should be skipped
        monkeypatch.setattr(secrets_mod, "_fetch_from_gcp",
                            lambda name, project: "sm-value")

        out = secrets_mod.get_secret("FOO_KEY")

        assert out == "sm-value"

    def test_gcp_failure_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("GCP_SECRET_PROJECT", "my-proj")
        monkeypatch.setenv("FOO_KEY", "env-fallback")
        # Simulate fetch failure (NotFound / PermissionDenied / network issue)
        monkeypatch.setattr(secrets_mod, "_fetch_from_gcp",
                            lambda name, project: None)

        out = secrets_mod.get_secret("FOO_KEY")

        assert out == "env-fallback"

    def test_all_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
        monkeypatch.delenv("FOO_KEY", raising=False)

        out = secrets_mod.get_secret("FOO_KEY", default="fallback")

        assert out == "fallback"

    def test_all_missing_default_is_empty_string(self, monkeypatch):
        monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
        monkeypatch.delenv("FOO_KEY", raising=False)

        out = secrets_mod.get_secret("FOO_KEY")

        assert out == ""

    def test_gcp_returns_placeholder_falls_through(self, monkeypatch):
        """SM returning a placeholder value (e.g. example secret never updated)
        must trigger fall-through to env."""
        monkeypatch.setenv("GCP_SECRET_PROJECT", "my-proj")
        monkeypatch.setenv("FOO_KEY", "real-env-value")
        monkeypatch.setattr(secrets_mod, "_fetch_from_gcp",
                            lambda name, project: "<your-api-key>")

        out = secrets_mod.get_secret("FOO_KEY")

        # SM value is placeholder → must use env
        assert out == "real-env-value"


# ──────────────────────────────────────────────────────────────────────────
# Placeholder detection
# ──────────────────────────────────────────────────────────────────────────

class TestPlaceholderDetection:
    @pytest.mark.parametrize("placeholder", [
        "<your-openai-key-here>",
        "<YOUR-API-KEY>",        # uppercase variant
        "your-key-here",
        "PLACEHOLDER",
        "changeme",
        "sk-...",
        "tvly-...",
        "llx-...",
        "ls__...",
    ])
    def test_known_placeholders_detected(self, monkeypatch, placeholder):
        monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
        monkeypatch.setenv("FOO_KEY", placeholder)

        out = secrets_mod.get_secret("FOO_KEY")

        assert out == "", f"Placeholder {placeholder!r} not detected"

    def test_real_value_not_misidentified(self, monkeypatch):
        # Use a short, obviously fake value that:
        # 1. Doesn't match any _PLACEHOLDER_MARKERS pattern → still passes through
        # 2. Doesn't trigger the pre-commit secret scanner (sk- + 20+ chars)
        monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
        monkeypatch.setenv("FOO_KEY", "genuine-value-123")

        out = secrets_mod.get_secret("FOO_KEY")

        assert out == "genuine-value-123"

    def test_empty_string_treated_as_missing(self, monkeypatch):
        monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
        monkeypatch.setenv("FOO_KEY", "")

        out = secrets_mod.get_secret("FOO_KEY", default="d")

        assert out == "d"


# ──────────────────────────────────────────────────────────────────────────
# SDK / auth failure → graceful degrade
# ──────────────────────────────────────────────────────────────────────────

class TestSdkGracefulDegrade:
    def test_sm_client_returns_none_on_import_error(self, monkeypatch, capsys):
        """If google-cloud-secret-manager isn't installed, _sm_client returns None."""
        import builtins
        real_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name.startswith("google.cloud.secretmanager") or name == "google.cloud":
                # Be conservative: only fail the specific submodule
                if name == "google.cloud.secretmanager":
                    raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_import)
        secrets_mod._sm_client.cache_clear()

        client = secrets_mod._sm_client()

        assert client is None
        captured = capsys.readouterr()
        assert "google-cloud-secret-manager" in captured.out

    def test_get_secret_recovers_when_sm_unavailable(self, monkeypatch):
        """Even with GCP_SECRET_PROJECT set, if SM client init returns None,
        get_secret must still resolve from env."""
        monkeypatch.setenv("GCP_SECRET_PROJECT", "my-proj")
        monkeypatch.setenv("FOO_KEY", "env-value")
        # _fetch_from_gcp short-circuits when _sm_client returns None → returns None
        monkeypatch.setattr(secrets_mod, "_sm_client", lambda: None)
        # Bust the LRU cache on _fetch_from_gcp so the patched _sm_client is consulted
        secrets_mod._fetch_from_gcp.cache_clear()

        out = secrets_mod.get_secret("FOO_KEY")

        assert out == "env-value"


# ──────────────────────────────────────────────────────────────────────────
# LRU cache + clear_cache
# ──────────────────────────────────────────────────────────────────────────

class TestCaching:
    def test_repeat_fetch_hits_cache(self, monkeypatch):
        calls = {"n": 0}

        class FakeSMClient:
            def access_secret_version(self, request):
                calls["n"] += 1
                fake_payload = type("p", (), {"data": b"sm-value"})()
                return type("r", (), {"payload": fake_payload})()

        monkeypatch.setattr(secrets_mod, "_sm_client", lambda: FakeSMClient())
        secrets_mod._fetch_from_gcp.cache_clear()

        # Two calls with same args
        v1 = secrets_mod._fetch_from_gcp("FOO", "proj-1")
        v2 = secrets_mod._fetch_from_gcp("FOO", "proj-1")

        assert v1 == v2 == "sm-value"
        assert calls["n"] == 1  # second call hit cache

    def test_clear_cache_forces_refetch(self, monkeypatch):
        calls = {"n": 0}

        class FakeSMClient:
            def access_secret_version(self, request):
                calls["n"] += 1
                fake_payload = type("p", (), {"data": b"v"})()
                return type("r", (), {"payload": fake_payload})()

        monkeypatch.setattr(secrets_mod, "_sm_client", lambda: FakeSMClient())

        secrets_mod._fetch_from_gcp("FOO", "proj-1")
        # Bust only _fetch_from_gcp's cache directly — secrets_mod.clear_cache()
        # would also call _sm_client.cache_clear(), but the lambda monkeypatch
        # replaced _sm_client with doesn't have cache_clear.
        secrets_mod._fetch_from_gcp.cache_clear()
        secrets_mod._fetch_from_gcp("FOO", "proj-1")

        assert calls["n"] == 2

    def test_fetch_failure_returns_none_and_warns(self, monkeypatch, capsys):
        class BoomClient:
            def access_secret_version(self, request):
                raise PermissionError("403 lacks accessor role")

        monkeypatch.setattr(secrets_mod, "_sm_client", lambda: BoomClient())
        secrets_mod._fetch_from_gcp.cache_clear()

        out = secrets_mod._fetch_from_gcp("FOO", "proj-1")

        assert out is None
        captured = capsys.readouterr()
        assert "failed to fetch" in captured.out


# ──────────────────────────────────────────────────────────────────────────
# is_gcp_enabled
# ──────────────────────────────────────────────────────────────────────────

class TestIsGcpEnabled:
    def test_no_project_returns_false(self, monkeypatch):
        monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
        assert secrets_mod.is_gcp_enabled() is False

    def test_project_with_sdk_returns_true(self, monkeypatch):
        monkeypatch.setenv("GCP_SECRET_PROJECT", "my-proj")
        monkeypatch.setattr(secrets_mod, "_sm_client", lambda: object())
        assert secrets_mod.is_gcp_enabled() is True

    def test_project_without_sdk_returns_false(self, monkeypatch):
        monkeypatch.setenv("GCP_SECRET_PROJECT", "my-proj")
        monkeypatch.setattr(secrets_mod, "_sm_client", lambda: None)
        assert secrets_mod.is_gcp_enabled() is False


# ──────────────────────────────────────────────────────────────────────────
# bridge_to_env
# ──────────────────────────────────────────────────────────────────────────

class TestBridgeToEnv:
    def test_preserves_existing_env(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_API_KEY", "user-set")
        monkeypatch.setattr(secrets_mod, "get_secret",
                            lambda name, default="": "sm-would-be")

        secrets_mod.bridge_to_env("LANGSMITH_API_KEY")

        import os
        assert os.environ["LANGSMITH_API_KEY"] == "user-set"

    def test_fills_empty_env_from_sm(self, monkeypatch):
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        monkeypatch.setattr(secrets_mod, "get_secret",
                            lambda name, default="": "ls__abc")

        secrets_mod.bridge_to_env("LANGSMITH_API_KEY")

        import os
        assert os.environ.get("LANGSMITH_API_KEY") == "ls__abc"

    def test_no_value_no_write(self, monkeypatch):
        monkeypatch.delenv("MAYBE_KEY", raising=False)
        monkeypatch.setattr(secrets_mod, "get_secret",
                            lambda name, default="": "")

        secrets_mod.bridge_to_env("MAYBE_KEY")

        import os
        assert "MAYBE_KEY" not in os.environ


# ──────────────────────────────────────────────────────────────────────────
# bridge_streamlit_secrets_to_env — st.secrets → os.environ 鏡像
# ──────────────────────────────────────────────────────────────────────────

def _fake_streamlit(monkeypatch, secrets_obj):
    """Inject a fake `streamlit` module whose .secrets is `secrets_obj`."""
    import sys
    import types

    fake_st = types.ModuleType("streamlit")
    fake_st.secrets = secrets_obj
    monkeypatch.setitem(sys.modules, "streamlit", fake_st)


class _RaisingSecrets:
    """dict(st.secrets) triggers .keys() — raise the configured exception."""

    def __init__(self, exc):
        self._exc = exc

    def keys(self):
        raise self._exc


class TestBridgeStreamlitSecretsToEnv:
    def test_mirrors_string_secrets_into_empty_env(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_TEST_KEY", raising=False)
        _fake_streamlit(monkeypatch, {"BRIDGE_TEST_KEY": "from-secrets"})

        secrets_mod.bridge_streamlit_secrets_to_env()

        import os
        assert os.environ.get("BRIDGE_TEST_KEY") == "from-secrets"

    def test_does_not_overwrite_existing_env(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TEST_KEY", "from-dotenv")
        _fake_streamlit(monkeypatch, {"BRIDGE_TEST_KEY": "from-secrets"})

        secrets_mod.bridge_streamlit_secrets_to_env()

        import os
        assert os.environ["BRIDGE_TEST_KEY"] == "from-dotenv"

    def test_skips_non_string_values(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_NUM", raising=False)
        monkeypatch.delenv("gcp_service_account", raising=False)
        _fake_streamlit(monkeypatch, {
            "BRIDGE_NUM": 0.5,                       # 未加引號的 TOML 數字
            "gcp_service_account": {"type": "x"},    # TOML 表格
        })

        secrets_mod.bridge_streamlit_secrets_to_env()

        import os
        assert "BRIDGE_NUM" not in os.environ
        assert "gcp_service_account" not in os.environ

    def test_missing_secrets_file_is_silent(self, monkeypatch, capsys):
        """本機無 secrets.toml（FileNotFoundError）→ 無輸出、不丟出。"""
        _fake_streamlit(monkeypatch,
                        _RaisingSecrets(FileNotFoundError("No secrets files found")))

        secrets_mod.bridge_streamlit_secrets_to_env()

        assert capsys.readouterr().out == ""

    def test_parse_error_prints_warning(self, monkeypatch, capsys):
        """secrets.toml 存在但壞掉（如 TOML 語法錯誤）→ 警告但不中斷啟動。"""
        _fake_streamlit(monkeypatch, _RaisingSecrets(ValueError("bad TOML")))

        secrets_mod.bridge_streamlit_secrets_to_env()

        out = capsys.readouterr().out
        assert "st.secrets" in out and "⚠" in out
