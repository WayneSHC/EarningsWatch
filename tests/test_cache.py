"""
Unit tests for src/ui/cache.py.

Covers the demo-cache capability spec:
  - sanitize_str: None safety + HTML-escape behaviour
  - cache_key: deterministic, sorted quarters, custom_query distinct
  - load_cache: file-missing / corrupted → {}
  - get_cached_result: hit / miss
  - save_to_cache: atomic write, drops chunk content from `retrieved`
"""
import json

import pytest

from src.ui import cache as cache_mod


# ──────────────────────────────────────────────────────────────────────────
# sanitize_str
# ──────────────────────────────────────────────────────────────────────────

class TestSanitizeStr:
    def test_none_returns_empty(self):
        assert cache_mod.sanitize_str(None) == ""

    def test_escapes_html_specials(self):
        out = cache_mod.sanitize_str("<script>alert(1)</script>")
        assert "&lt;script&gt;" in out
        assert "<script>" not in out

    def test_coerces_non_string(self):
        assert cache_mod.sanitize_str(123) == "123"

    def test_escape_ampersand_and_quotes(self):
        out = cache_mod.sanitize_str('Tom & "Jerry"')
        assert "&amp;" in out
        assert "&quot;" in out


# ──────────────────────────────────────────────────────────────────────────
# cache_key
# ──────────────────────────────────────────────────────────────────────────

class TestCacheKey:
    def test_deterministic(self):
        k1 = cache_mod.cache_key("A", "AI", ["Q1"], "")
        k2 = cache_mod.cache_key("A", "AI", ["Q1"], "")
        assert k1 == k2

    def test_quarter_order_irrelevant(self):
        k1 = cache_mod.cache_key("A", "AI", ["2024Q2", "2024Q1"], "")
        k2 = cache_mod.cache_key("A", "AI", ["2024Q1", "2024Q2"], "")
        assert k1 == k2

    def test_custom_query_affects_key(self):
        k1 = cache_mod.cache_key("A", "AI", ["Q1"], "")
        k2 = cache_mod.cache_key("A", "AI", ["Q1"], "比較毛利率")
        assert k1 != k2

    def test_company_change_affects_key(self):
        k1 = cache_mod.cache_key("台積電", "AI", None, "")
        k2 = cache_mod.cache_key("聯發科", "AI", None, "")
        assert k1 != k2


# ──────────────────────────────────────────────────────────────────────────
# load_cache — graceful behaviour
# ──────────────────────────────────────────────────────────────────────────

class TestLoadCache:
    def test_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cache_mod, "CACHE_PATH", tmp_path / "no_such.json")
        assert cache_mod.load_cache() == {}

    def test_corrupted_file_returns_empty(self, monkeypatch, tmp_path):
        bad = tmp_path / "demo_cache.json"
        bad.write_text("not valid JSON {[", encoding="utf-8")
        monkeypatch.setattr(cache_mod, "CACHE_PATH", bad)

        assert cache_mod.load_cache() == {}

    def test_valid_file_loaded(self, monkeypatch, tmp_path):
        good = tmp_path / "demo_cache.json"
        good.write_text(json.dumps({"key1": {"final_report": "x"}}),
                        encoding="utf-8")
        monkeypatch.setattr(cache_mod, "CACHE_PATH", good)

        out = cache_mod.load_cache()
        assert out == {"key1": {"final_report": "x"}}


# ──────────────────────────────────────────────────────────────────────────
# get_cached_result + save_to_cache
# ──────────────────────────────────────────────────────────────────────────

class TestRoundTrip:
    def test_miss_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cache_mod, "CACHE_PATH", tmp_path / "demo_cache.json")
        out = cache_mod.get_cached_result("A", "AI")
        assert out is None

    def test_save_then_get_hits(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cache_mod, "CACHE_PATH", tmp_path / "demo_cache.json")
        cache_mod.save_to_cache("A", "AI", {"final_report": "hello"})

        out = cache_mod.get_cached_result("A", "AI")
        assert out is not None
        assert out["final_report"] == "hello"

    def test_retrieved_chunks_stripped_keep_quarter_keys(self, monkeypatch, tmp_path):
        """save_to_cache must not persist the heavy chunk vectors, only quarter keys."""
        monkeypatch.setattr(cache_mod, "CACHE_PATH", tmp_path / "demo_cache.json")

        result = {
            "final_report": "x",
            "retrieved": {
                "2024Q1": [{"big": "chunk", "payload": {"content": "x" * 5000}}],
                "2024Q2": [{"more": "chunks"}],
            },
        }
        cache_mod.save_to_cache("A", "AI", result)

        entry = cache_mod.get_cached_result("A", "AI")
        assert entry["retrieved"] == {"2024Q1": [], "2024Q2": []}

    def test_parent_dir_auto_created(self, monkeypatch, tmp_path):
        nested_path = tmp_path / "subdir" / "demo_cache.json"
        monkeypatch.setattr(cache_mod, "CACHE_PATH", nested_path)

        # Parent didn't exist before
        assert not nested_path.parent.exists()

        cache_mod.save_to_cache("A", "AI", {"final_report": "x"})

        # Now both parent and file exist
        assert nested_path.exists()

    def test_atomic_write_no_temp_leftover_on_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cache_mod, "CACHE_PATH", tmp_path / "demo_cache.json")
        cache_mod.save_to_cache("A", "AI", {"final_report": "x"})

        # No .tmp files should remain after a successful save
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []
