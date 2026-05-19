"""
Unit tests for src/ui/chart.py.

Covers the stance-trend-chart capability spec:
  - build_stance_series: quarter_b dedup + most-significant-stance preference
  - "無關" / boilerplate placeholders (delta=0, is_relevant=False)
  - STANCE_SCORE constants
  - cumulative computation
  - sort order
  - render_trend_chart: empty data annotation + relevant/irrelevant trace split
  - chart_to_scrollable_html wraps in horizontal-scroll div
"""
import pytest

from src.ui import chart


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

class TestStanceScore:
    def test_score_map_locked(self):
        assert chart.STANCE_SCORE["更樂觀"] == 1
        assert chart.STANCE_SCORE["維持不變"] == 0
        assert chart.STANCE_SCORE["更保守"] == -1


# ──────────────────────────────────────────────────────────────────────────
# build_stance_series
# ──────────────────────────────────────────────────────────────────────────

def _c(qa, qb, stance, ev_a="x", ev_b="y", detail=""):
    return {
        "quarter_a": qa,
        "quarter_b": qb,
        "analysis": {
            "stance_change": stance,
            "evidence_early": ev_a,
            "evidence_later": ev_b,
            "change_detail": detail,
        },
    }


class TestBuildStanceSeries:
    def test_missing_quarter_b_skipped(self):
        out = chart.build_stance_series([
            _c("2024Q1", "", "更樂觀"),
        ])
        assert out == []

    def test_same_qb_keeps_most_significant(self):
        """When two contradictions target the same quarter_b, the one with
        higher abs(delta) wins (更樂觀 = ±1 beats 維持不變 = 0)."""
        out = chart.build_stance_series([
            _c("2024Q1", "2024Q3", "維持不變"),
            _c("2024Q2", "2024Q3", "更樂觀"),
        ])
        assert len(out) == 1
        assert out[0]["stance"] == "更樂觀"
        assert out[0]["delta"] == 1

    def test_irrelevant_placeholder(self):
        out = chart.build_stance_series([
            _c("2024Q1", "2024Q3", "無關"),
        ])
        assert len(out) == 1
        assert out[0]["delta"] == 0
        assert out[0]["stance"] == "無關"
        assert out[0]["is_relevant"] is False

    def test_boilerplate_repeat_evidence_placeholder(self):
        out = chart.build_stance_series([
            _c("2024Q1", "2024Q3", "更樂觀",
               ev_a="前瞻性陳述...", ev_b="前瞻性陳述..."),
        ])
        assert len(out) == 1
        assert out[0]["is_relevant"] is False
        assert out[0]["delta"] == 0

    def test_cumulative_sum_with_zero_holdings(self):
        out = chart.build_stance_series([
            _c("2024Q1", "2024Q2", "更樂觀"),
            _c("2024Q2", "2024Q3", "維持不變"),
            _c("2024Q3", "2024Q4", "更樂觀"),
        ])
        cumulatives = [s["cumulative"] for s in out]
        # +1, 0, +1 → cumulative 1, 1, 2
        assert cumulatives == [1, 1, 2]

    def test_cumulative_with_negative_delta(self):
        out = chart.build_stance_series([
            _c("2024Q1", "2024Q2", "更樂觀"),
            _c("2024Q2", "2024Q3", "更保守"),
            _c("2024Q3", "2024Q4", "維持不變"),
            _c("2024Q4", "2025Q1", "更樂觀"),
        ])
        cumulatives = [s["cumulative"] for s in out]
        # +1, -1, 0, +1 → 1, 0, 0, 1
        assert cumulatives == [1, 0, 0, 1]

    def test_series_sorted_by_quarter(self):
        out = chart.build_stance_series([
            _c("2024Q1", "2024Q2", "更樂觀"),
            _c("2023Q3", "2023Q4", "更保守"),
            _c("2024Q3", "2024Q4", "更樂觀"),
        ])
        quarters = [s["quarter"] for s in out]
        assert quarters == ["2023Q4", "2024Q2", "2024Q4"]


# ──────────────────────────────────────────────────────────────────────────
# render_trend_chart
# ──────────────────────────────────────────────────────────────────────────

class TestRenderTrendChart:
    def test_empty_data_returns_figure_with_annotation(self):
        fig = chart.render_trend_chart({"A": []}, "AI", "cumulative")
        # Plotly Figure objects expose .layout.annotations
        ann_texts = [a.text for a in fig.layout.annotations]
        assert any("無立場變化資料" in t for t in ann_texts)

    def test_cumulative_mode_splits_relevant_irrelevant(self):
        """1 relevant + 1 irrelevant series → 2 traces (main + open-marker)."""
        series = chart.build_stance_series([
            _c("2024Q1", "2024Q2", "更樂觀"),
            _c("2024Q2", "2024Q3", "無關"),
        ])
        fig = chart.render_trend_chart({"A": series}, "AI", "cumulative")
        assert len(fig.data) == 2

    def test_cumulative_mode_all_relevant_single_trace(self):
        series = chart.build_stance_series([
            _c("2024Q1", "2024Q2", "更樂觀"),
            _c("2024Q2", "2024Q3", "更保守"),
        ])
        fig = chart.render_trend_chart({"A": series}, "AI", "cumulative")
        # Only the relevant main line, no irrelevant trace
        assert len(fig.data) == 1

    def test_delta_mode_is_bar_chart(self):
        series = chart.build_stance_series([
            _c("2024Q1", "2024Q2", "更樂觀"),
        ])
        fig = chart.render_trend_chart({"A": series}, "AI", "delta")
        # Bar chart
        from plotly.graph_objects import Bar
        assert any(isinstance(t, Bar) for t in fig.data)


# ──────────────────────────────────────────────────────────────────────────
# chart_to_scrollable_html
# ──────────────────────────────────────────────────────────────────────────

class TestChartToScrollableHtml:
    def test_wraps_in_overflow_div(self):
        import plotly.graph_objects as go
        fig = go.Figure()
        html = chart.chart_to_scrollable_html(fig)

        # Outer div with horizontal scroll
        assert html.startswith("<div style=\"overflow-x:auto")
        assert html.endswith("</div>")
        # Plotly CDN reference present
        assert "plotly" in html.lower()
