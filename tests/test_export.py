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

class TestPdfSingle:
    def test_emits_valid_pdf_bytes_when_font_available(self):
        """If a CJK font is found on the machine, the export must emit PDF bytes."""
        if export_mod._resolve_cjk_font() is None:
            pytest.skip("No CJK font installed locally; PDF emission cannot be tested.")

        result = {
            "contradictions": [],
            "promises": [],
            "confidence": 0.85,
            "final_report": "報告內容",
        }
        out = export_mod.to_pdf_single(result, "台積電", "AI需求")

        assert isinstance(out, bytes)
        assert out.startswith(b"%PDF-")
