"""
Unit tests for src/ingestion/embedder.py.

Covers the gemini-embedding capability spec:
  - Model ID and output dimension constants
  - BATCH_SIZE = 1 (per-text SDK call)
  - Document vs query prompt prefixes
  - 429 / RESOURCE_EXHAUSTED retry with exponential backoff
  - Inter-call sleep (RPM throttle)
  - Client singleton via lru_cache
  - Deterministic UUID generation in upsert_chunks
  - UPSERT_BATCH = 100 batching and partial-failure handling
  - Empty input shortcut
"""
import re
import types

import pytest

from src.ingestion import embedder


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_model_id_locked(self):
        assert embedder.EMBEDDING_MODEL == "gemini-embedding-2"

    def test_embed_dim_locked(self):
        assert embedder.EMBED_DIM == 768

    def test_batch_size_is_one(self):
        # gemini-embedding-2 is multimodal — must call one text at a time
        assert embedder.BATCH_SIZE == 1


# ──────────────────────────────────────────────────────────────────────────
# Prompt prefixes (document vs query)
# ──────────────────────────────────────────────────────────────────────────

class _FakeEmbedding:
    def __init__(self, values):
        self.values = values


class _FakeEmbedResp:
    def __init__(self, n_vectors: int):
        self.embeddings = [_FakeEmbedding([0.0] * 768) for _ in range(n_vectors)]


def _install_fake_embed(monkeypatch, recorder: list):
    """Replace _embed_once so we can record the contents passed in without hitting the SDK."""
    def fake_once(client, batch_texts, config):
        recorder.append(list(batch_texts))
        return _FakeEmbedResp(len(batch_texts))
    monkeypatch.setattr(embedder, "_embed_once", fake_once)
    # Skip the client init path
    monkeypatch.setattr(embedder, "_get_client", lambda: object())
    # Disable sleeps to keep tests fast
    monkeypatch.setattr(embedder.time, "sleep", lambda *_a, **_k: None)


class TestPromptPrefixes:
    def test_document_prefix(self, monkeypatch):
        recorder: list = []
        _install_fake_embed(monkeypatch, recorder)

        embedder.embed_documents(["AI 需求強勁"])

        # 1 text → 1 SDK call → 1 batch
        assert len(recorder) == 1
        assert recorder[0] == ["title: none | text: AI 需求強勁"]

    def test_query_prefix(self, monkeypatch):
        recorder: list = []
        _install_fake_embed(monkeypatch, recorder)

        embedder.embed_query_texts(["毛利率"])

        assert recorder[0] == ["task: search result | query: 毛利率"]

    def test_embed_texts_alias_for_documents(self, monkeypatch):
        recorder: list = []
        _install_fake_embed(monkeypatch, recorder)

        embedder.embed_texts(["x"])

        # Backward-compat alias must use the document prefix
        assert recorder[0] == ["title: none | text: x"]

    def test_three_texts_three_sdk_calls(self, monkeypatch):
        recorder: list = []
        _install_fake_embed(monkeypatch, recorder)

        embedder.embed_documents(["a", "b", "c"])

        # BATCH_SIZE=1 → 3 separate SDK invocations
        assert len(recorder) == 3
        assert [r[0] for r in recorder] == [
            "title: none | text: a",
            "title: none | text: b",
            "title: none | text: c",
        ]

    def test_empty_input_returns_empty(self, monkeypatch):
        recorder: list = []
        _install_fake_embed(monkeypatch, recorder)

        out = embedder._embed([])

        assert out == []
        assert recorder == []


# ──────────────────────────────────────────────────────────────────────────
# 429 / RESOURCE_EXHAUSTED retry
# ──────────────────────────────────────────────────────────────────────────

def _make_client_error(*, code=None, status_code=None, msg="error"):
    """Build a real google.genai ClientError-shaped object without invoking the SDK constructor."""
    from google.genai import errors as genai_errors

    exc = genai_errors.ClientError.__new__(genai_errors.ClientError)
    Exception.__init__(exc, msg)
    if code is not None:
        exc.code = code
    if status_code is not None:
        exc.status_code = status_code
    return exc


class TestRetryBehavior:
    def test_429_retries_then_succeeds(self, monkeypatch):
        """First call raises 429, second succeeds → no raise."""
        calls = {"n": 0}

        class FakeClient:
            class models:
                @staticmethod
                def embed_content(*, model, contents, config):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise _make_client_error(code=429, msg="429 quota")
                    return _FakeEmbedResp(len(contents))

        monkeypatch.setattr(embedder.time, "sleep", lambda *_a, **_k: None)

        resp = embedder._embed_once(FakeClient(), ["title: none | text: x"], None)

        assert calls["n"] == 2
        assert len(resp.embeddings) == 1

    def test_resource_exhausted_message_triggers_retry(self, monkeypatch):
        """ClientError whose message contains RESOURCE_EXHAUSTED must be treated as 429."""
        calls = {"n": 0}

        class FakeClient:
            class models:
                @staticmethod
                def embed_content(*, model, contents, config):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise _make_client_error(msg="RESOURCE_EXHAUSTED quota exceeded")
                    return _FakeEmbedResp(len(contents))

        monkeypatch.setattr(embedder.time, "sleep", lambda *_a, **_k: None)

        resp = embedder._embed_once(FakeClient(), ["x"], None)
        assert calls["n"] == 2
        assert len(resp.embeddings) == 1

    def test_non_429_client_error_raises_immediately(self, monkeypatch):
        """A 401 ClientError must not enter the retry branch."""
        calls = {"n": 0}

        class FakeClient:
            class models:
                @staticmethod
                def embed_content(*, model, contents, config):
                    calls["n"] += 1
                    raise _make_client_error(code=401, msg="401 Unauthorized")

        monkeypatch.setattr(embedder.time, "sleep", lambda *_a, **_k: None)

        from google.genai import errors as genai_errors
        with pytest.raises(genai_errors.ClientError):
            embedder._embed_once(FakeClient(), ["x"], None)

        # Hard floor: only one attempt, no retry
        assert calls["n"] == 1

    def test_retry_exhausted_raises(self, monkeypatch):
        """If every attempt hits 429, the final error must propagate."""
        calls = {"n": 0}

        class FakeClient:
            class models:
                @staticmethod
                def embed_content(*, model, contents, config):
                    calls["n"] += 1
                    raise _make_client_error(code=429, msg="429 again")

        monkeypatch.setattr(embedder.time, "sleep", lambda *_a, **_k: None)
        # Shrink retry budget so the test is fast
        monkeypatch.setattr(embedder, "_RETRY_MAX", 2)

        from google.genai import errors as genai_errors
        with pytest.raises(genai_errors.ClientError):
            embedder._embed_once(FakeClient(), ["x"], None)

        # _RETRY_MAX=2 means: initial attempt + 2 retries = 3 total
        assert calls["n"] == 3


# ──────────────────────────────────────────────────────────────────────────
# Inter-call sleep (RPM throttle)
# ──────────────────────────────────────────────────────────────────────────

class TestInterCallSleep:
    def test_three_texts_insert_two_sleeps(self, monkeypatch):
        recorder_contents: list = []
        sleeps: list = []

        def fake_once(client, batch_texts, config):
            recorder_contents.append(list(batch_texts))
            return _FakeEmbedResp(len(batch_texts))

        monkeypatch.setattr(embedder, "_embed_once", fake_once)
        monkeypatch.setattr(embedder, "_get_client", lambda: object())
        monkeypatch.setattr(embedder.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(embedder, "_INTER_CALL_SLEEP", 0.6)

        embedder._embed(["a", "b", "c"])

        # Sleep between 1→2 and 2→3, NOT after the last call
        assert sleeps == [0.6, 0.6]

    def test_single_text_no_sleep(self, monkeypatch):
        sleeps: list = []
        monkeypatch.setattr(embedder, "_embed_once",
                            lambda *a, **kw: _FakeEmbedResp(1))
        monkeypatch.setattr(embedder, "_get_client", lambda: object())
        monkeypatch.setattr(embedder.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(embedder, "_INTER_CALL_SLEEP", 0.6)

        embedder._embed(["only"])

        assert sleeps == []

    def test_zero_sleep_disabled(self, monkeypatch):
        sleeps: list = []
        monkeypatch.setattr(embedder, "_embed_once",
                            lambda *a, **kw: _FakeEmbedResp(1))
        monkeypatch.setattr(embedder, "_get_client", lambda: object())
        monkeypatch.setattr(embedder.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(embedder, "_INTER_CALL_SLEEP", 0)

        embedder._embed(["a", "b", "c"])

        assert sleeps == []


# ──────────────────────────────────────────────────────────────────────────
# upsert_chunks — deterministic IDs + batching
# ──────────────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class _FakeBQClient:
    def __init__(self, errors_for_batch_idx: dict | None = None):
        self.inserted_batches: list[list[dict]] = []
        self._errors_for_batch_idx = errors_for_batch_idx or {}

    def insert_rows_json(self, table_id, batch):
        idx = len(self.inserted_batches)
        self.inserted_batches.append(batch)
        return self._errors_for_batch_idx.get(idx, [])


def _install_bq_fakes(monkeypatch, fake_client):
    monkeypatch.setattr(embedder, "get_bq_client", lambda: fake_client)
    monkeypatch.setattr(embedder, "ensure_dataset_and_table", lambda _c: None)
    monkeypatch.setattr(embedder, "get_table_path", lambda: "project.dataset.table")
    # Avoid network embeddings — replace embed_texts wholesale
    monkeypatch.setattr(
        embedder, "embed_texts",
        lambda texts: [[0.0] * embedder.EMBED_DIM for _ in texts]
    )


class TestUpsertChunks:
    def _chunk(self, **overrides):
        base = {
            "company": "TSMC",
            "quarter": "2024Q1",
            "section": "discussion",
            "content": "x",
            "source_file": "TSMC 1Q24 Transcript.pdf",
            "source_page": 5,
            "chunk_index": 0,
        }
        base.update(overrides)
        return base

    def test_empty_input_short_circuits(self, monkeypatch):
        fake = _FakeBQClient()
        _install_bq_fakes(monkeypatch, fake)

        out = embedder.upsert_chunks([])

        assert out == 0
        assert fake.inserted_batches == []

    def test_deterministic_id_same_seed(self, monkeypatch):
        fake = _FakeBQClient()
        _install_bq_fakes(monkeypatch, fake)

        c1 = self._chunk(content="alpha")
        c2 = self._chunk(content="beta")  # different content
        embedder.upsert_chunks([c1, c2])

        rows = fake.inserted_batches[0]
        # Same source_file / source_page / chunk_index → same ID
        assert rows[0]["id"] == rows[1]["id"]

    def test_id_is_uuid_shape(self, monkeypatch):
        fake = _FakeBQClient()
        _install_bq_fakes(monkeypatch, fake)

        embedder.upsert_chunks([self._chunk()])

        row = fake.inserted_batches[0][0]
        assert _UUID_RE.match(row["id"]) is not None

    def test_different_seed_different_id(self, monkeypatch):
        fake = _FakeBQClient()
        _install_bq_fakes(monkeypatch, fake)

        c1 = self._chunk(chunk_index=0)
        c2 = self._chunk(chunk_index=1)
        embedder.upsert_chunks([c1, c2])

        ids = [r["id"] for r in fake.inserted_batches[0]]
        assert ids[0] != ids[1]

    def test_250_chunks_three_batches(self, monkeypatch):
        fake = _FakeBQClient()
        _install_bq_fakes(monkeypatch, fake)

        chunks = [self._chunk(chunk_index=i) for i in range(250)]
        out = embedder.upsert_chunks(chunks, show_progress=False)

        # 100 + 100 + 50
        assert len(fake.inserted_batches) == 3
        assert [len(b) for b in fake.inserted_batches] == [100, 100, 50]
        assert out == 250

    def test_partial_failure_other_batches_count(self, monkeypatch, capsys):
        fake = _FakeBQClient(errors_for_batch_idx={1: [{"reason": "boom"}]})
        _install_bq_fakes(monkeypatch, fake)

        chunks = [self._chunk(chunk_index=i) for i in range(200)]
        out = embedder.upsert_chunks(chunks, show_progress=False)

        # First batch (100) succeeded; second batch (100) errored
        assert out == 100
        captured = capsys.readouterr()
        assert "BigQuery 寫入錯誤" in captured.out


# ──────────────────────────────────────────────────────────────────────────
# Client singleton
# ──────────────────────────────────────────────────────────────────────────

class TestClientSingleton:
    def test_get_client_returns_same_object(self, monkeypatch):
        """Two calls in a row return the same client (lru_cache)."""
        from google import genai

        sentinel = object()
        monkeypatch.setattr(genai, "Client", lambda **kw: sentinel)
        # Clear lru cache so the new monkeypatch takes effect
        embedder._get_client.cache_clear()

        c1 = embedder._get_client()
        c2 = embedder._get_client()

        assert c1 is c2
        embedder._get_client.cache_clear()

    def test_missing_key_raises(self, monkeypatch):
        """No GEMINI_API_KEY and no GOOGLE_API_KEY → RuntimeError."""
        # Override the auto-stubbed env in conftest
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setattr(embedder, "get_secret", lambda _k: None)
        embedder._get_client.cache_clear()

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            embedder._get_client()

        embedder._get_client.cache_clear()
