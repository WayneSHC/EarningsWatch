"""
Unit tests for src/ingestion/chunker.py.

Covers the pdf-chunking capability spec:
  - Chunking constants locked (CHUNK_SIZE / OVERLAP / MIN_CHUNK_LEN)
  - Section classification (QA / guidance / opening)
  - Table pages preserved as a single chunk
  - Sliding window splits + minimum-length filter
  - QA splitting with overflow → falls back to sliding window
  - Per-chunk feature tags (contains_guidance / contains_number / topics)
  - Metadata propagation
  - Empty page → []
  - chunk_document flattens pages
"""
import pytest

from src.ingestion import chunker


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_chunk_size_locked(self):
        assert chunker.CHUNK_SIZE == 500

    def test_overlap_locked(self):
        assert chunker.OVERLAP == 100

    def test_min_chunk_len_locked(self):
        assert chunker.MIN_CHUNK_LEN == 50


# ──────────────────────────────────────────────────────────────────────────
# _classify_section
# ──────────────────────────────────────────────────────────────────────────

class TestClassifySection:
    def test_qa_marker(self):
        assert chunker._classify_section("分析師：請問本季毛利率") == "QA"

    def test_qa_question_prefix(self):
        assert chunker._classify_section("Q1：請說明 AI 需求趨勢") == "QA"

    def test_guidance_keyword(self):
        assert chunker._classify_section("本季毛利率展望 53%") == "guidance"

    def test_guidance_english_keyword(self):
        # "guidance" alone in body triggers
        assert chunker._classify_section("Provided clear guidance for Q4") == "guidance"

    def test_opening_default(self):
        # Avoid "Q3" — the QA regex matches "Q\d*[\s：]" so any "Q數字 " trips it.
        assert chunker._classify_section("歡迎各位來到本年度法說會") == "opening"


# ──────────────────────────────────────────────────────────────────────────
# _sliding_window
# ──────────────────────────────────────────────────────────────────────────

class TestSlidingWindow:
    def test_short_text_dropped(self):
        # 30 chars < MIN_CHUNK_LEN(50) → dropped
        text = "短文" * 7  # ~14 chars
        assert chunker._sliding_window(text) == []

    def test_chunk_size_capped(self):
        text = "本季" * 600  # 1200 chars
        chunks = chunker._sliding_window(text)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c) <= chunker.CHUNK_SIZE

    def test_chunks_overlap(self):
        """Consecutive chunks should share `overlap` worth of tail/head — verify
        the algorithm advances by chunk_size - overlap each step."""
        text = "x" * 1100  # 1100 chars
        chunks = chunker._sliding_window(text, chunk_size=500, overlap=100)
        # 1100 chars / (500 - 100 = 400 step) → 3 chunks: [0:500], [400:900], [800:1100]
        assert len(chunks) == 3


# ──────────────────────────────────────────────────────────────────────────
# _split_qa
# ──────────────────────────────────────────────────────────────────────────

class TestSplitQA:
    def test_keeps_question_prefix(self):
        text = "分析師王明：請問本季毛利率變化。\n" + ("我們本季毛利率提升至 53%，主要原因為 N3 良率改善。" * 5)
        chunks = chunker._split_qa(text)
        assert len(chunks) >= 1
        # The QA chunk should include the question prefix
        assert "分析師" in chunks[0]

    def test_too_short_falls_back_to_sliding_window(self):
        """_split_qa returns sliding_window result only when its own loop
        yields nothing (e.g. text too short to pass MIN_CHUNK_LEN)."""
        text = "短"  # 1 char → below MIN_CHUNK_LEN
        chunks = chunker._split_qa(text)
        sliding = chunker._sliding_window(text)
        assert chunks == sliding == []

    def test_long_qa_split_via_sliding_window(self):
        """A single QA chunk exceeding CHUNK_SIZE * 2 must be re-split."""
        question = "分析師：請說明 AI 需求"
        long_answer = "AI 需求強勁延續" * 200  # ~2400 chars
        text = question + "\n" + long_answer
        chunks = chunker._split_qa(text)
        # All resulting chunks should respect CHUNK_SIZE upper bound
        for c in chunks:
            assert len(c) <= chunker.CHUNK_SIZE


# ──────────────────────────────────────────────────────────────────────────
# chunk_page — table page
# ──────────────────────────────────────────────────────────────────────────

class TestChunkPageTable:
    def test_table_page_single_chunk_section_table(self):
        page = {
            "content": "（第5頁財務表格）\n季度：2024Q1、營收：500億" * 30,
            "has_table": True,
            "parse_method": "pdfplumber",
            "metadata": {"company": "台積電", "quarter": "2024Q1"},
        }
        chunks = chunker.chunk_page(page)
        assert len(chunks) == 1
        assert chunks[0]["section"] == "table"
        assert chunks[0]["content"] == page["content"]


# ──────────────────────────────────────────────────────────────────────────
# chunk_page — feature tags
# ──────────────────────────────────────────────────────────────────────────

class TestFeatureTags:
    def _page(self, content):
        return {
            "content": content,
            "has_table": False,
            "parse_method": "pdfplumber",
            "metadata": {"company": "台積電", "quarter": "2024Q3", "source_page": 5},
        }

    def test_contains_guidance_chinese(self):
        c = "我們對下季展望維持審慎樂觀，預估毛利率 53%。" + "補充說明" * 30
        chunks = chunker.chunk_page(self._page(c))
        assert len(chunks) >= 1
        assert chunks[0]["contains_guidance"] is True

    def test_contains_guidance_english(self):
        c = "We expect gross margin around 53% next quarter. " * 5
        chunks = chunker.chunk_page(self._page(c))
        assert len(chunks) >= 1
        assert chunks[0]["contains_guidance"] is True

    def test_contains_number(self):
        c = "本季毛利率為 53%，營收 800 億元。" + "補充" * 30
        chunks = chunker.chunk_page(self._page(c))
        assert chunks[0]["contains_number"] is True

    def test_topic_ai_via_cowos(self):
        c = "CoWoS 產能與 AI 需求展望。" + "補充內容" * 30
        chunks = chunker.chunk_page(self._page(c))
        assert "AI" in chunks[0]["topics"]

    def test_topic_margin_recognised(self):
        c = "本季毛利率上揚至 53%。" + "補充內容" * 30
        chunks = chunker.chunk_page(self._page(c))
        assert "毛利率" in chunks[0]["topics"]


# ──────────────────────────────────────────────────────────────────────────
# chunk_page — metadata + chunk_index
# ──────────────────────────────────────────────────────────────────────────

class TestMetadataAndIndex:
    def test_metadata_propagated(self):
        page = {
            "content": "本季營運表現符合預期。" * 30,
            "has_table": False,
            "parse_method": "pdfplumber",
            "metadata": {
                "company": "台積電", "quarter": "2024Q3",
                "source_file": "TSMC 3Q24.pdf", "source_page": 5,
            },
        }
        chunks = chunker.chunk_page(page)
        assert len(chunks) >= 1
        for c in chunks:
            assert c["company"] == "台積電"
            assert c["quarter"] == "2024Q3"
            assert c["source_file"] == "TSMC 3Q24.pdf"
            assert c["source_page"] == 5

    def test_chunk_index_increments(self):
        # Force enough content to produce >= 2 chunks
        page = {
            "content": "一般敘述" * 600,
            "has_table": False,
            "parse_method": "pdfplumber",
            "metadata": {},
        }
        chunks = chunker.chunk_page(page)
        assert len(chunks) >= 2
        assert chunks[0]["chunk_index"] == 0
        assert chunks[1]["chunk_index"] == 1


# ──────────────────────────────────────────────────────────────────────────
# chunk_page — empty content + chunk_document
# ──────────────────────────────────────────────────────────────────────────

class TestEdges:
    def test_empty_content_returns_empty_list(self):
        assert chunker.chunk_page({"content": "   "}) == []

    def test_chunk_document_flattens(self):
        pages = [
            {"content": "" * 30, "metadata": {}},  # empty → skip
            {"content": "本季營運穩健。" * 30, "metadata": {}},
            {"content": "AI 需求強勁。" * 30, "metadata": {}},
        ]
        chunks = chunker.chunk_document(pages)
        # Flattens both non-empty pages
        assert len(chunks) >= 2


# ──────────────────────────────────────────────────────────────────────────
# Regression tests for the spec-audit bug fixes
# ──────────────────────────────────────────────────────────────────────────

class TestSplitQADelimiterAlignment:
    """Regression: _split_qa previously paired delimiters[i] with splits[i],
    dropping the first question and attaching every answer to the WRONG
    question marker. The fix pairs delimiters[i-1] with splits[i].
    """

    def test_first_question_prefix_preserved(self):
        """Text starting with a QA marker must keep the FIRST question's prefix."""
        # Build a single-QA-pair text using the `^[問答][:：]` pattern.
        # Each side must exceed MIN_CHUNK_LEN=50 chars after strip.
        q_body = "能否說明本季毛利率的展望以及對下季的指引方向？" * 4  # ~96 chars
        a_body = "我們對毛利率維持審慎樂觀，N3 良率提升是主因。" * 4  # ~96 chars
        text = "問：" + q_body + "\n" + "答：" + a_body
        chunks = chunker._split_qa(text)

        # Both the 問 chunk and 答 chunk should appear, each with the CORRECT prefix
        assert any(c.startswith("問：") for c in chunks), \
            f"問: prefix missing. chunks={chunks!r}"
        assert any(c.startswith("答：") for c in chunks), \
            f"答: prefix missing. chunks={chunks!r}"

    def test_answer_not_attached_to_wrong_marker(self):
        """An answer must not be prefixed with the NEXT question's marker."""
        # 問→答→問→答 sequence. Each segment long enough to survive MIN_CHUNK_LEN.
        ans_text = "本季毛利率符合預期，主因為產品組合改善與良率提升。" * 3
        q_text   = "請說明下季的營收展望與資本支出規劃。" * 3
        text = (
            "問：" + q_text + "\n"
            + "答：" + ans_text + "\n"
            + "問：" + q_text + "\n"
            + "答：" + ans_text
        )
        chunks = chunker._split_qa(text)

        # Pre-fix: chunks[0] was "答：" + q_text (first question's text with answer marker).
        # Post-fix: any chunk containing q_text content should start with 問:, not 答:
        for c in chunks:
            if "毛利率符合預期" in c:  # 答 content
                assert c.startswith("答："), \
                    f"答 content must keep 答 prefix, got: {c[:30]!r}"
            elif "下季的營收展望" in c:  # 問 content
                assert c.startswith("問："), \
                    f"問 content must keep 問 prefix, got: {c[:30]!r}"


class TestChunkIndexContiguous:
    """Regression: chunk_page used enumerate index for chunk_index, which
    left gaps when raw chunks were filtered below MIN_CHUNK_LEN. Fix uses
    a survivor counter.
    """

    def test_chunk_index_no_gaps_when_chunks_filtered(self, monkeypatch):
        """If the middle raw chunk is dropped, surviving chunks must have
        chunk_index = 0, 1 (not 0, 2)."""
        # Mock _sliding_window to return chunks with one short (will be filtered)
        # sandwiched between two long ones.
        def fake_sliding(text, *_a, **_k):
            return [
                "first long chunk " * 20,  # ~340 chars, kept
                "tiny",                    # < MIN_CHUNK_LEN=50, dropped
                "third long chunk " * 20,  # kept
            ]
        monkeypatch.setattr(chunker, "_sliding_window", fake_sliding)

        page = {
            "content": "opening session content " * 30,
            "has_table": False,
            "parse_method": "pdfplumber",
            "metadata": {"company": "X", "quarter": "2024Q1"},
        }
        chunks = chunker.chunk_page(page)

        # 2 chunks survive (first + third); indices must be contiguous
        assert len(chunks) == 2
        assert [c["chunk_index"] for c in chunks] == [0, 1]
