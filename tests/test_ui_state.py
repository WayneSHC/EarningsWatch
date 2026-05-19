"""
Unit tests for src/ui/state.py.

Covers the ui-session-state capability spec:
  - UIState defaults
  - Independent list defaults per instance
  - UIState.get() uses setdefault → singleton per session
  - Mutation persists across get()
  - typo field raises AttributeError
"""
import pytest

from src.ui.state import UIState


# ──────────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────────

class TestDefaults:
    def test_constructs_without_args(self):
        s = UIState()
        assert s.last_run_time == 0.0
        assert s.mode is None
        assert s.result is None
        assert s.meta is None
        assert s.multi_results is None
        assert s.multi_companies == []
        assert s.multi_topic == ""
        assert s.multi_quarters == []
        assert s.multi_custom_query == ""
        assert s.pdf_cache_key is None
        assert s.single_pdf_bytes is None
        assert s.multi_pdf_bytes is None

    def test_list_defaults_are_independent_per_instance(self):
        a = UIState()
        b = UIState()
        a.multi_companies.append("x")
        assert b.multi_companies == []


# ──────────────────────────────────────────────────────────────────────────
# UIState.get() — session singleton
# ──────────────────────────────────────────────────────────────────────────

class _FakeSessionState(dict):
    """Mimics st.session_state behaviour for setdefault()."""


class TestSessionGet:
    def test_returns_same_instance_on_repeat_calls(self, monkeypatch):
        fake_session = _FakeSessionState()

        import sys
        fake_st = type(sys)("streamlit")
        fake_st.session_state = fake_session
        monkeypatch.setitem(sys.modules, "streamlit", fake_st)

        s1 = UIState.get()
        s2 = UIState.get()
        assert s1 is s2

    def test_mutation_persists_across_get_calls(self, monkeypatch):
        fake_session = _FakeSessionState()

        import sys
        fake_st = type(sys)("streamlit")
        fake_st.session_state = fake_session
        monkeypatch.setitem(sys.modules, "streamlit", fake_st)

        s1 = UIState.get()
        s1.multi_topic = "AI"

        s2 = UIState.get()
        assert s2.multi_topic == "AI"

    def test_fresh_session_state_yields_fresh_instance(self, monkeypatch):
        """Two distinct session_state dicts get two distinct UIState singletons."""
        import sys

        # First session
        fake_a = _FakeSessionState()
        fake_st_a = type(sys)("streamlit")
        fake_st_a.session_state = fake_a
        monkeypatch.setitem(sys.modules, "streamlit", fake_st_a)
        s_a = UIState.get()

        # Replace with brand-new session state
        fake_b = _FakeSessionState()
        fake_st_b = type(sys)("streamlit")
        fake_st_b.session_state = fake_b
        monkeypatch.setitem(sys.modules, "streamlit", fake_st_b)
        s_b = UIState.get()

        assert s_a is not s_b


# ──────────────────────────────────────────────────────────────────────────
# Typo protection
# ──────────────────────────────────────────────────────────────────────────

class TestTypoProtection:
    def test_undeclared_attribute_raises(self):
        s = UIState()
        with pytest.raises(AttributeError):
            _ = s.some_typo_field
