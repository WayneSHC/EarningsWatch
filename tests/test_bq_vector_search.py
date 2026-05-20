"""
Unit tests for bq-vector-search capability.

Covers:
  - bq_client.get_bq_client singleton + get_table_path format
  - ensure_dataset_and_table is idempotent (skips when present)
  - vector_search builds SQL with base.X filter pushdown
  - vector_search converts distance -> similarity (score = 1 - distance)
  - rerank short-circuits when no Cohere client or empty candidates
  - retrieve_coverage:
      * empty missing_quarters returns {} without BQ call
      * over max_quarters → truncates to most recent
      * missing-quarter warning is printed when no chunks meet min_score
"""
import pytest

from src.core import bq_client as bqc_mod
from src.core import retriever


# ──────────────────────────────────────────────────────────────────────────
# BigQuery client singleton + path
# ──────────────────────────────────────────────────────────────────────────

class TestBqClient:
    def test_get_bq_client_is_singleton(self, monkeypatch):
        """Two calls return the same client (lru_cache)."""
        from google.cloud import bigquery

        sentinel = object()
        monkeypatch.setattr(bigquery, "Client", lambda **kw: sentinel)
        bqc_mod.get_bq_client.cache_clear()

        c1 = bqc_mod.get_bq_client()
        c2 = bqc_mod.get_bq_client()

        assert c1 is c2
        bqc_mod.get_bq_client.cache_clear()

    def test_get_table_path_format(self):
        path = bqc_mod.get_table_path()
        # project.dataset.table — three segments
        assert path.count(".") == 2
        assert path.endswith(".earnings_data.earnings_calls")


# ──────────────────────────────────────────────────────────────────────────
# ensure_dataset_and_table — idempotent
# ──────────────────────────────────────────────────────────────────────────

class _FakeBqClient:
    """Captures create_* / get_* calls without hitting BigQuery."""

    def __init__(self, *, dataset_exists=True, table_exists=True):
        self.dataset_exists = dataset_exists
        self.table_exists = table_exists
        self.create_dataset_calls = 0
        self.create_table_calls = 0
        self.last_create_table_arg = None

    def get_dataset(self, ref):
        if not self.dataset_exists:
            raise RuntimeError("404 Not Found")
        return ref

    def create_dataset(self, ref):
        self.create_dataset_calls += 1
        self.dataset_exists = True

    def get_table(self, ref):
        if not self.table_exists:
            raise RuntimeError("404 Not Found")
        return ref

    def create_table(self, table):
        self.create_table_calls += 1
        self.last_create_table_arg = table


class TestEnsureDatasetAndTable:
    def test_skips_when_both_exist(self, capsys):
        fake = _FakeBqClient(dataset_exists=True, table_exists=True)
        bqc_mod.ensure_dataset_and_table(fake)

        assert fake.create_dataset_calls == 0
        assert fake.create_table_calls == 0
        captured = capsys.readouterr()
        assert "Dataset earnings_data 已存在" in captured.out
        assert "Table earnings_calls 已存在" in captured.out

    def test_creates_missing_table_with_embedding_repeated(self):
        fake = _FakeBqClient(dataset_exists=True, table_exists=False)
        bqc_mod.ensure_dataset_and_table(fake)

        assert fake.create_table_calls == 1
        table = fake.last_create_table_arg
        embedding_field = next(
            (f for f in table.schema if f.name == "embedding"), None
        )
        assert embedding_field is not None
        assert embedding_field.field_type == "FLOAT64"
        assert embedding_field.mode == "REPEATED"

    def test_creates_missing_dataset(self):
        fake = _FakeBqClient(dataset_exists=False, table_exists=True)
        bqc_mod.ensure_dataset_and_table(fake)

        assert fake.create_dataset_calls == 1


# ──────────────────────────────────────────────────────────────────────────
# vector_search — SQL composition + score conversion
# ──────────────────────────────────────────────────────────────────────────

class _FakeRow:
    """Mock for the row returned by BigQuery client.query.result()."""

    def __init__(self, distance, **payload):
        self.distance = distance
        for k, v in payload.items():
            setattr(self, k, v)


class _CapturingBqClient:
    """Records each SQL/job_config pair without executing it."""

    def __init__(self, rows=None):
        self.queries: list[tuple[str, object]] = []
        self._rows = rows or []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))

        rows = self._rows

        class _QJ:
            def result(self):
                return rows

        return _QJ()


def _install_retriever_fakes(monkeypatch, fake_client, embed_vector=None):
    monkeypatch.setattr(retriever, "get_bq_client", lambda: fake_client)
    monkeypatch.setattr(retriever, "get_table_path",
                        lambda: "p.d.earnings_calls")
    # Skip real embedding work
    monkeypatch.setattr(retriever, "embed_query",
                        lambda q: embed_vector or [0.0] * 768)
    # Skip Cohere rerank
    monkeypatch.setattr(retriever, "_get_cohere_client", lambda: None)
    # Skip HyDE
    monkeypatch.setattr(retriever, "_maybe_expand", lambda q: q)


class TestVectorSearch:
    def test_sql_contains_base_company_filter(self, monkeypatch):
        fake = _CapturingBqClient(rows=[])
        _install_retriever_fakes(monkeypatch, fake)

        retriever.vector_search("AI 需求", company="台積電", top_k=5)

        sql = fake.queries[0][0]
        assert "base.company = @company" in sql

    def test_sql_contains_quarters_unnest(self, monkeypatch):
        fake = _CapturingBqClient(rows=[])
        _install_retriever_fakes(monkeypatch, fake)

        retriever.vector_search(
            "AI 需求", quarters=["2024Q1", "2024Q2"], top_k=5
        )

        sql = fake.queries[0][0]
        assert "base.quarter IN UNNEST(@quarters)" in sql

    def test_sql_no_filter_uses_true(self, monkeypatch):
        fake = _CapturingBqClient(rows=[])
        _install_retriever_fakes(monkeypatch, fake)

        retriever.vector_search("AI 需求", top_k=5)

        sql = fake.queries[0][0]
        # Outer WHERE TRUE when no filters supplied
        assert "WHERE TRUE" in sql

    def test_inner_top_k_at_least_200(self, monkeypatch):
        """Inner top_k = max(top_k * 20, 200) — protects recall after filtering."""
        fake = _CapturingBqClient(rows=[])
        _install_retriever_fakes(monkeypatch, fake)

        retriever.vector_search("AI 需求", top_k=5)
        sql = fake.queries[0][0]
        assert "top_k => 200" in sql

    def test_inner_top_k_scales_with_outer(self, monkeypatch):
        fake = _CapturingBqClient(rows=[])
        _install_retriever_fakes(monkeypatch, fake)

        # 20 * 20 = 400 > 200 → inner k = 400
        retriever.vector_search("AI 需求", top_k=20)
        sql = fake.queries[0][0]
        assert "top_k => 400" in sql

    def test_distance_converted_to_similarity_score(self, monkeypatch):
        fake = _CapturingBqClient(rows=[
            _FakeRow(
                distance=0.2,
                id="r1", company="台積電", quarter="2024Q1",
                section="discussion", content="AI", source_file="f.pdf",
                source_page=1, chunk_index=0,
            ),
        ])
        _install_retriever_fakes(monkeypatch, fake)

        hits = retriever.vector_search("AI", top_k=5)

        assert len(hits) == 1
        # cosine similarity = 1 - distance
        assert hits[0]["score"] == pytest.approx(0.8)
        assert hits[0]["id"] == "r1"
        assert hits[0]["payload"]["company"] == "台積電"


# ──────────────────────────────────────────────────────────────────────────
# rerank — short-circuit paths
# ──────────────────────────────────────────────────────────────────────────

class TestRerank:
    def test_no_cohere_client_returns_top_n(self, monkeypatch):
        monkeypatch.setattr(retriever, "_get_cohere_client", lambda: None)
        candidates = [
            {"id": str(i), "payload": {"content": f"c{i}"}} for i in range(10)
        ]
        out = retriever.rerank("q", candidates, top_n=3)
        assert len(out) == 3
        assert out == candidates[:3]

    def test_empty_candidates_returns_empty(self, monkeypatch):
        # Even with a "real" client this should not call SDK
        monkeypatch.setattr(retriever, "_get_cohere_client",
                            lambda: object())
        out = retriever.rerank("q", [], top_n=5)
        assert out == []

    def test_cohere_error_degrades_to_vector_order(self, monkeypatch, capsys):
        """Regression: a Cohere API failure (e.g. 429 Trial-key rate limit)
        must NOT propagate — rerank is a refinement step, vector_search
        already produced a ranked list. On failure, return candidates[:top_n]
        in their original (similarity) order instead of collapsing the whole
        retrieval."""
        class _BoomCohere:
            def rerank(self, **kwargs):
                raise RuntimeError(
                    "status_code: 429 — You are using a Trial key, "
                    "limited to 10 API calls / minute"
                )

        monkeypatch.setattr(retriever, "_get_cohere_client", lambda: _BoomCohere())
        candidates = [
            {"id": str(i), "payload": {"content": f"c{i}"}} for i in range(10)
        ]

        out = retriever.rerank("q", candidates, top_n=5)

        # Degrades to vector-search order, does not raise
        assert out == candidates[:5]
        captured = capsys.readouterr()
        assert "rerank 失敗" in captured.out
        assert "降級" in captured.out

    def test_successful_rerank_attaches_score(self, monkeypatch):
        """Happy path still works: reranked items keep original fields + rerank_score."""
        class _FakeResult:
            def __init__(self, index, score):
                self.index = index
                self.relevance_score = score

        class _FakeCohere:
            def rerank(self, **kwargs):
                # Return indices in a reordered sequence
                return type("R", (), {"results": [
                    _FakeResult(2, 0.9), _FakeResult(0, 0.7),
                ]})()

        monkeypatch.setattr(retriever, "_get_cohere_client", lambda: _FakeCohere())
        candidates = [
            {"id": str(i), "payload": {"content": f"c{i}"}} for i in range(5)
        ]

        out = retriever.rerank("q", candidates, top_n=2)

        assert len(out) == 2
        assert out[0]["id"] == "2" and out[0]["rerank_score"] == 0.9
        assert out[1]["id"] == "0" and out[1]["rerank_score"] == 0.7


# ──────────────────────────────────────────────────────────────────────────
# retrieve_coverage — empty / over-cap / no-results-warning
# ──────────────────────────────────────────────────────────────────────────

class TestRetrieveCoverage:
    def test_empty_missing_quarters_short_circuits(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(retriever, "get_bq_client",
                            lambda: _CapturingBqClient([]))

        out = retriever.retrieve_coverage("q", "台積電", [])

        assert out == {}

    def test_over_max_quarters_truncates_to_most_recent(self, monkeypatch, capsys):
        fake = _CapturingBqClient(rows=[])
        _install_retriever_fakes(monkeypatch, fake)

        many = [f"2024Q{i}" for i in range(1, 11)]  # 10 quarters
        retriever.retrieve_coverage(
            "q", "台積電", many, max_quarters=3
        )

        captured = capsys.readouterr()
        assert "超過 3 季" in captured.out

        # Find the @quarters parameter in the captured job_config
        job_config = fake.queries[0][1]
        quarters_param = next(
            (p for p in job_config.query_parameters if p.name == "quarters"),
            None,
        )
        assert quarters_param is not None
        # sorted(many)[-3:] = last 3 alphabetically
        assert list(quarters_param.values) == sorted(many)[-3:]

    def test_missing_results_quarter_is_warned_and_omitted(
            self, monkeypatch, capsys):
        """If BQ returns 0 rows for a quarter, log a warning and skip it."""
        fake = _CapturingBqClient(rows=[])  # no rows back
        _install_retriever_fakes(monkeypatch, fake)

        out = retriever.retrieve_coverage(
            "q", "台積電", ["2024Q1", "2024Q2"], min_score=0.4
        )

        assert out == {}
        captured = capsys.readouterr()
        assert "分數不足 0.4" in captured.out
