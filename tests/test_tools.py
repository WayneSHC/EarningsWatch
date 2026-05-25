"""
Unit tests for src/agent/tools.py.

Covers the tavily-news capability spec:
  - _get_tavily: lazy + lru_cache, returns None on missing key / missing package
  - _parse_pub_date: ISO 8601 Z / date-only / failure fallback
  - _news_mentions_company: alias matching (CJK + English + ticker)
  - search_news: company filter + time-major / score-minor stable sort + failure isolation
  - get_stock_price: STOCK_CODE_MAP lookup + error contract
  - decide_tools / decide_tools_by_keyword: bigquery always present + LLM fallback
"""
from datetime import datetime, timezone

import pytest

from src.agent import tools as tools_mod


# ──────────────────────────────────────────────────────────────────────────
# _get_tavily
# ──────────────────────────────────────────────────────────────────────────

class TestGetTavily:
    def test_no_key_returns_none(self, monkeypatch):
        from src.core import secrets as secrets_mod
        monkeypatch.setattr(secrets_mod, "get_secret",
                            lambda name, default="": "")
        tools_mod._get_tavily.cache_clear()

        assert tools_mod._get_tavily() is None
        tools_mod._get_tavily.cache_clear()


# ──────────────────────────────────────────────────────────────────────────
# _parse_pub_date
# ──────────────────────────────────────────────────────────────────────────

class TestParsePubDate:
    def test_iso_with_z(self):
        dt = tools_mod._parse_pub_date("2024-10-17T08:00:00Z")
        assert dt.year == 2024 and dt.month == 10 and dt.day == 17
        assert dt.tzinfo is not None

    def test_date_only_gets_utc(self):
        dt = tools_mod._parse_pub_date("2024-10-17")
        assert dt.tzinfo is not None

    def test_invalid_returns_min(self):
        dt = tools_mod._parse_pub_date("not a date")
        assert dt == datetime.min.replace(tzinfo=timezone.utc)

    def test_empty_returns_min(self):
        assert tools_mod._parse_pub_date("") == datetime.min.replace(tzinfo=timezone.utc)


# ──────────────────────────────────────────────────────────────────────────
# _news_mentions_company
# ──────────────────────────────────────────────────────────────────────────

class TestNewsMentionsCompany:
    def test_chinese_name_matches(self):
        item = {"title": "聯發科發布新晶片", "content": ""}
        assert tools_mod._news_mentions_company(item, "聯發科") is True

    def test_english_alias_matches(self):
        item = {"title": "TSMC ramps AI capacity", "content": ""}
        assert tools_mod._news_mentions_company(item, "台積電") is True

    def test_ticker_matches(self):
        item = {"title": "", "content": "Mediatek 2454 reports Q3"}
        assert tools_mod._news_mentions_company(item, "聯發科") is True

    def test_unrelated_filtered(self):
        item = {"title": "Other industry news", "content": "TE Connectivity"}
        # 台達電 aliases: 台達電 / 台達 / Delta / 2308
        assert tools_mod._news_mentions_company(item, "台達電") is False


# ──────────────────────────────────────────────────────────────────────────
# search_news
# ──────────────────────────────────────────────────────────────────────────

class TestSearchNews:
    def test_no_tavily_returns_empty(self, monkeypatch, capsys):
        monkeypatch.setattr(tools_mod, "_get_tavily", lambda: None)
        out = tools_mod.search_news("AI", "台積電")
        assert out == []
        captured = capsys.readouterr()
        assert "Tavily 未設定" in captured.out

    def test_sort_by_date_desc_then_score(self, monkeypatch):
        class FakeClient:
            def search(self, **kw):
                return {
                    "results": [
                        # Older but high relevance
                        {"title": "TSMC", "content": "",
                         "url": "u1", "published_date": "2024-10-01",
                         "score": 0.95},
                        # Newer, lower relevance
                        {"title": "TSMC", "content": "",
                         "url": "u2", "published_date": "2024-12-01",
                         "score": 0.40},
                        # No date — must sink to bottom
                        {"title": "TSMC", "content": "",
                         "url": "u3", "published_date": "",
                         "score": 0.99},
                    ],
                }

        monkeypatch.setattr(tools_mod, "_get_tavily", lambda: FakeClient())
        out = tools_mod.search_news("AI", "台積電", max_results=5)

        urls = [r["url"] for r in out]
        # Newer wins regardless of score
        assert urls[0] == "u2"
        # Older but datable beats undated
        assert urls[1] == "u1"
        # Undated dragged to the end
        assert urls[-1] == "u3"

    def test_filters_news_not_mentioning_company(self, monkeypatch, capsys):
        class FakeClient:
            def search(self, **kw):
                return {
                    "results": [
                        {"title": "TSMC ramps capacity", "content": "",
                         "url": "u1", "published_date": "2024-10-01",
                         "score": 0.9},
                        # Unrelated — gets filtered
                        {"title": "Apple unveils product", "content": "",
                         "url": "u2", "published_date": "2024-10-02",
                         "score": 0.9},
                    ],
                }

        monkeypatch.setattr(tools_mod, "_get_tavily", lambda: FakeClient())
        out = tools_mod.search_news("AI", "台積電")

        assert len(out) == 1
        assert out[0]["url"] == "u1"
        captured = capsys.readouterr()
        assert "過濾掉" in captured.out

    def test_search_failure_returns_empty(self, monkeypatch, capsys):
        class BoomClient:
            def search(self, **kw):
                raise RuntimeError("API down")

        monkeypatch.setattr(tools_mod, "_get_tavily", lambda: BoomClient())
        out = tools_mod.search_news("AI", "台積電")

        assert out == []
        captured = capsys.readouterr()
        # log_exc format: "[Tools] ⚠ Tavily 搜尋 失敗（RuntimeError: API down）"
        # Assert on stable substrings — the where-label and the exception type —
        # so future format tweaks in src/core/safe.log_exc don't break this test.
        assert "Tavily 搜尋" in captured.out
        assert "RuntimeError" in captured.out


# ──────────────────────────────────────────────────────────────────────────
# get_stock_price
# ──────────────────────────────────────────────────────────────────────────

class TestGetStockPrice:
    def test_unknown_company_returns_error_without_yf(self, monkeypatch):
        called = {"n": 0}

        class FakeYF:
            def Ticker(self, _sym):
                called["n"] += 1
                return object()

        monkeypatch.setattr(tools_mod, "yf", FakeYF())

        out = tools_mod.get_stock_price("未知公司")

        assert "error" in out
        assert called["n"] == 0  # yfinance not called


# ──────────────────────────────────────────────────────────────────────────
# decide_tools_by_keyword
# ──────────────────────────────────────────────────────────────────────────

class TestDecideToolsByKeyword:
    def test_bigquery_always_present(self):
        out = tools_mod.decide_tools_by_keyword("毛利率 歷史", "毛利率")
        assert "bigquery" in out

    def test_forward_looking_triggers_tavily(self):
        out = tools_mod.decide_tools_by_keyword("未來 AI 展望", "AI")
        assert "tavily" in out

    def test_stock_keyword_triggers_yfinance(self):
        out = tools_mod.decide_tools_by_keyword("股價漲跌如何", "")
        assert "yfinance" in out

    def test_no_trigger_returns_bigquery_only(self):
        out = tools_mod.decide_tools_by_keyword("毛利率歷史", "毛利率")
        assert out == ["bigquery"]


# ──────────────────────────────────────────────────────────────────────────
# decide_tools — LLM-driven with keyword fallback
# ──────────────────────────────────────────────────────────────────────────

class TestDecideTools:
    def test_llm_response_used(self, monkeypatch):
        # Patch the import target inside decide_tools
        from src.core import llm_client
        monkeypatch.setattr(
            llm_client, "chat",
            lambda prompt, max_tokens=150:
                '{"tools": ["bigquery", "tavily"], "reasoning": "ok"}'
        )

        out = tools_mod.decide_tools("AI 未來展望如何", "AI")
        assert "bigquery" in out and "tavily" in out

    def test_llm_failure_falls_back_to_keyword(self, monkeypatch):
        from src.core import llm_client

        def fail(*a, **kw):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(llm_client, "chat", fail)

        # Query with "最新" keyword should still surface tavily
        out = tools_mod.decide_tools("最新 AI 趨勢", "AI")
        assert "bigquery" in out
        assert "tavily" in out

    def test_llm_unknown_tools_filtered_but_bigquery_kept(self, monkeypatch):
        from src.core import llm_client
        monkeypatch.setattr(
            llm_client, "chat",
            lambda *a, **kw: '{"tools": ["bigquery", "fake_tool"], "reasoning": "x"}'
        )

        out = tools_mod.decide_tools("毛利率", "毛利率")
        assert "bigquery" in out
        assert "fake_tool" not in out
