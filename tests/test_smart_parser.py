"""
Unit tests for src/ingestion/smart_parser.py.

Pure-function coverage of the pdf-parsing capability spec:
  - _table_to_narrative: page header + key:value pairs + NaN skipping
  - _is_valid_narrative: too-short / numeric-only / Chinese-text classification
  - _get_llama_parser: graceful None when key missing
  - parse_page_with_pdfplumber: text-only and table paths via mocked page objects
  - parse_pdf: full pipeline via mocked pdfplumber.open context manager
"""
import types
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


# ──────────────────────────────────────────────────────────────────────────
# parse_page_with_pdfplumber — via mock pdfplumber page
# ──────────────────────────────────────────────────────────────────────────

def _make_page(page_num: int, text: str = "", tables: list | None = None):
    """Build a minimal mock that satisfies parse_page_with_pdfplumber's API."""
    page = types.SimpleNamespace(
        page_number=page_num,
        extract_text=lambda: text,
        extract_tables=lambda: (tables if tables is not None else []),
    )
    return page


class TestParsePageWithPdfplumber:
    def test_text_only_page_returns_content(self):
        page = _make_page(1, text="本季毛利率達 53%，超越市場預期。公司展望下季持續成長。")
        result = sp.parse_page_with_pdfplumber(page)

        assert result["page"] == 1
        assert result["parse_method"] == "pdfplumber"
        assert "毛利率" in result["content"]
        assert result["has_table"] is False
        assert result["parse_success"] is True

    def test_empty_page_marks_parse_failure(self):
        page = _make_page(2, text="")
        result = sp.parse_page_with_pdfplumber(page)

        assert result["content"] == ""
        assert result["parse_success"] is False

    def test_page_with_valid_table_sets_has_table(self):
        # Provide a 2-row table: header row + one data row
        table = [["季度", "毛利率"], ["2024Q1", "53%"]]
        page = _make_page(3, text="", tables=[table])
        result = sp.parse_page_with_pdfplumber(page)

        assert result["has_table"] is True
        # Narrative content should contain the table data
        assert "季度" in result["content"] or "毛利率" in result["content"]

    def test_table_with_bad_row_marks_parse_failure(self):
        # A table that pandas can't convert (e.g. header None) → parse_success=False
        bad_table = [[None, None], ["a", "b"]]
        page = _make_page(4, text="", tables=[bad_table])
        result = sp.parse_page_with_pdfplumber(page)

        assert result["has_table"] is True
        # parse_success may still be False if narrative is invalid

    def test_numeric_only_text_is_invalid_narrative(self):
        # A page of pure numbers is rejected by _is_valid_narrative
        numeric_text = " ".join(["1.23"] * 30)
        page = _make_page(5, text=numeric_text)
        result = sp.parse_page_with_pdfplumber(page)

        assert result["parse_success"] is False


# ──────────────────────────────────────────────────────────────────────────
# parse_pdf — full pipeline via mocked pdfplumber.open
# ──────────────────────────────────────────────────────────────────────────

class TestParsePdf:
    def _mock_open(self, monkeypatch, pages_text: list[str]):
        """Replace pdfplumber.open with a context manager yielding fake pages."""
        fake_pages = [_make_page(i + 1, text=t) for i, t in enumerate(pages_text)]

        class _FakePDF:
            pages = fake_pages

        class _FakeCtx:
            def __enter__(self):
                return _FakePDF()
            def __exit__(self, *_):
                return False

        monkeypatch.setattr(sp.pdfplumber, "open", lambda _path: _FakeCtx())

    def test_valid_pages_returned(self, monkeypatch, tmp_path):
        texts = [
            "本季毛利率達 53%，超越市場預期，受惠於先進製程良率改善與訂單回升。",
            "展望下季，公司預期需求持續強勁，毛利率有望進一步提升至 54–55% 區間。",
        ]
        self._mock_open(monkeypatch, texts)
        # Patch _get_llama_parser so fallback is disabled
        monkeypatch.setattr(sp, "_get_llama_parser", lambda: None)

        result = sp.parse_pdf(
            tmp_path / "fake.pdf",
            company="台積電", quarter="2024Q1",
            date="2024-01-15", stock_code="2330",
        )

        assert len(result) == 2
        assert result[0]["page"] == 1
        assert result[0]["parse_method"] == "pdfplumber"
        assert result[0]["metadata"]["company"] == "台積電"
        assert result[0]["metadata"]["quarter"] == "2024Q1"

    def test_empty_pages_filtered_out(self, monkeypatch, tmp_path):
        texts = [
            "本季營收創新高，毛利率提升至 53%，受惠於先進製程良率改善。",
            "",  # blank page — must be filtered
        ]
        self._mock_open(monkeypatch, texts)
        monkeypatch.setattr(sp, "_get_llama_parser", lambda: None)

        result = sp.parse_pdf(
            tmp_path / "fake.pdf",
            company="台積電", quarter="2024Q1",
            date="2024-01-15", stock_code="2330",
        )

        # Only the non-empty page should survive
        assert len(result) == 1

    def test_metadata_attached_to_each_page(self, monkeypatch, tmp_path):
        texts = ["公司本季表現穩健，毛利率維持高水準，各業務線均達成季度目標。"]
        self._mock_open(monkeypatch, texts)
        monkeypatch.setattr(sp, "_get_llama_parser", lambda: None)

        result = sp.parse_pdf(
            tmp_path / "fake.pdf",
            company="聯發科", quarter="2024Q2",
            date="2024-04-15", stock_code="2454", lang="M",
        )

        meta = result[0]["metadata"]
        assert meta["company"] == "聯發科"
        assert meta["quarter"] == "2024Q2"
        assert meta["stock_code"] == "2454"
        assert meta["lang"] == "M"
