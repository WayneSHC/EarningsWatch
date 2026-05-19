"""
Unit tests for scripts/run_ingestion.py.

Covers the pdf-ingestion-pipeline capability spec:
  - _date_to_quarter month → fiscal quarter mapping
  - parse_filename all 5 formats (A: MOPS, B: TSMC, C: 2454, D: Hon Hai, E: Delta)
  - Unrecognised filename → None
  - Unknown stock code → None
  - _save_log atomic write + .tmp cleanup
  - _load_log graceful on missing / corrupted file
"""
import importlib
import json
import sys
from pathlib import Path

import pytest


# Lazy import so we can monkeypatch _PROJECT_ROOT-derived paths if needed
@pytest.fixture
def ingest_mod():
    """Import scripts/run_ingestion.py as a module.

    Derives the path from this test file's location so it works on any
    machine / CI runner — never hardcode absolute paths.
    """
    project_root = Path(__file__).resolve().parent.parent
    scripts_dir = project_root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    if "run_ingestion" in sys.modules:
        return importlib.reload(sys.modules["run_ingestion"])
    return importlib.import_module("run_ingestion")


# ──────────────────────────────────────────────────────────────────────────
# _date_to_quarter
# ──────────────────────────────────────────────────────────────────────────

class TestDateToQuarter:
    def test_jan_reports_prev_year_q4(self, ingest_mod):
        assert ingest_mod._date_to_quarter(2026, 1) == "2025Q4"

    def test_march_reports_prev_year_q4(self, ingest_mod):
        assert ingest_mod._date_to_quarter(2024, 3) == "2023Q4"

    def test_april_reports_current_q1(self, ingest_mod):
        assert ingest_mod._date_to_quarter(2024, 4) == "2024Q1"

    def test_june_reports_current_q1(self, ingest_mod):
        assert ingest_mod._date_to_quarter(2024, 6) == "2024Q1"

    def test_july_reports_current_q2(self, ingest_mod):
        assert ingest_mod._date_to_quarter(2024, 7) == "2024Q2"

    def test_october_reports_current_q3(self, ingest_mod):
        assert ingest_mod._date_to_quarter(2024, 10) == "2024Q3"

    def test_december_reports_current_q3(self, ingest_mod):
        assert ingest_mod._date_to_quarter(2024, 12) == "2024Q3"


# ──────────────────────────────────────────────────────────────────────────
# parse_filename — Format A (MOPS)
# ──────────────────────────────────────────────────────────────────────────

class TestParseFilenameFormatA:
    def test_tsmc_mops(self, ingest_mod):
        out = ingest_mod.parse_filename("233020260115M001.pdf")
        assert out is not None
        assert out["company"] == "台積電"
        assert out["stock_code"] == "2330"
        assert out["quarter"] == "2025Q4"
        assert out["date"] == "2026-01-15"
        assert out["lang"] == "M"
        assert out["source_file"] == "233020260115M001.pdf"

    def test_unknown_stock_returns_none(self, ingest_mod):
        # 9999 not in STOCK_CODE_TO_COMPANY
        assert ingest_mod.parse_filename("999920240115M001.pdf") is None

    def test_case_insensitive(self, ingest_mod):
        out = ingest_mod.parse_filename("233020240115m001.PDF")
        assert out is not None
        assert out["lang"] == "M"  # uppercased


# ──────────────────────────────────────────────────────────────────────────
# parse_filename — Format B (TSMC Transcript)
# ──────────────────────────────────────────────────────────────────────────

class TestParseFilenameFormatB:
    def test_standard_transcript(self, ingest_mod):
        out = ingest_mod.parse_filename("TSMC 4Q25 Transcript.pdf")
        assert out is not None
        assert out["company"] == "台積電"
        assert out["quarter"] == "2025Q4"
        # Q4 calls happen in Jan of next year (近似)
        assert out["date"] == "2026-01-01"
        assert out["lang"] == "E"
        assert out["part"] == "1"

    def test_suffix_extracted_as_part(self, ingest_mod):
        out = ingest_mod.parse_filename("TSMC 4Q25 Transcript-2.pdf")
        assert out is not None
        assert out["part"] == "2"

    def test_q1_call_month_is_april(self, ingest_mod):
        out = ingest_mod.parse_filename("TSMC 1Q24 Transcript.pdf")
        assert out is not None
        assert out["quarter"] == "2024Q1"
        assert out["date"] == "2024-04-01"


# ──────────────────────────────────────────────────────────────────────────
# parse_filename — Format C (2454_MediaTek)
# ──────────────────────────────────────────────────────────────────────────

class TestParseFilenameFormatC:
    def test_mediatek_earnings_call(self, ingest_mod):
        out = ingest_mod.parse_filename("2454_1Q24_Earnings_Call_Transcript.pdf")
        assert out is not None
        assert out["company"] == "聯發科"
        assert out["stock_code"] == "2454"
        assert out["quarter"] == "2024Q1"
        assert out["lang"] == "E"
        # Underscores in doctype replaced with spaces
        assert out["doc_subtype"] == "Earnings Call Transcript"

    def test_unknown_stock_returns_none(self, ingest_mod):
        out = ingest_mod.parse_filename("9999_1Q24_Earnings.pdf")
        assert out is None


# ──────────────────────────────────────────────────────────────────────────
# parse_filename — Format D (Hon Hai)
# ──────────────────────────────────────────────────────────────────────────

class TestParseFilenameFormatD:
    def test_embedded_date_used(self, ingest_mod):
        out = ingest_mod.parse_filename("Hon Hai 1Q25 Results_Chinese_20250514_5223.pdf")
        assert out is not None
        assert out["company"] == "鴻海"
        assert out["stock_code"] == "2317"
        assert out["quarter"] == "2025Q1"
        # Embedded date wins over approximation
        assert out["date"] == "2025-05-14"
        assert out["lang"] == "M"  # has "Chinese"

    def test_no_embedded_date_uses_approximation(self, ingest_mod):
        out = ingest_mod.parse_filename("Hon_Hai_1Q22_Results_Chinese.pdf")
        assert out is not None
        assert out["quarter"] == "2022Q1"
        # Q1 approximation for Hon Hai: May
        assert out["date"] == "2022-05-01"
        assert out["lang"] == "M"

    def test_english_when_no_chinese_marker(self, ingest_mod):
        out = ingest_mod.parse_filename("Hon_Hai_4Q23_Results_Transcript.pdf")
        assert out is not None
        assert out["lang"] == "E"


# ──────────────────────────────────────────────────────────────────────────
# parse_filename — Format E (Delta Analyst Meeting)
# ──────────────────────────────────────────────────────────────────────────

class TestParseFilenameFormatE:
    def test_with_2308_parent_dir(self, ingest_mod):
        out = ingest_mod.parse_filename("1Q22_Analyst Meeting.pdf",
                                        parent_dir="2308_Delta")
        assert out is not None
        assert out["company"] == "台達電"
        assert out["stock_code"] == "2308"
        assert out["quarter"] == "2022Q1"
        # Q1 approximation for Delta: May
        assert out["date"] == "2022-05-01"
        assert out["lang"] == "M"

    def test_unknown_parent_returns_none(self, ingest_mod):
        out = ingest_mod.parse_filename("1Q22_Analyst Meeting.pdf",
                                        parent_dir="9999_Unknown")
        assert out is None


# ──────────────────────────────────────────────────────────────────────────
# parse_filename — unrecognised
# ──────────────────────────────────────────────────────────────────────────

class TestParseFilenameUnrecognised:
    def test_random_name_returns_none(self, ingest_mod):
        assert ingest_mod.parse_filename("random_name.pdf") is None

    def test_no_extension_returns_none(self, ingest_mod):
        assert ingest_mod.parse_filename("233020240115M001") is None


# ──────────────────────────────────────────────────────────────────────────
# _save_log + _load_log
# ──────────────────────────────────────────────────────────────────────────

class TestIngestionLog:
    def test_load_missing_file_returns_empty(self, ingest_mod, monkeypatch, tmp_path):
        monkeypatch.setattr(ingest_mod, "INGESTION_LOG", tmp_path / "no_such.json")
        monkeypatch.setattr(ingest_mod, "PROCESSED_DIR", tmp_path)
        assert ingest_mod._load_log() == {}

    def test_load_corrupted_file_returns_empty(self, ingest_mod, monkeypatch,
                                                tmp_path, capsys):
        bad = tmp_path / "ingestion_log.json"
        bad.write_text("{ corrupt json", encoding="utf-8")
        monkeypatch.setattr(ingest_mod, "INGESTION_LOG", bad)
        monkeypatch.setattr(ingest_mod, "PROCESSED_DIR", tmp_path)

        out = ingest_mod._load_log()
        assert out == {}
        captured = capsys.readouterr()
        assert "無法讀取匯入日誌" in captured.out

    def test_save_log_atomic_no_temp_leftover(self, ingest_mod, monkeypatch, tmp_path):
        log_path = tmp_path / "ingestion_log.json"
        monkeypatch.setattr(ingest_mod, "INGESTION_LOG", log_path)
        monkeypatch.setattr(ingest_mod, "PROCESSED_DIR", tmp_path)

        ingest_mod._save_log({"a.pdf": {"status": "ok"}})

        # Final file present, no .tmp residue
        assert log_path.exists()
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

        # Round-trip JSON
        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert data == {"a.pdf": {"status": "ok"}}

    def test_save_log_auto_creates_processed_dir(self, ingest_mod, monkeypatch, tmp_path):
        nested = tmp_path / "subdir"
        monkeypatch.setattr(ingest_mod, "PROCESSED_DIR", nested)
        monkeypatch.setattr(ingest_mod, "INGESTION_LOG", nested / "ingestion_log.json")

        assert not nested.exists()
        ingest_mod._save_log({"a.pdf": {"status": "ok"}})
        assert nested.exists()
