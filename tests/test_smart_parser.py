"""
Unit tests for src/ingestion/smart_parser.py.

Pure-function coverage of the pdf-parsing capability spec:
  - _table_to_narrative: page header + key:value pairs + NaN skipping
  - _is_valid_narrative: too-short / numeric-only / Chinese-text classification
  - _get_llama_parser: graceful None when key missing
"""
import pandas as pd
import pytest

from src.ingestion import smart_parser as sp


# ──────────────────────────────────────────────────────────────────────────
# _table_to_narrative
# ──────────────────────────────────────────────────────────────────────────

class TestTableToNarrative:
    def test_page_header_present(self):
        df = pd.DataFrame({"季度": ["2024Q1"], "毛利率": ["53%"]})
        out = sp._table_to_narrative(df, 7)
        assert out.splitlines()[0] == "（第7頁財務表格）"

    def test_key_value_format(self):
        df = pd.DataFrame({"季度": ["2024Q1"], "毛利率": ["53%"]})
        out = sp._table_to_narrative(df, 5)
        # Cells joined by full-width comma
        assert "季度：2024Q1" in out
        assert "毛利率：53%" in out
        assert "、" in out  # full-width separator

    def test_nan_cells_skipped(self):
        df = pd.DataFrame({
            "季度": ["2024Q1"],
            "營收": [float("nan")],
            "毛利率": ["53%"],
        })
        out = sp._table_to_narrative(df, 5)
        # 營收 NaN must not appear
        assert "營收" not in out
        # Other cells survive
        assert "毛利率：53%" in out


# ──────────────────────────────────────────────────────────────────────────
# _is_valid_narrative
# ──────────────────────────────────────────────────────────────────────────

class TestIsValidNarrative:
    def test_empty_invalid(self):
        assert sp._is_valid_narrative("") is False

    def test_too_short_invalid(self):
        assert sp._is_valid_narrative("hi") is False

    def test_pure_numbers_invalid(self):
        # No Chinese + > 80% digits/punctuation → invalid
        numeric_table = "12.3 45.6 7.8 9.0 1.2 3.4 5.6 7.8 9.0 1.2 3.4 99.9"
        assert sp._is_valid_narrative(numeric_table) is False

    def test_chinese_text_valid(self):
        # Must clear 20-char floor — the previous shorter sample (19 chars) failed.
        assert sp._is_valid_narrative(
            "本季營收創新高，毛利率提升至 53%，受惠於先進製程良率改善。"
        ) is True


# ──────────────────────────────────────────────────────────────────────────
# _get_llama_parser
# ──────────────────────────────────────────────────────────────────────────

class TestGetLlamaParser:
    def test_returns_none_without_key(self, monkeypatch):
        monkeypatch.setattr(sp, "get_secret", lambda _name: "")
        assert sp._get_llama_parser() is None

    def test_returns_none_when_package_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(sp, "get_secret", lambda _name: "valid-key")

        import builtins
        real_import = builtins.__import__

        def fail_llama_parse(name, *args, **kwargs):
            if name == "llama_parse":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_llama_parse)

        assert sp._get_llama_parser() is None
        captured = capsys.readouterr()
        assert "llama-parse 未安裝" in captured.out
