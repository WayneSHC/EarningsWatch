# Feature Specification: Retrieval & Coverage Sweep

**Feature Branch**: `002-retrieval-coverage`

**Created**: 2026-05-22

**Status**: Draft (as-built — reverse-engineered from `src/core/retriever.py`)

**Input**: User description: "As-built spec for the Retrieval and Coverage Sweep subsystem of EarningsWatch — vector search, Cohere rerank, HyDE query expansion, per-quarter coverage sweep with score gate, and safe env-driven configuration."

> **Documentation drift flagged**: `CLAUDE.md` and `.specify/memory/constitution.md` describe the vector DB as Qdrant. The actual `src/core/retriever.py` uses **BigQuery Vector Search** (`VECTOR_SEARCH(TABLE …, 'embedding', …, distance_type => 'COSINE')`) with Cohere rerank. The CLAUDE.md text needs an update; this spec describes what the code actually does today.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Retrieve and rank topic-relevant chunks (Priority: P1)

A user (or an upstream agent node) supplies a natural-language query plus optional filters (company, quarters, transcript section). The system returns the top-N transcript chunks ranked by relevance to that query, suitable for downstream LLM analysis.

**Why this priority**: Retrieval quality is the upper bound on every other capability — bad chunks in, bad contradictions out.

**Independent Test**: Issue a query with a known-relevant fact in the corpus, with filter `company=TSMC`. Verify the top result contains text relevant to the query and that its `payload.company` is TSMC.

**Acceptance Scenarios**:

1. **Given** a query and no filters, **When** retrieve runs, **Then** up to `TOP_K_RERANK` results are returned, each with `id`, `score` (cosine similarity), `payload`, and (if rerank succeeded) `rerank_score`.
2. **Given** a query and a `company` filter, **When** retrieve runs, **Then** every returned chunk's `payload.company` equals the filter value.
3. **Given** a query and a list of `quarters` filter, **When** retrieve runs, **Then** every returned chunk's `payload.quarter` is in that list.
4. **Given** the Cohere API key is missing or the call fails, **When** retrieve runs, **Then** the system degrades to vector-search order (no rerank) without raising.

---

### User Story 2 — Backfill missing quarters via coverage sweep (Priority: P1)

After the initial top-K retrieval, some quarters that should have results are missing (the top-K was dominated by other quarters). The system runs a single follow-up query that returns the top-K-per-quarter for exactly those missing quarters in **one** vector search — not one query per quarter — and keeps only chunks above a similarity floor so irrelevant low-score noise is excluded.

**Why this priority**: Without coverage sweep, multi-quarter comparisons get N-shaped gaps that prevent the contradiction detector from running on the missing quarters. Without the score gate, the system fills those gaps with off-topic chunks (legal disclaimers, generic boilerplate) that degrade analysis quality.

**Independent Test**: Provide `missing_quarters=["2024Q1","2024Q2","2024Q3"]`. Verify exactly one vector_search SQL is executed (via a logging spy or query count) and that the returned dict contains entries for the missing quarters that have above-threshold content, with up to `top_k_per_quarter` chunks each.

**Acceptance Scenarios**:

1. **Given** 3 missing quarters all with on-topic content, **When** coverage sweep runs, **Then** the returned dict has 3 keys, each containing up to `top_k_per_quarter` chunks ordered by relevance.
2. **Given** a quarter whose chunks all have similarity below the score gate, **When** coverage sweep runs, **Then** that quarter is omitted from the result and a warning is logged.
3. **Given** more than `max_quarters` (default 8) missing quarters, **When** coverage sweep runs, **Then** only the most-recent `max_quarters` are queried (chronologically-trimmed).
4. **Given** `missing_quarters=[]`, **When** coverage sweep runs, **Then** an empty dict is returned with zero DB calls.

---

### User Story 3 — Safe configuration of the coverage score gate (Priority: P2)

The coverage-sweep score gate is environment-driven (`COVERAGE_MIN_SCORE`). A typo, a non-numeric value, or an out-of-range value MUST NOT crash retrieval — it MUST fall back to the documented default with a warning, because retrieval is on the critical read path and a single bad env var must not take down the service.

**Why this priority**: Constitution Principle II (fault tolerance) and the Tech & Architecture Constraints both require safe env-var fallback.

**Independent Test**: Set `COVERAGE_MIN_SCORE=abc`, then `=1.5`, then `=-0.1`, then unset. In each case verify `_load_min_score_from_env()` returns the default (0.25) and prints a warning (for the invalid cases).

**Acceptance Scenarios**:

1. **Given** `COVERAGE_MIN_SCORE` unset, **When** the value is read, **Then** the default 0.25 is returned with no warning.
2. **Given** `COVERAGE_MIN_SCORE="0.4"`, **When** the value is read, **Then** 0.4 is returned.
3. **Given** `COVERAGE_MIN_SCORE="abc"`, **When** the value is read, **Then** the default is returned with a warning identifying the bad input.
4. **Given** `COVERAGE_MIN_SCORE="1.5"`, **When** the value is read, **Then** the default is returned with a range warning.
5. **Given** `retrieve_coverage` is called with explicit `min_score=0.3`, **When** it runs, **Then** the environment variable is ignored and 0.3 is used.

---

### User Story 4 — Boost recall with HyDE query expansion (Priority: P3)

For short or terse queries, the system can optionally generate a hypothetical earnings-call-style answer first (80–150 chars, Chinese), embed *that* instead of the raw query, and use it for vector search — improving recall by aligning the embedding vocabulary with target chunks. The feature is gated by `LLM_HYDE_ENABLED`, off by default, and only runs for queries above a minimum length.

**Why this priority**: Useful quality enhancement but not on the critical path; off by default.

**Independent Test**: Set `LLM_HYDE_ENABLED=true`. Issue the same query twice; verify the expansion is generated once (cached) and that the embedded text is the expanded version, not the raw query.

**Acceptance Scenarios**:

1. **Given** `LLM_HYDE_ENABLED=false` (default), **When** any query is issued, **Then** no HyDE LLM call is made.
2. **Given** `LLM_HYDE_ENABLED=true` and a query shorter than the minimum length, **When** retrieval runs, **Then** no expansion is done.
3. **Given** `LLM_HYDE_ENABLED=true` and a sufficiently long query, **When** the same query is issued twice, **Then** the LLM expansion is called only once (LRU cached).
4. **Given** HyDE is enabled but the LLM call fails, **When** retrieval runs, **Then** the raw query is used (graceful degradation) and the user sees no error.

---

### User Story 5 — Filter pushdown for vector search (Priority: P2)

When the user provides company/quarter/section filters, those filters MUST be applied **inside** the `VECTOR_SEARCH` base-table query, not as an outer-`WHERE` post-filter. This prevents the failure mode where the inner top-K=20 already excludes filter-matching rows, leaving the outer filter with 0 results.

**Why this priority**: A correctness fix for a real recall bug discovered earlier in the project (see commit `9d6ecc0 fix(retriever): push filter into VECTOR_SEARCH to avoid BadRequest`).

**Independent Test**: With `company=TSMC` filter against a corpus where TSMC chunks are not in the inner top-20 by raw vector distance, verify retrieve() still returns TSMC results. (A naive post-filter implementation would return 0 results in this case.)

**Acceptance Scenarios**:

1. **Given** filters provided, **When** vector_search runs, **Then** the SQL uses `base.<column>` references inside the `VECTOR_SEARCH` clause (via the inner `WHERE base.company = @company …`) AND uses a widened inner top-K (at least 200) so filter recall is preserved.
2. **Given** no filters, **When** vector_search runs, **Then** the inner WHERE evaluates to `TRUE` and the standard inner top-K applies.

---

### Edge Cases

- **No filters supplied** — `_build_where_clause` returns `1=1` and the query runs against the whole corpus.
- **Empty candidate list to rerank** — `rerank` returns the empty list unchanged; no Cohere call is made.
- **Cohere client unavailable** — `_get_cohere_client` returns `None`; rerank degrades to returning the first `top_n` candidates in vector-search order.
- **Coverage sweep produces zero hits for a quarter** — quarter is omitted from result; warning logged with the active `min_score`.
- **`missing_quarters` larger than `max_quarters`** — trimmed to the latest `max_quarters` (lexicographic sort on quarter key acts as chronological sort given `YYYYQn` format).
- **HyDE LLM failure** — silently degrades to the raw query; warning logged with exception type only (not message, to avoid leaking provider details).
- **Explicit `min_score` passed to `retrieve_coverage`** — overrides the env var without warning (intentional API).

## Requirements *(mandatory)*

### Functional Requirements

**Vector search**

- **FR-001**: System MUST embed the query through `embed_query_texts` (single-element batch) and perform a cosine-distance VECTOR_SEARCH against the configured table.
- **FR-002**: When `company`, `quarters`, or `section` filters are supplied, the filters MUST be applied inside the `VECTOR_SEARCH` clause via `base.<column>` references — not as an outer post-filter.
- **FR-003**: When filters are supplied, the inner `top_k` MUST be widened (≥ 20× the caller's `top_k`, minimum 200) so filter recall is preserved.
- **FR-004**: Returned hits MUST be sorted by cosine distance (ascending) and converted to cosine similarity (`score = 1 - distance`) before returning.
- **FR-005**: Each returned hit MUST be `{id, score, payload}` where payload contains `company`, `quarter`, `section`, `content`, `source_file`, `source_page`, `chunk_index`.

**Rerank**

- **FR-006**: When a Cohere API key is available and candidates is non-empty, `rerank` MUST call Cohere's `rerank-v3.5` model with the candidate documents and the original query.
- **FR-007**: When the Cohere client cannot be created (missing key) or candidates is empty, `rerank` MUST return the first `top_n` candidates unchanged.
- **FR-008**: Reranked items MUST be returned with an added `rerank_score` field; the underlying `score` from vector search MUST NOT be mutated.

**Coverage sweep**

- **FR-009**: `retrieve_coverage` MUST execute **at most one** VECTOR_SEARCH SQL regardless of how many quarters are in `missing_quarters` (no per-quarter loop of DB calls).
- **FR-010**: The SQL MUST use `ROW_NUMBER() OVER(PARTITION BY base.quarter ORDER BY distance)` to keep `top_k_per_quarter × oversample_factor` candidates per quarter, then filter `rn <= fetch_k`.
- **FR-011**: System MUST exclude any hit whose distance exceeds `1 - min_score` (i.e. similarity below the gate).
- **FR-012**: When more than `max_quarters` (default 8) missing quarters are requested, system MUST keep only the most-recent `max_quarters` (after lexicographic sort).
- **FR-013**: When `use_rerank=True` and a quarter has more candidates than `top_k_per_quarter`, the per-quarter candidates MUST be reranked before truncation.
- **FR-014**: Quarters with zero hits after the score gate MUST be omitted from the result dict with a warning identifying the active `min_score`.
- **FR-015**: `missing_quarters=[]` MUST short-circuit with an empty result and zero DB calls.

**Safe configuration**

- **FR-016**: `_load_min_score_from_env` MUST return `_DEFAULT_MIN_SCORE` (0.25) when `COVERAGE_MIN_SCORE` is unset, empty, non-float, or outside `[0.0, 1.0]`, and MUST log a warning identifying the invalid input.
- **FR-017**: An explicit `min_score` argument to `retrieve_coverage` MUST override the env-var lookup entirely (no warning, no fallback).

**HyDE query expansion**

- **FR-018**: HyDE expansion MUST be disabled by default and enabled only when `LLM_HYDE_ENABLED` is set to one of `true` / `1` / `yes` (case-insensitive, whitespace-trimmed).
- **FR-019**: When enabled, HyDE MUST run only for queries whose stripped length ≥ `_HYDE_MIN_QUERY_LEN`.
- **FR-020**: `_hyde_expand` MUST be LRU-cached (size 128) keyed on the raw query.
- **FR-021**: HyDE LLM failures MUST fall back to the original query (graceful degradation) with a warning that includes the exception type but not its message.

**Company quarter listing**

- **FR-022**: `get_company_quarters` MUST return the distinct, sorted list of quarters present for the given company in the configured table, with `NULL` quarters excluded.

### Key Entities

- **Hit** — `{id: str, score: float, payload: dict}` where `payload` carries chunk metadata. The unit returned by vector_search/retrieve/coverage.
- **Filter** — Optional `company`, `quarters` (list), `section` triplet. Used to scope retrieval.
- **Coverage Map** — `dict[quarter → list[Hit]]` returned by `retrieve_coverage`; only quarters with above-gate content appear.
- **HyDE Expansion** — A hypothetical earnings-call-style answer (80–150 Chinese chars) used as the embedding source in place of the raw query.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For the benchmark suite, when a filter is supplied that matches < 1% of the corpus by raw vector distance, `retrieve()` MUST still return matching results (recall > 0% verified against an inner-top-K=20 naive baseline that returns 0).
- **SC-002**: `retrieve_coverage` issues exactly **1** DB query for any non-empty `missing_quarters` list.
- **SC-003**: `_load_min_score_from_env` returns the default for **100%** of invalid inputs (unset, empty, non-float, out-of-range) without raising.
- **SC-004**: With HyDE disabled, **0** LLM calls are made per retrieval. With HyDE enabled and identical queries, the LLM is called **at most once per unique query**.
- **SC-005**: When the Cohere API key is absent or the call fails, retrieve still returns results (graceful degradation verified by integration test).
- **SC-006**: Quarters whose chunks all fall below `min_score` are **0%** present in the returned coverage map (no noise leakage).
- **SC-007**: When `missing_quarters=[]`, the function returns within < 1 ms (no DB call).

## Assumptions

- The vector store is BigQuery Vector Search (`VECTOR_SEARCH(... distance_type => 'COSINE')`), not Qdrant. CLAUDE.md and constitution refer to Qdrant; documentation drift will be fixed under the audit phase.
- `embed_query_texts` returns a 768-or-similar dimensional FLOAT64 vector compatible with the configured table's `embedding` column.
- Quarter keys are `YYYYQn` strings so lexicographic sort matches chronological order — same assumption as spec 001.
- The corpus is small enough that an inner top-K of 200 (or 20× the caller's top-K) is a reasonable widening that doesn't blow up cost; if the corpus grows materially, this may need revisiting.
- Cohere's `rerank-v3.5` model is the project's chosen reranker; switching reranker would be a substantial change requiring spec revision.
- HyDE expansion uses the project's `chat()` LLM cascade in `mode="dev"` (cheaper model); this assumes the cascade has at least one configured backend.
- Constitution Principle II (fault tolerance), III (efficiency: bounded work, parallelism, caching), and the "safe fallback on invalid values" tech constraint are all mandatory acceptance criteria for any change to this subsystem.
