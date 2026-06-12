"""
Unit tests for src/ui/insights.py — 決策導向摘要的純函數層。

Covers:
  - summarize_trajectory: 連續轉向 streak、穩定、無資料、無關季度跳過
  - promise_stats: 達標/未兌現/不明 計數與兌現率（不明不入分母）
  - materiality 排序: 財測/毛利 > 一般敘述；同類依 confidence
  - top_follow_ups: 去重、依重要性排序、上限 n
  - latest_quarter / quarter_end_date / index_prices
"""
import pytest

from src.ui import insights as ins


def _entry(q, delta, relevant=True, stance=None):
    stance = stance or {1: "更樂觀", 0: "維持不變", -1: "更保守"}[delta]
    if not relevant:
        stance = "無關"
    return {"quarter": q, "delta": delta, "cumulative": 0,
            "stance": stance, "detail": "", "is_relevant": relevant}


class TestSummarizeTrajectory:
    def test_empty_series(self):
        out = ins.summarize_trajectory([])
        assert out["direction"] == "無資料"

    def test_consecutive_conservative_streak(self):
        s = [_entry("2024Q1", 1), _entry("2024Q2", -1), _entry("2024Q3", -1)]
        out = ins.summarize_trajectory(s)
        assert out["direction"] == "保守"
        assert out["streak"] == 2
        assert out["arrow"] == "↘"
        assert "連續 2 季" in out["label"]

    def test_single_quarter_shift(self):
        s = [_entry("2024Q1", 0), _entry("2024Q2", 1)]
        out = ins.summarize_trajectory(s)
        assert out["direction"] == "樂觀"
        assert out["streak"] == 1
        assert "最近一季" in out["label"]

    def test_stable_when_latest_is_unchanged(self):
        s = [_entry("2024Q1", -1), _entry("2024Q2", 0)]
        out = ins.summarize_trajectory(s)
        assert out["direction"] == "穩定"
        assert out["arrow"] == "→"

    def test_irrelevant_quarters_skipped(self):
        # 最後一季無關（佔位）→ 應跳過，以最近「有討論」的季度判向
        s = [_entry("2024Q1", -1), _entry("2024Q2", -1),
             _entry("2024Q3", 0, relevant=False)]
        out = ins.summarize_trajectory(s)
        assert out["direction"] == "保守"
        assert out["streak"] == 2

    def test_all_irrelevant(self):
        s = [_entry("2024Q1", 0, relevant=False)]
        assert ins.summarize_trajectory(s)["direction"] == "無資料"


class TestPromiseStats:
    def test_counts_and_rate(self):
        promises = [
            {"status": "✅ 達標"}, {"status": "✅ 達標"},
            {"status": "❌ 未兌現"}, {"status": "⚠ 不明"},
        ]
        out = ins.promise_stats(promises)
        assert (out["fulfilled"], out["missed"], out["unclear"]) == (2, 1, 1)
        # 不明不入分母：2 / (2+1)
        assert out["rate"] == pytest.approx(2 / 3)

    def test_empty(self):
        out = ins.promise_stats([])
        assert out["total"] == 0 and out["rate"] is None

    def test_all_unclear_rate_none(self):
        out = ins.promise_stats([{"status": "⚠ 不明"}])
        assert out["rate"] is None


class TestMaterialityOrdering:
    def _c(self, detail, conf, q="2024Q2"):
        return {"quarter_a": "2024Q1", "quarter_b": q,
                "analysis": {"stance_change": "更保守", "has_contradiction": True,
                             "change_detail": detail, "confidence": conf,
                             "follow_up_question": f"追問:{detail}"}}

    def test_guidance_outranks_generic(self):
        generic = self._c("措辭略有調整", 0.9)
        guidance = self._c("毛利率財測指引由 53% 下修", 0.7)
        out = ins.sort_by_materiality([generic, guidance])
        assert out[0] is guidance  # 權重 × 信心 勝過高信心一般敘述

    def test_tag_assignment(self):
        score, tag = ins.materiality_of(
            {"change_detail": "資本支出計畫縮減", "confidence": 0.8})
        assert tag == "資本支出"
        _, tag2 = ins.materiality_of({"change_detail": "語氣微調", "confidence": 0.8})
        assert tag2 == "一般敘述"

    def test_top_follow_ups_dedup_and_order(self):
        a = self._c("毛利率指引下修", 0.9)
        b = self._c("措辭調整", 0.9)
        dup = self._c("毛利率指引下修", 0.9)  # 與 a 相同追問 → 去重
        out = ins.top_follow_ups([b, a, dup], n=3)
        assert out[0] == "追問:毛利率指引下修"
        assert len(out) == 2


class TestQuarterHelpers:
    def test_latest_quarter(self):
        assert ins.latest_quarter(["2024Q3", "2025Q1", "2024Q4"]) == "2025Q1"
        assert ins.latest_quarter([]) is None

    def test_quarter_end_date(self):
        assert ins.quarter_end_date("2024Q1") == "2024-03-31"
        assert ins.quarter_end_date("2025Q4") == "2025-12-31"
        assert ins.quarter_end_date("bad") is None

    def test_index_prices_base_100(self):
        out = ins.index_prices({"2024Q1": 50.0, "2024Q2": 60.0, "2024Q3": 45.0})
        assert out["2024Q1"] == pytest.approx(100.0)
        assert out["2024Q2"] == pytest.approx(120.0)
        assert out["2024Q3"] == pytest.approx(90.0)

    def test_index_prices_empty(self):
        assert ins.index_prices({}) == {}
