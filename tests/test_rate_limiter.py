"""
Unit tests for src/core/rate_limiter.py.

Coverage:
  - check / record cycle for single IP
  - independence between IPs
  - thread-safe concurrent access
  - empty IP is a no-op (allows graceful degrade)
  - get_client_ip header parsing (X-Forwarded-For, X-Real-IP)
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.core import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean_state():
    rl.reset()
    yield
    rl.reset()


# ──────────────────────────────────────────────────────────────────────────
# check / record core flow
# ──────────────────────────────────────────────────────────────────────────

class TestRateLimiter:
    def test_fresh_ip_no_cooldown(self):
        assert rl.check("1.2.3.4") == 0.0

    def test_record_then_check_returns_cooldown(self):
        rl.record("1.2.3.4")
        remaining = rl.check("1.2.3.4")
        assert 9.0 < remaining <= 10.0  # roughly 10s minus tiny elapsed

    def test_independent_ips(self):
        rl.record("1.2.3.4")
        # Different IP should be unaffected
        assert rl.check("5.6.7.8") == 0.0

    def test_empty_ip_is_noop(self):
        # Both record and check on empty string must not crash, must allow
        rl.record("")
        assert rl.check("") == 0.0

    def test_reset_specific_ip(self):
        rl.record("1.2.3.4")
        rl.record("5.6.7.8")
        rl.reset("1.2.3.4")
        assert rl.check("1.2.3.4") == 0.0
        # Other IP still locked
        assert rl.check("5.6.7.8") > 0

    def test_reset_all(self):
        rl.record("1.2.3.4")
        rl.record("5.6.7.8")
        rl.reset()
        assert rl.check("1.2.3.4") == 0.0
        assert rl.check("5.6.7.8") == 0.0


# ──────────────────────────────────────────────────────────────────────────
# Thread safety
# ──────────────────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_record_no_loss(self):
        """100 threads × 10 records should produce coherent state (no crashes)."""
        def worker(idx):
            for i in range(10):
                rl.record(f"ip-{idx}-{i}")

        with ThreadPoolExecutor(max_workers=100) as pool:
            list(pool.map(worker, range(100)))

        # Pick a few sample IPs and verify they're tracked
        assert rl.check("ip-0-0") > 0
        assert rl.check("ip-99-9") > 0


# ──────────────────────────────────────────────────────────────────────────
# get_client_ip — header parsing
# ──────────────────────────────────────────────────────────────────────────

class TestGetClientIP:
    def test_no_streamlit_context_returns_empty(self, monkeypatch):
        # Force the streamlit import to fail or return no headers
        import streamlit as st

        class _NoCtx:
            pass
        monkeypatch.setattr(st, "context", _NoCtx(), raising=False)
        # Without a `headers` attribute, our code falls through to ""
        assert rl.get_client_ip() == ""

    def test_x_forwarded_for_first_value(self, monkeypatch):
        import streamlit as st

        class _Ctx:
            headers = {"X-Forwarded-For": "203.0.113.5, 10.0.0.1, 172.16.0.1"}
        monkeypatch.setattr(st, "context", _Ctx(), raising=False)
        assert rl.get_client_ip() == "203.0.113.5"

    def test_x_real_ip_fallback(self, monkeypatch):
        import streamlit as st

        class _Ctx:
            headers = {"X-Real-IP": "198.51.100.7"}
        monkeypatch.setattr(st, "context", _Ctx(), raising=False)
        assert rl.get_client_ip() == "198.51.100.7"

    def test_xff_takes_precedence_over_real_ip(self, monkeypatch):
        import streamlit as st

        class _Ctx:
            headers = {
                "X-Forwarded-For": "203.0.113.5",
                "X-Real-IP": "198.51.100.7",
            }
        monkeypatch.setattr(st, "context", _Ctx(), raising=False)
        assert rl.get_client_ip() == "203.0.113.5"

    def test_lowercase_header_keys(self, monkeypatch):
        import streamlit as st

        class _Ctx:
            headers = {"x-forwarded-for": "203.0.113.99"}
        monkeypatch.setattr(st, "context", _Ctx(), raising=False)
        assert rl.get_client_ip() == "203.0.113.99"

    def test_empty_xff_falls_through(self, monkeypatch):
        import streamlit as st

        class _Ctx:
            headers = {"X-Forwarded-For": "  ", "X-Real-IP": "198.51.100.7"}
        monkeypatch.setattr(st, "context", _Ctx(), raising=False)
        assert rl.get_client_ip() == "198.51.100.7"
