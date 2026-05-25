<!--
SYNC IMPACT REPORT
==================
Version change: 1.0.0 → 1.0.1
Bump rationale: PATCH — clarification + correction. No principles added or redefined.
  (a) Principle I now names `src/ingestion/` as a fourth shared-dependency layer that
      core MAY depend on; this matches existing code (retriever imports embedder).
  (b) Technology & Architecture Constraints corrected: the vector store is BigQuery
      Vector Search, not Qdrant. The original v1.0.0 wording carried over from a
      Qdrant-era CLAUDE.md that is itself being updated in the same change set.

Previous changes (v0 → 1.0.0):
  - Initial ratification. Six principles derived from CLAUDE.md sections a-f.

Modified principles:
  - I. Layered Architecture — added ingestion-layer paragraph
Added sections: (none)
Removed sections: (none)

Templates requiring updates:
  ✅ .specify/templates/plan-template.md
  ✅ .specify/templates/spec-template.md
  ✅ .specify/templates/tasks-template.md
  ⚠ CLAUDE.md — multiple Qdrant references being updated in same commit

Follow-up TODOs:
  - RATIFICATION_DATE remains 2026-05-22 (v1.0.0 ratification date).
-->

# EarningsWatch Constitution

EarningsWatch is a Python RAG application that detects tone/stance contradictions across
corporate earnings calls. It is built as a three-layer system: a Streamlit UI, a LangGraph
7-node agent, and a Core layer (LLM client, BigQuery client, retriever, contradiction
detection), with a supporting Ingestion layer that produces the corpus the Core layer reads.
This constitution codifies the non-negotiable engineering standards that every change MUST
satisfy.

## Core Principles

### I. Layered Architecture (程式架構合理性)

The codebase MUST preserve a strict layered separation:

- **UI layer** (`src/ui/`) — Streamlit pages, charts, export. MUST NOT contain business logic.
- **Agent layer** (`src/agent/`) — LangGraph 7-node orchestration only. MUST NOT call the
  vector store directly; it delegates retrieval to the Core layer.
- **Core layer** (`src/core/`) — LLM client, BigQuery client, retriever, contradiction
  detection. Each module MUST be independently testable.
- **Ingestion layer** (`src/ingestion/`) — corpus preparation: PDF parsing, chunking,
  embedding. Treated as a shared dependency layer: **Core MAY import from Ingestion**
  (e.g. `embed_query_texts`); the reverse is prohibited. UI and Agent MUST NOT depend on
  Ingestion directly.

Each layer has a single responsibility and MUST NOT reach into the internals of another.
Shared resources MUST use the singleton accessor (e.g. `get_bq_client()` via `lru_cache`) —
direct client instantiation is prohibited. Inter-node state MUST flow through the typed
`AgentState` TypedDict.

Rationale: Single-responsibility layers keep modules unit-testable and prevent the coupling
that makes RAG pipelines brittle.

### II. Fault Tolerance & Defensive Programming (程式容錯與防呆)

External calls MUST fail soft, never crash the pipeline:

- LLM JSON parsing MUST use layered fallback (direct parse → markdown fence → greedy `{}` →
  degrade to `confidence=0`).
- Each independent unit of batch work (e.g. a quarter-pair in `batch_detect`, a retrieval
  source in `parallel_retrieval`) MUST be wrapped in its own try/except so one failure does
  not abort the batch.
- External-service calls (LLM backends, Tavily, yfinance, BigQuery) MUST degrade gracefully:
  backend fallback cascade, cached-result fallback, or hardcoded fallback as last resort.
- Inputs at function boundaries MUST be type-validated; invalid arguments MUST raise early
  with a clear error rather than failing deep in the call stack.

Rationale: A RAG agent depends on many flaky external services; partial failure MUST yield
partial results, not a blank screen.

### III. Efficiency by Design (程式效率)

Performance-sensitive paths MUST apply the established techniques:

- Independent I/O MUST run concurrently (`ThreadPoolExecutor`) — retrieval sources in
  parallel, multi-company agents in parallel.
- Repeated expensive lookups MUST be cached (`@st.cache_data` with TTL, `lru_cache`,
  session-state byte caches).
- LLM prompt content MUST be bounded (per-quarter content truncation) to cap cost and
  latency.
- Low-value work MUST be skipped early (coverage-sweep `min_score` gate, adjacent-quarter
  comparison instead of all-pairs).

New code that adds an I/O-bound or LLM-bound path MUST justify its cost profile (linear, not
quadratic) before merging.

Rationale: LLM and vector-DB calls dominate both latency and cost; efficiency is a
correctness concern, not an optimization afterthought.

### IV. Purposeful Comments (適當註解)

Comments MUST explain *why*, never restate *what*:

- Every module MUST open with a docstring stating its responsibility and key design
  decisions.
- Every public function MUST have a docstring covering Args/Returns types, fault-tolerance
  behavior, and side effects.
- Inline comments MUST be reserved for non-obvious rationale and MUST use the project tag
  convention: `# [f]` security, `# [b]` fault-tolerance, `# [c]` efficiency.
- `TODO`/`FIXME` markers are prohibited in committed code; known debt MUST be tracked as a
  GitHub Issue.

Rationale: Self-documenting code plus rationale-only comments stay accurate as the code
evolves; narration comments rot.

### V. Full Explainability (能解釋每一行程式碼)

Every non-trivial design choice MUST be defensible and documented:

- Architectural and algorithmic decisions (e.g. LLM-based contradiction detection vs. rules,
  adjacent-quarter vs. all-pairs comparison, `agent.stream()` vs. `invoke()`,
  `operator.add` reducers) MUST have a recorded rationale in CLAUDE.md or the relevant spec.
- No code may be merged that a maintainer cannot explain line-by-line on review.
- "Cargo-cult" copied code, unexplained magic numbers, and unreachable branches are
  prohibited.

Rationale: This is an academic/professional project that must withstand line-by-line
defense; unexplained code is a defect regardless of whether it runs.

### VI. Security First (高安全性)

The application MUST defend against the threat model below; these controls are
NON-NEGOTIABLE:

- **XSS** — all LLM/output text inserted into HTML MUST pass through `html.escape()`
  (`_sanitize_str()`).
- **Prompt injection** — user input MUST be length-capped (500 chars) and stripped of HTML
  tags before reaching any LLM.
- **API abuse / DoS** — request rate limiting MUST be enforced (session cooldown + IP-based
  cooldown).
- **Illegal parameter injection** — company/topic values MUST be validated against a
  whitelist even if the UI widget is bypassed; BigQuery query parameters MUST be bound
  via `ScalarQueryParameter` / `ArrayQueryParameter`, never string-interpolated.
- **Token-explosion** — LLM prompt content MUST be truncated to bounded length.
- **Liability** — the UI MUST display the "no stock-picking advice" disclaimer.

New code that handles user input, renders output, or calls an external API MUST be reviewed
against this list before merge.

Rationale: A public-facing LLM app is exposed to injection, abuse, and content-rendering
attacks; security controls are mandatory acceptance criteria, not enhancements.

## Technology & Architecture Constraints

- **Stack**: Python, Streamlit (UI), LangGraph (agent orchestration), **BigQuery Vector
  Search** (`VECTOR_SEARCH(... distance_type => 'COSINE')`) as the vector store with
  Cohere `rerank-v3.5` as the second-stage reranker.
- **LLM backends**: multi-backend cascade (`gemini → openai → anthropic → cohere`) behind a
  single `chat()` entrypoint; backends MUST be hot-swappable and fall through on
  quota/429/401/404/503.
- **Agent shape**: exactly the 7 nodes defined in `graph.py`
  (`classify → decompose → route → retrieve → detect → reflect → report`); the
  `reflect → retrieve` self-reflection loop is bounded (confidence ≥ 0.75, iteration ≥ 3,
  or cost guard).
- **Cost control**: per-query LLM spend MUST respect `LLM_BUDGET_USD`; the cost guard
  forces loop termination when exceeded.
- **Observability**: every `chat()` call MUST record tokens, latency, and estimated USD cost
  into the telemetry registry (successful and failed calls alike).
- **Configuration**: behavior-affecting values MUST be environment-variable driven with
  documented defaults and safe fallback on invalid values (e.g. `COVERAGE_MIN_SCORE`).

## Development Workflow & Quality Gates

- **Tests**: unit tests in `tests/` MUST run offline (no API keys). The end-to-end
  benchmark MUST meet its thresholds: contradiction accuracy ≥ 80%, hallucination ≤ 5%,
  citation ≥ 90%, self-reflection trigger ≥ 30%, promise tracking ≥ 75%.
- **Pre-merge checks**: changed Python files MUST pass `py_compile`; affected modules'
  self-checks (`python src/core/llm_client.py`, etc.) MUST succeed.
- **Code review**: every change MUST be reviewed against all six Core Principles; a change
  that violates a NON-NEGOTIABLE control (Principle VI) MUST NOT merge.
- **Scope discipline**: changes MUST be minimal and focused — no speculative abstraction, no
  unrelated refactoring bundled into a fix.

## Governance

This constitution supersedes ad-hoc practice. CLAUDE.md sections a–f are the source rubric
and MUST be kept consistent with this document.

- **Amendments** MUST be made via pull request, documented in the Sync Impact Report at the
  top of this file, and version-bumped per the policy below.
- **Versioning policy** (semantic):
  - MAJOR — backward-incompatible removal or redefinition of a principle or governance rule.
  - MINOR — a new principle/section added or guidance materially expanded.
  - PATCH — clarifications, wording, and non-semantic refinements.
- **Compliance review**: every PR review MUST verify principle compliance; complexity that
  appears to violate Principle III or V MUST be justified in the PR description or rejected.
- **Runtime guidance**: CLAUDE.md provides the day-to-day development guidance that
  implements these principles.

**Version**: 1.0.1 | **Ratified**: 2026-05-22 | **Last Amended**: 2026-05-22
