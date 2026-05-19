"""
Unit tests for src/core/comparison.py.

Covers the multi-company-comparison capability spec:
  - run_multi_company: parallel + 2-worker cap + per-company error isolation
  - custom_query overrides template
  - build_comparison_table: filters 無關 / boilerplate / sorts by quarter
  - synthesize_diff: empty / all-aligned / has-divergence / LLM failure / row cap
"""
import pytest

from src.core import comparison as cmp_mod


# ──────────────────────────────────────────────────────────────────────────
# run_multi_company
# ──────────────────────────────────────────────────────────────────────────

class TestRunMultiCompany:
    def test_one_company_one_worker_call(self, monkeypatch):
        calls = []

        def fake_run_agent(*, query, company, topic, quarters):
            calls.append({"query": query, "company": company,
                          "topic": topic, "quarters": quarters})
            return {"contradictions": [], "promises": [],
                    "confidence": 0.9, "final_report": ""}

        monkeypatch.setattr(cmp_mod, "run_agent", fake_run_agent)

        out = cmp_mod.run_multi_company(["台積電"], "AI需求", [])

        assert "台積電" in out
        assert calls[0]["company"] == "台積電"
        # Default template used
        assert "AI需求" in calls[0]["query"]

    def test_custom_query_overrides_template(self, monkeypatch):
        captured = []

        def fake_run_agent(*, query, company, topic, quarters):
            captured.append(query)
            return {"contradictions": [], "promises": [], "confidence": 0.9,
                    "final_report": ""}

        monkeypatch.setattr(cmp_mod, "run_agent", fake_run_agent)

        cmp_mod.run_multi_company(["台積電"], "AI", [], custom_query="比較毛利率")

        assert captured[0] == "比較毛利率"

    def test_per_company_error_isolation(self, monkeypatch):
        """Failure in one company must not impact others."""
        def fake_run_agent(*, query, company, topic, quarters):
            if company == "台積電":
                raise RuntimeError("intentional boom")
            return {"contradictions": [], "promises": [], "confidence": 0.8,
                    "final_report": "ok"}

        monkeypatch.setattr(cmp_mod, "run_agent", fake_run_agent)

        out = cmp_mod.run_multi_company(["台積電", "聯發科"], "AI", [])

        assert "台積電" in out and "聯發科" in out
        assert out["台積電"]["error"] == "RuntimeError"
        assert "分析失敗" in out["台積電"]["final_report"]
        assert out["台積電"]["confidence"] == 0.0
        # Other company unaffected
        assert out["聯發科"]["final_report"] == "ok"
        assert out["聯發科"]["confidence"] == 0.8


# ──────────────────────────────────────────────────────────────────────────
# build_comparison_table
# ──────────────────────────────────────────────────────────────────────────

def _contradiction(qa, qb, stance, ev_a="x", ev_b="y"):
    return {
        "quarter_a": qa,
        "quarter_b": qb,
        "analysis": {
            "stance_change": stance,
            "evidence_early": ev_a,
            "evidence_later": ev_b,
        },
    }


class TestBuildComparisonTable:
    def test_filters_neutral_stance(self):
        results = {
            "台積電": {"contradictions": [
                _contradiction("2024Q1", "2024Q2", "無關"),
                _contradiction("2024Q2", "2024Q3", "更樂觀"),
            ]},
            "聯發科": {"contradictions": []},
        }
        table = cmp_mod.build_comparison_table(results)
        # Only the 更樂觀 pair survives
        assert len(table) == 1
        assert table[0]["quarter_pair"] == "2024Q2 vs 2024Q3"

    def test_filters_repeated_evidence_boilerplate(self):
        same_text = "前瞻性陳述：..."
        results = {
            "台積電": {"contradictions": [
                _contradiction("2024Q1", "2024Q2", "更樂觀",
                               ev_a=same_text, ev_b=same_text),
            ]},
        }
        table = cmp_mod.build_comparison_table(results)
        assert table == []

    def test_missing_company_quarter_fills_dash(self):
        results = {
            "台積電": {"contradictions": [
                _contradiction("2024Q1", "2024Q2", "更樂觀"),
            ]},
            "聯發科": {"contradictions": []},
        }
        table = cmp_mod.build_comparison_table(results)
        assert table[0]["台積電"] == "更樂觀"
        assert table[0]["聯發科"] == "—"

    def test_rows_sorted_by_quarter(self):
        results = {
            "A": {"contradictions": [
                _contradiction("2024Q2", "2024Q3", "更樂觀"),
                _contradiction("2023Q4", "2024Q1", "更保守"),
                _contradiction("2024Q1", "2024Q2", "更樂觀"),
            ]},
        }
        table = cmp_mod.build_comparison_table(results)
        # Pairs sorted by the first quarter
        first_quarters = [row["quarter_pair"].split(" vs ")[0] for row in table]
        assert first_quarters == sorted(first_quarters)
        assert first_quarters[0] == "2023Q4"


# ──────────────────────────────────────────────────────────────────────────
# synthesize_diff
# ──────────────────────────────────────────────────────────────────────────

class TestSynthesizeDiff:
    def test_empty_table_short_circuits(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(cmp_mod, "llm_chat",
                            lambda *a, **kw: (called.__setitem__("n", called["n"] + 1)) or "x")

        out = cmp_mod.synthesize_diff([], "AI", ["A", "B"])

        assert "資料不足" in out
        assert called["n"] == 0  # LLM not invoked

    def test_all_aligned_returns_consistency_message(self, monkeypatch):
        called = {"n": 0}

        def fake_llm(*a, **kw):
            called["n"] += 1
            return "should not be called"

        monkeypatch.setattr(cmp_mod, "llm_chat", fake_llm)

        # All rows: both companies show identical stance → no divergence
        table = [
            {"quarter_pair": "2024Q1 vs 2024Q2", "A": "更樂觀", "B": "更樂觀"},
            {"quarter_pair": "2024Q2 vs 2024Q3", "A": "更保守", "B": "更保守"},
        ]
        out = cmp_mod.synthesize_diff(table, "AI", ["A", "B"])

        assert "立場走勢高度一致" in out
        assert called["n"] == 0

    def test_divergence_invokes_llm(self, monkeypatch):
        captured = []
        monkeypatch.setattr(cmp_mod, "llm_chat",
                            lambda prompt, max_tokens=250: captured.append(prompt) or "summary OK")

        table = [
            {"quarter_pair": "2024Q1 vs 2024Q2", "A": "更樂觀", "B": "更保守"},
            {"quarter_pair": "2024Q2 vs 2024Q3", "A": "更樂觀", "B": "更樂觀"},
        ]
        out = cmp_mod.synthesize_diff(table, "AI", ["A", "B"])

        assert out == "summary OK"
        assert len(captured) == 1
        # Divergence row must be in prompt, aligned row should still survive within cap
        assert "2024Q1 vs 2024Q2" in captured[0]

    def test_caps_divergence_rows_to_eight(self, monkeypatch):
        captured = []
        monkeypatch.setattr(cmp_mod, "llm_chat",
                            lambda prompt, **kw: captured.append(prompt) or "ok")

        table = [
            {"quarter_pair": f"P{i}", "A": "更樂觀", "B": "更保守"}
            for i in range(10)
        ]
        cmp_mod.synthesize_diff(table, "AI", ["A", "B"])

        prompt = captured[0]
        # Only first 8 divergence rows survive
        assert "P0" in prompt and "P7" in prompt
        assert "P9" not in prompt

    def test_llm_failure_returns_safe_message(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(cmp_mod, "llm_chat", boom)

        table = [{"quarter_pair": "2024Q1 vs 2024Q2", "A": "更樂觀", "B": "更保守"}]
        out = cmp_mod.synthesize_diff(table, "AI", ["A", "B"])

        assert "差異摘要生成失敗" in out
