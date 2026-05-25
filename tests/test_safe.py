"""
Unit tests for src/core/safe.py.

Covers safe_int_env, safe_float_env, and log_exc — including the
fallback/warning branches that require invalid env var values.
"""
from __future__ import annotations

import pytest

from src.core.safe import safe_int_env, safe_float_env, log_exc


class TestSafeIntEnv:
    def test_missing_var_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_INT_VAR", raising=False)
        assert safe_int_env("TEST_INT_VAR", 42) == 42

    def test_empty_var_returns_default(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_VAR", "")
        assert safe_int_env("TEST_INT_VAR", 7) == 7

    def test_valid_integer_parsed(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_VAR", "99")
        assert safe_int_env("TEST_INT_VAR", 0) == 99

    def test_invalid_value_returns_default_and_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_INT_VAR", "not_an_int")
        result = safe_int_env("TEST_INT_VAR", 5, prefix="Test")
        assert result == 5
        out = capsys.readouterr().out
        assert "[Test]" in out
        assert "TEST_INT_VAR" in out
        assert "5" in out

    def test_float_string_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_INT_VAR", "3.14")
        result = safe_int_env("TEST_INT_VAR", 10)
        assert result == 10


class TestSafeFloatEnv:
    def test_missing_var_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_FLOAT_VAR", raising=False)
        assert safe_float_env("TEST_FLOAT_VAR", 0.5) == 0.5

    def test_empty_var_returns_default(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT_VAR", "")
        assert safe_float_env("TEST_FLOAT_VAR", 1.0) == 1.0

    def test_valid_float_parsed(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT_VAR", "0.25")
        assert safe_float_env("TEST_FLOAT_VAR", 0.0) == pytest.approx(0.25)

    def test_integer_string_parsed_as_float(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT_VAR", "3")
        assert safe_float_env("TEST_FLOAT_VAR", 0.0) == pytest.approx(3.0)

    def test_invalid_value_returns_default_and_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_FLOAT_VAR", "abc")
        result = safe_float_env("TEST_FLOAT_VAR", 2.5, prefix="FloatTest")
        assert result == pytest.approx(2.5)
        out = capsys.readouterr().out
        assert "[FloatTest]" in out
        assert "TEST_FLOAT_VAR" in out


class TestLogExc:
    def test_prints_prefix_and_location(self, capsys):
        exc = ValueError("something went wrong")
        log_exc("MyModule", "my_operation", exc)
        out = capsys.readouterr().out
        assert "[MyModule]" in out
        assert "my_operation" in out
        assert "ValueError" in out

    def test_truncates_long_message(self, capsys):
        long_msg = "x" * 200
        exc = RuntimeError(long_msg)
        log_exc("M", "op", exc, max_len=50)
        out = capsys.readouterr().out
        # Should only contain 50 x's, not 200
        assert "x" * 51 not in out
        assert "x" * 50 in out

    def test_default_truncation_is_120(self, capsys):
        long_msg = "a" * 200
        exc = Exception(long_msg)
        log_exc("M", "op", exc)
        out = capsys.readouterr().out
        assert "a" * 121 not in out
        assert "a" * 120 in out
