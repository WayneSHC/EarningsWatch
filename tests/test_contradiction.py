"""
Unit tests for src/core/contradiction.py.

Coverage targets the v1.1 hardening patch:
  - _extract_json: 4 fallback layers (direct → fence → greedy → degraded)
  - _unwrap: tenacity.RetryError unwrapping
  - batch_detect: empty content skip / per-pair isolation / pair_mode
  - detect_promises: filtering of has_promise=False / per-task isolation
"""
from concurrent.futures import Future
import json

import pytest
from tenacity import RetryError

from src.core import contradiction as cd
from tests.conftest import make_chunk


# ──────────────────────────────────────────────────────────────────────────
# _extract_json — JSON parser with 4-layer fallback
# ──────────────────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_direct_json(self):
        out = cd._extract_json('{"stance_change": "更樂觀", "confidence": 0.9}')
        assert out["stance_change"] == "更樂觀"
        assert out["confidence"] == 0.9

    def test_json_with_whitespace(self):
        out = cd._extract_json('   \n  {"a": 1}  \n  ')
        assert out == {"a": 1}

    def test_markdown_fence(self):
        text = '好的，這是分析結果：\n```json\n{"stance_change": "維持不變"}\n```\n供您參考。'
        out = cd._extract_json(text)
        assert out["stance_change"] == "維持不變"

    def test_markdown_fence_no_lang_tag(self):
        text = '```\n{"x": "y"}\n```'
        out = cd._extract_json(text)
        assert out == {"x": "y"}

    def test_greedy_first_to_last_brace(self):
        # Nested braces require first-to-last scan, not regex `\{[^{}]*\}`
        text = 'prefix {"outer": {"inner": "value"}, "n": 1} suffix'
        out = cd._extract_json(text)
        assert out["outer"] == {"inner": "value"}
        assert out["n"] == 1

    def test_complete_garbage_returns_default(self, capsys):
        out = cd._extract_json("totally not json at all 完全亂碼")
        # Degraded result has all the keys downstream code expects
        assert out["confidence"] == 0.0
        assert out["stance_change"] == "無關"
        assert out["has_contradiction"] is False
        assert out["same_topic"] is False
        # Must print a warning so failures surface in logs
        captured = capsys.readouterr().out
        assert "JSON 解析失敗" in captured

    def test_malformed_json_falls_through(self):
        # `{"a":` is truncated → all 3 strategies fail → default
        out = cd._extract_json('{"a":')
        assert out["confidence"] == 0.0


# ──────────────────────────────────────────────────────────────────────────
# _unwrap — must surface real API error from inside RetryError
# ──────────────────────────────────────────────────────────────────────────

class TestUnwrap:
    def test_unwrap_retry_error(self):
        # Simulate tenacity wrapping a real exception
        real = ValueError("RESOURCE_EXHAUSTED: quota exceeded")
        fake_attempt = Future()
        fake_attempt.set_exception(real)
        retry_err = RetryError(last_attempt=fake_attempt)

        unwrapped = cd._unwrap(retry_err)
        assert unwrapped is real
        assert "RESOURCE_EXHAUSTED" in str(unwrapped)

    def test_passthrough_non_retry(self):
        e = RuntimeError("boom")
        assert cd._unwrap(e) is e

    def test_retry_error_with_no_inner(self):
        # Defensive: if last_attempt.exception() returns None, return original
        fake_attempt = Future()
        fake_attempt.set_result("ok")  # no exception → exception() returns None
        retry_err = RetryError(last_attempt=fake_attempt)
        out = cd._unwrap(retry_err)
        # Should return the RetryError itself (no inner exception to unwrap)
        assert out is retry_err


# ──────────────────────────────────────────────────────────────────────────
# _is_boilerplate — legal disclaimer filter
# ──────────────────────────────────────────────────────────────────────────

class TestIsBoilerplate:
    def test_english_forward_looking(self):
        text = "These statements are forward-looking statements subject to significant risks."
        assert cd._is_boilerplate(text) is True

    def test_english_safe_harbor(self):
        text = "This presentation is made under the Safe Harbor provisions."
        assert cd._is_boilerplate(text) is True

    def test_chinese_forward_looking_canonical(self):
        text = "本資料所載之前瞻性陳述涉及風險與不確定性，實際結果可能與預期有所不同。"
        assert cd._is_boilerplate(text) is True

    def test_chinese_alternate_phrasing(self):
        # 「前瞻性敘述」是另一種譯法
        text = "前瞻性敘述受多項因素影響"
        assert cd._is_boilerplate(text) is True

    def test_full_width_whitespace_normalized(self):
        # 全形空白與換行不應阻擋比對
        text = "前瞻性　陳述\n\n  涉及風險"
        assert cd._is_boilerplate(text) is True

    def test_real_topic_content_not_flagged(self):
        text = "本季 AI 需求強勁，HPC 平台營收年增 35%，毛利率優於上季。"
        assert cd._is_boilerplate(text) is False

    def test_case_insensitive_english(self):
        text = "FORWARD-LOOKING STATEMENTS SUBJECT TO SIGNIFICANT RISKS"
        assert cd._is_boilerplate(text) is True


# ──────────────────────────────────────────────────────────────────────────
# detect_contradiction — type guards
# ──────────────────────────────────────────────────────────────────────────

class TestDetectContradiction:
    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="必須為 dict"):
            cd.detect_contradiction(None, {"quarter": "2024Q1"}, "AI")

    def test_swaps_quarters_for_chronological_order(self, monkeypatch):
        captured = {}

        def fake_chat(prompt, max_tokens=600, mode="demo"):
            captured["prompt"] = prompt
            return '{"stance_change": "更樂觀", "confidence": 0.8}'

        monkeypatch.setattr(cd, "llm_chat", fake_chat)

        # Pass later quarter first; function should swap so 2024Q1 appears earlier in prompt
        a = {"quarter": "2024Q3", "content": "later content", "date": "2024-10-01"}
        b = {"quarter": "2024Q1", "content": "earlier content", "date": "2024-04-01"}
        cd.detect_contradiction(a, b, "AI需求")

        idx_q1 = captured["prompt"].index("2024Q1")
        idx_q3 = captured["prompt"].index("2024Q3")
        assert idx_q1 < idx_q3, "earlier quarter must appear first in prompt"


# ──────────────────────────────────────────────────────────────────────────
# batch_detect — fault isolation, content gating, pair modes
# ──────────────────────────────────────────────────────────────────────────

class TestBatchDetect:
    def test_empty_input(self, monkeypatch):
        monkeypatch.setattr(cd, "llm_chat",
                            lambda *a, **kw: '{"stance_change":"無關"}')
        assert cd.batch_detect({}, "AI需求") == []

    def test_skips_quarter_with_empty_content(self, monkeypatch, capsys):
        called = {"n": 0}

        def fake_chat(*a, **kw):
            called["n"] += 1
            return '{"stance_change":"維持不變","confidence":0.8}'

        monkeypatch.setattr(cd, "llm_chat", fake_chat)

        data = {
            "2024Q1": [make_chunk("real content about AI", "2024Q1")],
            "2024Q2": [make_chunk("   ", "2024Q2")],   # whitespace only
            "2024Q3": [make_chunk("more AI talk", "2024Q3")],
        }
        results = cd.batch_detect(data, "AI需求")

        # Adjacent pairs: (Q1,Q2) and (Q2,Q3). Q2 is empty, so both are skipped.
        # Only (Q1,Q3) is NOT a generated adjacent pair → 0 LLM calls.
        assert called["n"] == 0
        assert results == []
        assert "內容為空，跳過比對" in capsys.readouterr().out

    def test_per_pair_failure_isolated(self, monkeypatch, capsys):
        # First call succeeds, second call fails — second result must still be present
        # with degraded analysis, not abort the whole batch.
        call_count = {"n": 0}

        def fake_chat(prompt, max_tokens=600, mode="demo"):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return '{"stance_change":"更樂觀","confidence":0.85}'
            raise RuntimeError("simulated API failure")

        monkeypatch.setattr(cd, "llm_chat", fake_chat)
        # Force max_workers=1 so call order is deterministic
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")
        # Disable retry so the failing call returns fast
        monkeypatch.setattr(cd.detect_contradiction, "retry",
                            lambda *a, **kw: cd.detect_contradiction.__wrapped__,
                            raising=False)

        data = {
            "2024Q1": [make_chunk("c1", "2024Q1")],
            "2024Q2": [make_chunk("c2", "2024Q2")],
            "2024Q3": [make_chunk("c3", "2024Q3")],
        }
        results = cd.batch_detect(data, "AI需求")

        assert len(results) == 2
        # First pair succeeded
        assert results[0]["analysis"]["confidence"] == 0.85
        # Second pair degraded but not missing
        assert results[1]["analysis"]["confidence"] == 0.0
        assert "RuntimeError" in results[1]["analysis"]["change_detail"]

    def test_pair_mode_adjacent_default(self, monkeypatch):
        prompts = []

        def fake_chat(prompt, max_tokens=600, mode="demo"):
            prompts.append(prompt)
            return '{"stance_change":"維持不變","confidence":0.8}'

        monkeypatch.setattr(cd, "llm_chat", fake_chat)
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        data = {q: [make_chunk("x", q)] for q in
                ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]}
        cd.batch_detect(data, "AI")
        # Adjacent: 4 quarters → 3 pairs
        assert len(prompts) == 3

    def test_pair_mode_all_pairs(self, monkeypatch):
        prompts = []

        def fake_chat(prompt, max_tokens=600, mode="demo"):
            prompts.append(prompt)
            return '{"stance_change":"維持不變","confidence":0.8}'

        monkeypatch.setattr(cd, "llm_chat", fake_chat)
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        data = {q: [make_chunk("x", q)] for q in
                ["2024Q1", "2024Q2", "2024Q3"]}
        cd.batch_detect(data, "AI", pair_mode="all_pairs")
        # All pairs: 3 quarters → 3 pairs (Q1-Q2, Q1-Q3, Q2-Q3)
        assert len(prompts) == 3

        data4 = {q: [make_chunk("x", q)] for q in
                 ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]}
        prompts.clear()
        cd.batch_detect(data4, "AI", pair_mode="all_pairs")
        # 4 quarters → 6 pairs
        assert len(prompts) == 6

    def test_content_truncation(self, monkeypatch):
        captured = {}

        def fake_chat(prompt, max_tokens=600, mode="demo"):
            captured["prompt"] = prompt
            return '{"stance_change":"維持不變","confidence":0.8}'

        monkeypatch.setattr(cd, "llm_chat", fake_chat)
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        # 5000 chars; _MAX_CONTENT = 2000
        big = "字" * 5000
        data = {
            "2024Q1": [make_chunk(big, "2024Q1")],
            "2024Q2": [make_chunk(big, "2024Q2")],
        }
        cd.batch_detect(data, "AI")

        # The prompt should not contain the full 5000-char string
        assert "字" * 2001 not in captured["prompt"]

    def test_sources_extracted_dedupe(self, monkeypatch):
        monkeypatch.setattr(cd, "llm_chat",
                            lambda *a, **kw: '{"stance_change":"維持不變","confidence":0.8}')
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        data = {
            "2024Q1": [
                make_chunk("a", "2024Q1", file="A.pdf", page=1),
                make_chunk("b", "2024Q1", file="A.pdf", page=1),  # dup
                make_chunk("c", "2024Q1", file="A.pdf", page=2),
            ],
            "2024Q2": [make_chunk("d", "2024Q2", file="B.pdf", page=5)],
        }
        results = cd.batch_detect(data, "AI", chunks_per_pair=4)
        sources_a = results[0]["sources_a"]
        # Expect 2 unique (file,page) pairs from Q1
        assert len(sources_a) == 2
        assert {(s["file"], s["page"]) for s in sources_a} == {("A.pdf", 1), ("A.pdf", 2)}


# ──────────────────────────────────────────────────────────────────────────
# detect_promises — filtering + isolation
# ──────────────────────────────────────────────────────────────────────────

class TestDetectPromises:
    def test_filters_no_promise(self, monkeypatch):
        # All quarters return has_promise=false → empty list
        monkeypatch.setattr(cd, "llm_chat",
                            lambda *a, **kw: '{"has_promise": false}')
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        data = {q: [make_chunk("x", q)] for q in ["2024Q1", "2024Q2"]}
        assert cd.detect_promises(data, "AI") == []

    def test_emits_status_emoji(self, monkeypatch):
        responses = iter([
            json.dumps({
                "has_promise": True,
                "promise_summary": "毛利率將回到 53%",
                "status": "達標",
                "detail": "Q2 毛利率 53.2%",
                "confidence": 0.9,
            }),
        ])
        monkeypatch.setattr(cd, "llm_chat", lambda *a, **kw: next(responses))
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        data = {
            "2024Q1": [make_chunk("guidance about gross margin", "2024Q1")],
            "2024Q2": [make_chunk("Q2 actual results", "2024Q2")],
        }
        results = cd.detect_promises(data, "毛利率")
        assert len(results) == 1
        assert results[0]["status"] == "✅ 達標"
        assert results[0]["promise_quarter"] == "2024Q1"
        assert results[0]["followup_quarter"] == "2024Q2"

    def test_per_task_failure_isolated(self, monkeypatch, capsys):
        call_count = {"n": 0}

        def fake_chat(prompt, max_tokens=300, mode="demo"):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated failure")
            return json.dumps({
                "has_promise": True,
                "promise_summary": "ok",
                "status": "達標",
                "detail": "done",
                "confidence": 0.7,
            })

        monkeypatch.setattr(cd, "llm_chat", fake_chat)
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        data = {q: [make_chunk("x", q)] for q in
                ["2024Q1", "2024Q2", "2024Q3"]}
        results = cd.detect_promises(data, "AI")
        # First task failed (returns None → filtered), second task succeeded
        assert len(results) == 1
        assert "承諾分析失敗" in capsys.readouterr().out

    def test_skips_quarter_with_no_chunks(self, monkeypatch):
        called = {"n": 0}

        def fake_chat(*a, **kw):
            called["n"] += 1
            return '{"has_promise": false}'

        monkeypatch.setattr(cd, "llm_chat", fake_chat)
        monkeypatch.setenv("LLM_PAIR_WORKERS", "1")

        data = {
            "2024Q1": [],   # empty → no task
            "2024Q2": [make_chunk("x", "2024Q2")],
        }
        cd.detect_promises(data, "AI")
        # No task generated for Q1→Q2 because Q1 chunks is empty
        assert called["n"] == 0
