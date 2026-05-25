"""
Unit tests for src/ui/auth.py.

Covers the app-auth capability spec:
  - No APP_PASSWORD → bypass auth (no stop)
  - Already authenticated session → bypass (no stop)
  - Active lockout → error + st.stop()
  - Expired lockout → show input (not lockout path)
  - Correct password → _authenticated=True, fail counts cleared, st.rerun()
  - Wrong password (not 5th) → fail count incremented, error shown
  - 5th wrong password → lockout timestamp set, lockout error shown
  - Successful login after prior failures → fail count cleared
  - hmac.compare_digest used (timing-safe, not ==)
  - IP-based lockout persists across new sessions (cross-session test)
"""

from __future__ import annotations

import time

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────

class _Stop(Exception):
    """Raised by our fake st.stop() so tests can assert it was called."""


class _Calls:
    def __init__(self) -> None:
        self.stop_count = 0
        self.rerun_count = 0
        self.error_msgs: list[str] = []
        self.text_input_count = 0
        self.button_count = 0


def _patch_st(monkeypatch, *, password_input: str = "", button_clicked: bool = False) -> _Calls:
    """Patch Streamlit UI primitives used by require_password()."""
    import streamlit as st

    calls = _Calls()

    def fake_stop() -> None:
        calls.stop_count += 1
        raise _Stop()

    def fake_rerun() -> None:
        calls.rerun_count += 1

    def fake_text_input(*_a, **_k) -> str:
        calls.text_input_count += 1
        return password_input

    def fake_button(*_a, **_k) -> bool:
        calls.button_count += 1
        return button_clicked

    def fake_error(msg: object) -> None:
        calls.error_msgs.append(str(msg))

    monkeypatch.setattr(st, "stop", fake_stop)
    monkeypatch.setattr(st, "rerun", fake_rerun)
    monkeypatch.setattr(st, "text_input", fake_text_input)
    monkeypatch.setattr(st, "button", fake_button)
    monkeypatch.setattr(st, "error", fake_error)
    monkeypatch.setattr(st, "title", lambda *_a, **_k: None)

    return calls


@pytest.fixture(autouse=True)
def _clean_session():
    """Clear auth-related session keys before and after each test."""
    import streamlit as st
    _AUTH_KEYS = ("_authenticated", "_pwd_fail_count", "_pwd_lockout_until")
    for k in _AUTH_KEYS:
        st.session_state.pop(k, None)
    yield
    for k in _AUTH_KEYS:
        st.session_state.pop(k, None)


# ── Bypass paths (no st.stop() expected) ─────────────────────────────────

class TestBypass:
    def test_no_password_set_skips_auth(self, monkeypatch):
        """Spec: 未設定 APP_PASSWORD SHALL 跳過驗證."""
        import streamlit as st
        calls = _patch_st(monkeypatch)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "")

        from src.ui import auth
        auth.require_password()  # must return normally, no _Stop raised

        assert calls.stop_count == 0
        assert calls.text_input_count == 0

    def test_already_authenticated_skips_auth(self, monkeypatch):
        """Spec: 已驗證的 session SHALL 不再要求密碼."""
        import streamlit as st
        calls = _patch_st(monkeypatch)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "secret123")
        st.session_state["_authenticated"] = True

        from src.ui import auth
        auth.require_password()  # must return normally

        assert calls.stop_count == 0
        assert calls.text_input_count == 0


# ── Lockout paths ─────────────────────────────────────────────────────────

class TestLockout:
    def test_active_lockout_shows_error_and_stops(self, monkeypatch):
        """Spec: fail ≥ 5 且鎖定期未過 → 顯示倒數 + st.stop()."""
        import streamlit as st
        calls = _patch_st(monkeypatch)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "secret123")
        st.session_state["_pwd_fail_count"] = 5
        st.session_state["_pwd_lockout_until"] = time.time() + 200

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        assert calls.stop_count == 1
        assert any("秒後再試" in m for m in calls.error_msgs), calls.error_msgs

    def test_active_lockout_does_not_show_text_input(self, monkeypatch):
        """Lockout path MUST NOT render the password input."""
        import streamlit as st
        calls = _patch_st(monkeypatch)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "secret123")
        st.session_state["_pwd_fail_count"] = 5
        st.session_state["_pwd_lockout_until"] = time.time() + 100

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        assert calls.text_input_count == 0

    def test_expired_lockout_shows_input_not_lockout_message(self, monkeypatch):
        """Spec: 鎖定期過後 SHALL 允許再次嘗試."""
        import streamlit as st
        calls = _patch_st(monkeypatch, button_clicked=False)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "secret123")
        st.session_state["_pwd_fail_count"] = 5
        st.session_state["_pwd_lockout_until"] = time.time() - 1  # already expired

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        # Must show text input, not lockout error message
        assert calls.text_input_count >= 1
        assert not any("秒後再試" in m for m in calls.error_msgs)


# ── Password-form paths ───────────────────────────────────────────────────

class TestPasswordForm:
    def test_correct_password_authenticates_session(self, monkeypatch):
        """Spec: 密碼正確 → session _authenticated=True, st.rerun()."""
        import streamlit as st
        calls = _patch_st(monkeypatch, password_input="correct", button_clicked=True)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "correct")

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        assert st.session_state.get("_authenticated") is True
        assert calls.rerun_count == 1

    def test_correct_password_clears_fail_counts(self, monkeypatch):
        """Spec: 成功登入清除失敗計數."""
        import streamlit as st
        st.session_state["_pwd_fail_count"] = 2
        calls = _patch_st(monkeypatch, password_input="correct", button_clicked=True)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "correct")

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        assert "_pwd_fail_count" not in st.session_state
        assert "_pwd_lockout_until" not in st.session_state

    def test_wrong_password_increments_fail_count(self, monkeypatch):
        """Spec: 密碼錯誤 → _pwd_fail_count 遞增."""
        import streamlit as st
        calls = _patch_st(monkeypatch, password_input="wrong", button_clicked=True)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "correct")

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        assert st.session_state.get("_pwd_fail_count") == 1
        assert st.session_state.get("_authenticated") is None

    def test_fifth_failure_triggers_lockout(self, monkeypatch):
        """Spec: 第 5 次失敗觸發鎖定."""
        import streamlit as st
        st.session_state["_pwd_fail_count"] = 4
        calls = _patch_st(monkeypatch, password_input="wrong", button_clicked=True)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "correct")

        from src.ui import auth
        before = time.time()
        with pytest.raises(_Stop):
            auth.require_password()

        assert st.session_state.get("_pwd_fail_count") == 5
        lockout = st.session_state.get("_pwd_lockout_until", 0.0)
        # Lockout must be set ~300s in the future
        assert lockout >= before + 290
        assert any("鎖定" in m for m in calls.error_msgs), calls.error_msgs

    def test_non_fifth_failure_shows_remaining_attempts(self, monkeypatch):
        """Wrong password before reaching limit shows remaining attempts."""
        import streamlit as st
        calls = _patch_st(monkeypatch, password_input="wrong", button_clicked=True)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "correct")

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        assert any("機會" in m for m in calls.error_msgs), calls.error_msgs
        # No lockout yet
        assert "_pwd_lockout_until" not in st.session_state

    def test_no_button_click_does_not_check_password(self, monkeypatch):
        """Without button click the form is shown but no comparison happens."""
        import streamlit as st
        calls = _patch_st(monkeypatch, password_input="wrong", button_clicked=False)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "correct")

        from src.ui import auth
        with pytest.raises(_Stop):
            auth.require_password()

        # No fail count incremented
        assert st.session_state.get("_pwd_fail_count", 0) == 0
        assert st.session_state.get("_authenticated") is None


# ── Security: timing-safe comparison ─────────────────────────────────────

class TestTimingSafe:
    def test_compare_digest_used_not_equality(self):
        """Spec: MUST use hmac.compare_digest, not == or is."""
        import inspect
        from src.ui import auth
        source = inspect.getsource(auth.require_password)
        assert "compare_digest" in source
        # Ensure naive == is NOT used for the password comparison
        # (The source may contain == for other comparisons, but not for pwd)
        assert "pwd ==" not in source
        assert "pwd is " not in source


# ── IP-based lockout: cross-session persistence ───────────────────────────

class TestIpBasedLockout:
    def setup_method(self):
        from src.core import rate_limiter
        rate_limiter.auth_reset()  # ensure clean IP state before each test

    def teardown_method(self):
        from src.core import rate_limiter
        rate_limiter.auth_reset()

    def test_ip_lockout_persists_to_new_session(self, monkeypatch):
        """Lockout set via IP-based store is visible even after session state is cleared."""
        import streamlit as st
        from src.core import rate_limiter

        TEST_IP = "203.0.113.42"
        monkeypatch.setattr("src.ui.auth.get_client_ip", lambda: TEST_IP)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "secret")

        calls = _patch_st(monkeypatch, password_input="wrong", button_clicked=True)

        from src.ui import auth

        # Exhaust all 5 attempts via IP store
        for _ in range(_MAX_ATTEMPTS - 1):
            try:
                auth.require_password()
            except _Stop:
                pass

        # 5th attempt triggers lockout
        try:
            auth.require_password()
        except _Stop:
            pass

        assert rate_limiter.auth_fail_count(TEST_IP) == _MAX_ATTEMPTS
        assert rate_limiter.auth_lockout_until(TEST_IP) > time.time()

        # Simulate new session: clear session state but keep IP store intact
        for k in ("_authenticated", "_pwd_fail_count", "_pwd_lockout_until"):
            st.session_state.pop(k, None)

        # New session with same IP should still be locked out
        calls2 = _patch_st(monkeypatch, password_input="wrong", button_clicked=False)
        with pytest.raises(_Stop):
            auth.require_password()

        assert calls2.stop_count == 1
        assert any("秒後再試" in m for m in calls2.error_msgs)

    def test_successful_login_clears_ip_store(self, monkeypatch):
        """Correct password clears the IP fail counter, not just session state."""
        from src.core import rate_limiter

        TEST_IP = "203.0.113.99"
        monkeypatch.setattr("src.ui.auth.get_client_ip", lambda: TEST_IP)
        monkeypatch.setattr("src.ui.auth.get_secret", lambda _: "correct")

        # Pre-seed 2 failures in IP store
        rate_limiter.auth_record_fail(TEST_IP, _MAX_ATTEMPTS, 300)
        rate_limiter.auth_record_fail(TEST_IP, _MAX_ATTEMPTS, 300)
        assert rate_limiter.auth_fail_count(TEST_IP) == 2

        _patch_st(monkeypatch, password_input="correct", button_clicked=True)
        from src.ui import auth
        try:
            auth.require_password()
        except _Stop:
            pass

        assert rate_limiter.auth_fail_count(TEST_IP) == 0


# expose _MAX_ATTEMPTS so the cross-session test can use it
from src.ui.auth import _MAX_ATTEMPTS
