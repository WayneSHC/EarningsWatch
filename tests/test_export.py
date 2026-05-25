"""
Unit tests for src/ui/export.py.

Covers the export-formats capability spec:
  - CSV UTF-8 BOM prefix
  - _strip_emoji ASCII substitution
  - _resolve_cjk_font cascade + raise when no font available
  - to_csv_single contents: two sections, has_contradiction yes/no mapping
  - to_csv_compare contents: dash for missing values
  - to_pdf_single produces PDF bytes
"""
import csv
import io

import pytest

from src.ui import export as export_mod


# ──────────────────────────────────────────────────────────────────────────
# CSV — BOM, sections, content
# ──────────────────────────────────────────────────────────────────────────

class TestCsvSingle:
    def _result(self):
        return {
            "contradictions": [
                {
                    "quarter_a": "2024Q1",
                    "quarter_b": "2024Q2",
                    "analysis": {
                        "stance_change": "更樂觀",
                        "has_contradiction": True,
                        "change_detail": "毛利率上揚",
                        "evidence_early": "本季毛利率 50%",
                        "evidence_later": "本季毛利率 53%",
                        "follow_up_question": "未來會持續？",
                    },
                },
            ],
            "promises": [
                {
                    "promise_quarter": "2024Q1",
                    "content": "毛利率維持 53%",
                    "followup_quarter": "2024Q2",
                    "status": "✅ 達標",
                    "detail": "達成",
                },
            ],
        }

    def test_starts_with_utf8_bom(self):
        out = export_mod.to_csv_single(self._result(), "台積電", "毛利率")
        assert out[:3] == b"\xef\xbb\xbf"

    def test_two_sections_present(self):
        out = export_mod.to_csv_single(self._result(), "台積電", "毛利率")
        text = out.decode("utf-8-sig")
        assert "# 矛盾偵測結果" in text
        assert "# 承諾追蹤" in text

    def test_has_contradiction_yes_mapped_to_chinese(self):
        out = export_mod.to_csv_single(self._result(), "台積電", "毛利率")
        text = out.decode("utf-8-sig")
        reader = list(csv.reader(io.StringIO(text)))
        # Find the contradiction data row (after header line "季度A,季度B,...")
        data_rows = [r for r in reader if r and r[0] == "2024Q1" and len(r) > 3]
        assert data_rows, "Expected at least one contradiction data row"
        # "有明確矛盾" 欄位 → 是
        assert data_rows[0][3] == "是"

    def test_csv_decodes_chinese_correctly(self):
        out = export_mod.to_csv_single(self._result(), "台積電", "毛利率")
        # Strip BOM, decode UTF-8 → "台積電" should appear
        decoded = out[3:].decode("utf-8")
        assert "台積電" in decoded


class TestCsvCompare:
    def test_dash_for_missing_company_value(self):
        table = [
            {"quarter_pair": "2024Q1 vs 2024Q2", "A": "更樂觀", "B": "—", "C": "更保守"},
        ]
        out = export_mod.to_csv_compare(table, ["A", "B", "C"], "AI")

        text = out.decode("utf-8-sig")
        reader = list(csv.reader(io.StringIO(text)))
        data_rows = [r for r in reader if r and r[0] == "2024Q1 vs 2024Q2"]
        assert data_rows
        assert data_rows[0] == ["2024Q1 vs 2024Q2", "更樂觀", "—", "更保守"]


# ──────────────────────────────────────────────────────────────────────────
# _strip_emoji
# ──────────────────────────────────────────────────────────────────────────

class TestStripEmoji:
    def test_known_emojis_replaced(self):
        out = export_mod._strip_emoji("✅ ok ❌ fail ⚠ warn 🚨 alarm")
        assert "[OK]" in out
        assert "[X]" in out
        assert "[!]" in out
        assert "[!!]" in out

    def test_decorative_emojis_dropped(self):
        out = export_mod._strip_emoji("📄 report 📋 list 📈 chart 📅 date")
        # Decorative emojis become empty string → text remains
        assert "report" in out
        assert "📄" not in out

    def test_no_emoji_unchanged(self):
        assert export_mod._strip_emoji("一般文字") == "一般文字"


# ──────────────────────────────────────────────────────────────────────────
# _resolve_cjk_font — cascade + raise
# ──────────────────────────────────────────────────────────────────────────

class TestResolveCjkFont:
    def test_first_existing_path_wins(self, monkeypatch):
        # Pretend the very first candidate is the only one that exists
        first_path = export_mod._FONT_CANDIDATES[0][1]

        def fake_exists(path):
            return path == first_path

        monkeypatch.setattr(export_mod._os.path, "exists", fake_exists)

        found = export_mod._resolve_cjk_font()
        assert found is not None
        assert found[1] == first_path

    def test_none_when_no_path_exists(self, monkeypatch):
        monkeypatch.setattr(export_mod._os.path, "exists", lambda _p: False)

        assert export_mod._resolve_cjk_font() is None

    def test_new_pdf_raises_with_remediation(self, monkeypatch):
        monkeypatch.setattr(export_mod, "_resolve_cjk_font", lambda: None)

        with pytest.raises(RuntimeError, match="找不到中文字型"):
            export_mod._new_pdf()


# ──────────────────────────────────────────────────────────────────────────
# to_pdf_single — only run if a CJK font is locally available
# ──────────────────────────────────────────────────────────────────────────

def _skip_no_font():
    if export_mod._resolve_cjk_font() is None:
        pytest.skip("No CJK font installed locally; PDF emission cannot be tested.")


class TestPdfSingle:
    def test_emits_valid_pdf_bytes_when_font_available(self):
        """If a CJK font is found on the machine, the export must emit PDF bytes."""
        _skip_no_font()

        result = {
            "contradictions": [],
            "promises": [],
            "confidence": 0.85,
            "final_report": "報告內容",
        }
        out = export_mod.to_pdf_single(result, "台積電", "AI需求")

        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")

    def test_pdf_with_contradictions_and_promises(self):
        """PDF is generated when contradictions and promises are non-empty."""
        _skip_no_font()

        result = {
            "contradictions": [
                {
                    "quarter_a": "2024Q1",
                    "quarter_b": "2024Q2",
                    "analysis": {
                        "stance_change": "更樂觀",
                        "has_contradiction": True,
                        "change_detail": "毛利率上揚",
                        "evidence_early": "本季毛利率 50%",
                        "evidence_later": "本季毛利率 53%",
                        "follow_up_question": "未來趨勢？",
                    },
                },
                {
                    "quarter_a": "2024Q2",
                    "quarter_b": "2024Q3",
                    "analysis": {
                        "stance_change": "無關",
                        "has_contradiction": False,
                        "change_detail": "",
                        "evidence_early": "",
                        "evidence_later": "",
                        "follow_up_question": "",
                    },
                },
            ],
            "promises": [
                {
                    "promise_quarter": "2024Q1",
                    "content": "毛利率維持 53%",
                    "followup_quarter": "2024Q2",
                    "status": "✅ 達標",
                    "detail": "如期達成",
                },
            ],
            "confidence": 0.80,
            "final_report": "詳細分析報告",
        }
        out = export_mod.to_pdf_single(result, "台積電", "毛利率")

        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")

    def test_pdf_with_contradictions_no_promises(self):
        """PDF renders contradiction section even when promises list is empty."""
        _skip_no_font()

        result = {
            "contradictions": [
                {
                    "quarter_a": "2024Q3",
                    "quarter_b": "2024Q4",
                    "analysis": {
                        "stance_change": "更保守",
                        "has_contradiction": True,
                        "change_detail": "展望調降",
                        "evidence_early": "需求強勁",
                        "evidence_later": "庫存調整",
                        "follow_up_question": "",
                    },
                }
            ],
            "promises": [],
            "confidence": 0.70,
            "final_report": "季度分析",
        }
        out = export_mod.to_pdf_single(result, "聯發科", "展望")

        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")


# ──────────────────────────────────────────────────────────────────────────
# to_pdf_compare — multi-company PDF
# ──────────────────────────────────────────────────────────────────────────

class TestPdfCompare:
    def test_emits_valid_pdf_bytes(self):
        """to_pdf_compare produces valid PDF bytes from multi-company results."""
        _skip_no_font()

        multi_results = {
            "台積電": {
                "confidence": 0.85,
                "contradictions": [
                    {
                        "quarter_a": "2024Q1",
                        "quarter_b": "2024Q2",
                        "analysis": {"stance_change": "更樂觀", "has_contradiction": True},
                    }
                ],
                "final_report": "台積電報告",
            },
            "聯發科": {
                "confidence": 0.75,
                "contradictions": [],
                "final_report": "聯發科報告",
            },
        }
        comparison_table = [
            {"quarter_pair": "2024Q1 vs 2024Q2", "台積電": "更樂觀", "聯發科": "維持不變"},
        ]
        out = export_mod.to_pdf_compare(
            multi_results,
            comparison_table,
            "AI 需求前景差異摘要",
            ["台積電", "聯發科"],
            "AI 需求",
        )

        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")

    def test_pdf_compare_skips_error_companies(self):
        """Companies with 'error' key are skipped in the per-company section."""
        _skip_no_font()

        multi_results = {
            "台積電": {"confidence": 0.9, "contradictions": [], "final_report": "台積電"},
            "聯發科": {"error": "agent failed", "contradictions": [], "final_report": ""},
        }
        out = export_mod.to_pdf_compare(
            multi_results,
            [],
            "",
            ["台積電", "聯發科"],
            "毛利率",
        )

        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")

    def test_pdf_compare_with_colored_table_rows(self):
        """Comparison table rows with 更樂觀/更保守 trigger color fill without crashing."""
        _skip_no_font()

        multi_results = {
            "A": {"confidence": 0.8, "contradictions": [], "final_report": "A"},
            "B": {"confidence": 0.7, "contradictions": [], "final_report": "B"},
        }
        comparison_table = [
            {"quarter_pair": "2024Q1 vs 2024Q2", "A": "更樂觀", "B": "更保守"},
            {"quarter_pair": "2024Q2 vs 2024Q3", "A": "維持不變", "B": "維持不變"},
        ]
        out = export_mod.to_pdf_compare(
            multi_results,
            comparison_table,
            "差異摘要文字",
            ["A", "B"],
            "展望",
        )

        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")
