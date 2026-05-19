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
