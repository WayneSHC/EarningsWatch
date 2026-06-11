# Feature Specification: LangGraph 7-Node Agent

**Feature Branch**: `003-langgraph-agent`

**Created**: 2026-05-22

**Status**: Draft (as-built — reverse-engineered from `src/agent/`)

**Input**: User description: "As-built spec for the LangGraph 7-Node Agent subsystem — graph construction, the 7 nodes, self-reflection loop, cost guard, abstain path, tool routing, coverage matrix, LangSmith tracing."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Run a single end-to-end analysis (Priority: P1)

A user asks "台積電 AI 需求今年的展望" through the Streamlit UI. The agent classifies intent, decomposes the query into 2–5 sub-queries, plans tools, retrieves chunks in parallel (BigQuery + Tavily news + yfinance stock), runs contradiction detection across quarters, self-evaluates the confidence of the result, optionally loops back to retrieve more data, and finally generates a Markdown report — all while streaming step-by-step progress to the UI.

**Why this priority**: This is the agent's reason to exist; every other capability composes into this flow.

**Independent Test**: Call `run_agent(query, company, topic)` end-to-end with a populated BQ corpus; assert the returned state contains `final_report` (non-empty Markdown) and `confidence` in [0,1].

**Acceptance Scenarios**:

1. **Given** a fully populated state, **When** the agent runs, **Then** the 7 nodes execute in order `classify → decompose → route → retrieve → detect → reflect → report` and the final report contains "跨季發言比對" + "承諾兌現追蹤" + "來源索引" sections.
2. **Given** the user supplied `company` and `topic` directly via UI, **When** `intent_classifier` runs, **Then** no LLM call is made to re-extract them.
3. **Given** the corpus has chunks for only 1 quarter, **When** `contradiction_detect` runs, **Then** it returns empty lists and logs that cross-quarter comparison needs ≥ 2 quarters.

---

### User Story 2 — Self-reflection loop retries on low confidence (Priority: P1)

When the LLM judge scores the retrieved evidence below the confidence threshold (0.75), the agent must loop back to retrieval — but with **better-targeted sub-queries**, not the same ones. Retries are bounded: at most 3 reflect cycles (1 initial + 2 retries), with two additional safety stops (cost guard, abstain).

**Why this priority**: Without this loop the agent ships unreliable answers; without bounds it bills the user unbounded LLM cost.

**Independent Test**: Stub the LLM judge to return `score=0.5` on iterations 1–2 and `score=0.9` on iteration 3. Assert `parallel_retrieval` runs 3 times total and the final report has confidence 0.9.

**Acceptance Scenarios**:

1. **Given** `confidence < 0.75` and `iteration < 3`, **When** `should_continue` runs, **Then** it returns `"retry"`.
2. **Given** `iteration ≥ 3`, **When** `should_continue` runs, **Then** it returns `"end"` regardless of confidence.
3. **Given** `cost_guard_triggered=True`, **When** `should_continue` runs, **Then** it returns `"end"` regardless of confidence or iteration.
4. **Given** the LLM judge identified `gaps`, **When** retry is triggered, **Then** new sub-queries are built from those gaps (with tool routed to Tavily if the gap contains news/market/outlook keywords, else BigQuery).
5. **Given** the coverage matrix flags weak quarters (chunk_count<2, max_score<0.4, or quote unverified), **When** retry is triggered, **Then** up to 3 quarter-targeted sub-queries are added with `target_quarter` set so retrieval concentrates on those quarters.
6. **Given** no gaps and no weak quarters detected, **When** retry is triggered, **Then** the original sub-queries are reused unchanged (avoid introducing noise).

---

### User Story 3 — Cost guard enforces LLM budget (Priority: P1)

The agent must not allow a single user query to spend more than the configured LLM budget. The budget is measured per-query (not per-session) using a baseline captured at `intent_classifier` so parallel multi-company runs cannot double-count each other.

**Why this priority**: An unbounded retry loop on a paid LLM is the project's largest operational risk.

**Independent Test**: Set `LLM_BUDGET_USD=0.01`. Run the agent. Assert `cost_guard_triggered=True` and the report contains the 💸 budget-protection notice.

**Acceptance Scenarios**:

1. **Given** `intent_classifier` is entering, **When** it runs, **Then** it captures the current telemetry `estimated_cost_usd` as `cost_baseline_usd`.
2. **Given** during retry the spent amount (current cost − baseline) ≥ `LLM_BUDGET_USD`, **When** `self_reflect` runs, **Then** `cost_guard_triggered` is set to `True` and a log entry explains the trigger.
3. **Given** `cost_guard_triggered=True`, **When** `report_generator` runs, **Then** the report includes a "預算保護觸發" notice.

---

### User Story 4 — Abstain when evidence is severely insufficient (Priority: P2)

After all 3 retry attempts, if confidence is still below 0.4, the agent must abstain (not generate an analytical report). It outputs a clear "資料不足" message listing issues and remaining gaps, plus actionable suggestions (re-ingest PDFs, narrow scope, change keywords).

**Why this priority**: A confidently-wrong report is worse than no report — abstaining preserves user trust.

**Independent Test**: Stub LLM judge to return `score=0.3` for all iterations. Assert `abstain=True` and the report title begins with "⚠️ EarningsWatch — 資料不足".

**Acceptance Scenarios**:

1. **Given** retries exhausted and `confidence < 0.4`, **When** `self_reflect` runs, **Then** `abstain=True`.
2. **Given** `abstain=True`, **When** `report_generator` runs, **Then** the report is the abstention message (not the standard report sections).
3. **Given** `confidence ∈ [0.4, 0.75)` after exhausting retries, **When** the report generates, **Then** it produces the normal report (with caveats) — not the abstention.

---

### User Story 5 — Route to correct external tools (Priority: P2)

The Dynamic Tool Router decides which external tools to call (always BigQuery; conditionally Tavily news / yfinance stock) based on the user's question. The router is LLM-driven (function-calling-style schema) and degrades to keyword matching when the LLM fails. BigQuery is always included regardless of what the LLM says.

**Why this priority**: Tool selection determines what data the agent has access to. Wrong selection causes either unnecessary cost or missing context.

**Independent Test**: Issue a query containing "股價"; assert `tool_plan` includes `yfinance`. Issue a query containing "最新"; assert `tavily` is included. Issue a generic query; assert only `bigquery` is included.

**Acceptance Scenarios**:

1. **Given** any query, **When** `decide_tools` runs, **Then** the returned tool list always contains `bigquery`.
2. **Given** the LLM returns a valid `{"tools": [...]}` JSON, **When** the router runs, **Then** only whitelisted tool names are kept.
3. **Given** the LLM fails or returns malformed JSON, **When** the router runs, **Then** it falls back to keyword-based selection.

---

### User Story 6 — Off-topic detection redirects to news (Priority: P3)

When the direct-answer LLM concludes that the corpus does not cover the requested topic (or when retrieval returns zero chunks for the topic), the report skips cross-quarter comparison and promise tracking — those are noise for off-topic queries — and surfaces Tavily news instead. If the tool router didn't already trigger Tavily, it's invoked on demand.

**Why this priority**: A useful user experience for "I asked about something the transcripts don't actually cover" cases.

**Independent Test**: Run a query for a topic guaranteed to be absent from the corpus. Assert the report contains the "本主題未在法說會逐字稿中找到相關內容" notice and a "網路新聞補充" section.

**Acceptance Scenarios**:

1. **Given** the direct-answer LLM text contains any phrase from the off-topic phrase list, **When** the report builds, **Then** it switches to off-topic mode.
2. **Given** retrieval returned zero total chunks, **When** the report builds, **Then** it switches to off-topic mode (even without a direct-answer LLM call).
3. **Given** off-topic mode but Tavily returned no results, **When** the report builds, **Then** it suggests adjusting the query.

---

### User Story 7 — Optional LangSmith observability (Priority: P3)

When `LANGSMITH_TRACING=true` (or `LANGCHAIN_TRACING_V2=true`) AND a `LANGSMITH_API_KEY` (or `LANGCHAIN_API_KEY`) is set, LangGraph automatically uploads each agent execution's node trace to smith.langchain.com for debugging the self-reflection loop, observing token usage, and replaying historical traces.

**Why this priority**: Operational quality-of-life, not required for end-user value.

**Independent Test**: Set tracing env vars to valid values; `is_tracing_enabled()` returns True. Unset one of them; returns False.

---

### Edge Cases

- **No retrieval results at all** — `parallel_retrieval` returns empty `retrieved`; `contradiction_detect` returns empty lists; report generator switches to off-topic mode (User Story 6) and tries Tavily on demand.
- **All tool calls in `parallel_retrieval` fail** — `as_completed` per-future try/except keeps the loop alive; failing futures log a UI message (type only, not full exception) and don't abort the batch.
- **LLM JSON parsing failure** in intent_classifier / decomposer / router / judge — each node has its own fallback (defaults for classifier, sample template for decomposer, keyword fallback for router, `score=0.6` for judge).
- **Retrieved chunks deduplication on retry** — `parallel_retrieval` keeps prior-iteration results as a base and only appends new chunks whose `id` is not already present, so retries augment rather than replace.
- **Coverage sweep with user-selected quarters** — coverage sweep only triggers when `quarters_filter` is empty (i.e., user picked "all"); explicit quarter selections are respected verbatim.
- **`target_quarter` overrides `quarters_filter`** — when self-reflection generates a weak-quarter retry, the per-sub-query `target_quarter` field overrides the global filter so retrieval concentrates on that single weak quarter.
- **Tavily / yfinance off** — missing API keys cause the respective helpers to return empty lists / error dicts; the agent still runs.
- **`should_continue` is a pure function** — it does not mutate state, only reads it (LangGraph contract).

## Requirements *(mandatory)*

### Functional Requirements

**Graph construction**

- **FR-001**: System MUST register exactly 7 nodes (`classify`, `decompose`, `route`, `retrieve`, `detect`, `reflect`, `report`) and the conditional edge `reflect → {retry: retrieve | end: report}`.
- **FR-002**: `get_agent()` MUST cache the compiled graph as a module-level singleton — no recompilation per `run_agent` call.

**State contract**

- **FR-003**: All inter-node communication MUST flow through the typed `AgentState` TypedDict; `steps_log` MUST use `Annotated[list, operator.add]` so node logs accumulate rather than overwrite.

**Node 1 — Intent classifier**

- **FR-004**: When the user supplied `company` and `topic` via UI, system MUST NOT call the LLM for intent extraction; it uses the provided values directly.
- **FR-005**: `intent_classifier` MUST capture the current telemetry cost as `cost_baseline_usd` before any downstream LLM calls.

**Node 2 — Query decomposer**

- **FR-006**: System MUST decompose the query into 2–5 sub-queries via LLM, with topic-specific guidance to preserve technical acronyms (CoWoS, HBM, FinFET, N2…) verbatim — never substitute similar-but-different terms.
- **FR-007**: When the LLM fails or returns no valid sub-queries, system MUST fall back to the 3-sub-query template (cross_quarter, guidance, news).
- **FR-008**: Each sub-query MUST be truncated to 120 chars and `purpose` to 40 chars.

**Node 3 — Dynamic tool router**

- **FR-009**: `decide_tools` MUST always include `bigquery`; other tools are LLM-decided per `TOOL_SPECS`.
- **FR-010**: When the LLM call fails or response lacks a `"tools"` key, system MUST fall back to `decide_tools_by_keyword`.

**Node 4 — Parallel retrieval**

- **FR-011**: BigQuery / Tavily / yfinance sub-tasks MUST run concurrently via `ThreadPoolExecutor`; each subtask MUST have its own try/except so one failure does not abort the others.
- **FR-012**: BigQuery sub-queries with `target_quarter` set MUST override the global `quarters_filter` and constrain retrieval to that single quarter.
- **FR-013**: Per-sub-query `top_k` MUST be adaptive: base 5, increased to ~`n_quarters + 2` for cross-quarter, reduced to 4 for guidance, raised to 8 for coverage_fill, +2 on retry, capped at 12.
- **FR-014**: Retry iterations MUST keep prior-iteration retrieved chunks and only append new chunks whose `id` is unseen — no replacement.
- **FR-015**: When `quarters_filter` is empty (user picked "all"), system MUST run a coverage sweep for any quarters present in the corpus but missing from initial results.

**Node 5 — Contradiction detect**

- **FR-016**: When fewer than 2 quarters have results, system MUST skip detection and return empty lists with a log entry.
- **FR-017**: System MUST call `batch_detect` for contradictions AND `detect_promises` for forward-guidance follow-through.

**Node 6 — Self-reflect**

- **FR-018**: Build a per-quarter coverage matrix containing `chunk_count`, `max_score`, `avg_score`, `source_pages`, `quote_verified`, and `top_excerpt`.
- **FR-019**: Score the result via an LLM judge that returns `score`, `issues`, `gaps`, `should_retry`. JSON parse failure MUST degrade to `score=0.6` and fall back to hard-floor checks.
- **FR-020**: Hard floor checks MUST subtract penalty when retrieved quarters < 2 (−0.4), total chunks < 3 (−0.2), or mean contradiction confidence < 0.5 (−0.2).
- **FR-021**: When retry is triggered, sub-queries MUST be rebuilt from BOTH (a) gap-driven LLM gaps (routed Tavily if news-keyword, else BigQuery) AND (b) coverage-driven weak quarters (BigQuery with `target_quarter`, up to 3).
- **FR-022**: When neither gaps nor weak quarters yield queries, original sub-queries MUST be reused unchanged.
- **FR-023**: When retries are exhausted AND confidence < 0.4, system MUST set `abstain=True`.
- **FR-024**: When per-query spent (current cost − `cost_baseline_usd`) ≥ `LLM_BUDGET_USD`, system MUST set `cost_guard_triggered=True`.

**Node 7 — Report generator**

- **FR-025**: When `abstain=True`, system MUST output the abstention message (no analytical sections) with issues, gaps, and remediation suggestions.
- **FR-026**: When `cost_guard_triggered=True`, system MUST prepend a 💸 budget-protection notice to the report.
- **FR-027**: The "Direct Answer" LLM call MUST cap concatenated chunk text at 4000 chars and use mode `demo`.
- **FR-028**: When the direct-answer text matches any off-topic phrase OR `total_chunks==0`, system MUST switch to off-topic mode: skip contradictions/promises, prefer Tavily news (on-demand if missing), optionally include stock.
- **FR-029**: All LLM-returned strings inserted into the report MUST be HTML-escaped via `html.escape`; news URLs MUST be filtered to `http://` or `https://` only.

**Conditional edge — `should_continue`**

- **FR-030**: MUST return `"end"` if `cost_guard_triggered=True`.
- **FR-031**: MUST return `"retry"` iff `confidence < 0.75` AND `iteration < 3` AND not cost-guarded.
- **FR-032**: MUST be a pure function — no state mutation.

**Tools**

- **FR-033**: `search_news` MUST filter Tavily results by company alias (Chinese full / short / English / stock code) and sort by published_date (newest first) then relevance.
- **FR-034**: When Tavily / yfinance credentials are missing, the respective helper MUST return empty list / error dict rather than raising.

**Observability**

- **FR-035**: `is_tracing_enabled()` MUST return True iff both a tracing-flag env var AND a tracing-key env var are set (either legacy or new naming).

### Key Entities

- **AgentState** — the typed TypedDict carrying all node-to-node data; the only allowed communication channel between nodes.
- **Sub-query** — `{id, query, purpose, tool, [section_filter], [target_quarter], [tool_hint]}` — the unit of work for `parallel_retrieval`.
- **Coverage matrix entry** — per-quarter quality summary used by self-reflect to detect weak quarters.
- **Tool plan** — list of tool names (subset of `bigquery`/`tavily`/`yfinance`), always containing `bigquery`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For the benchmark suite, the agent produces a non-empty `final_report` for ≥ 98% of queries (failure budget reserved for catastrophic ext-service outages).
- **SC-002**: The self-reflection loop triggers (≥ 1 retry) on ≥ 30% of benchmark queries — proves the loop is exercised, not a no-op.
- **SC-003**: Across the benchmark suite, **0** queries exceed `2 × LLM_BUDGET_USD` (cost guard works under load).
- **SC-004**: For queries where the corpus genuinely doesn't cover the topic, the abstain or off-topic path triggers ≥ 90% of the time — measured by `python tests/benchmark.py --type outofcorpus` (section F: 5-question curated out-of-corpus set, asserting `abstain=True` or the off-topic report marker).
- **SC-005**: `should_continue` is verifiably pure — running it twice on identical state returns the same result with no observable side effect (testable via mocking).
- **SC-006**: When any one of {LLM backend, BigQuery, Tavily, yfinance} fails on a query, the agent still produces *some* report (graceful degradation), verified by injection tests.
- **SC-007**: HTML escaping: **0** un-escaped LLM-returned strings reach the final `final_report` (testable via grep for unescaped `<` in report bytes given an LLM stub returning `<script>`).

## Assumptions

- The LangGraph version supports `StateGraph.add_conditional_edges` and `operator.add` reducers on Annotated TypedDict fields (project pins this; no version-skew migration needed).
- Telemetry registry's `summary()` returns reliable per-process cost accumulators; cost guard's accuracy is bounded by telemetry's accuracy.
- The `cost_baseline_usd` pattern works correctly under multi-company parallel runs because each agent instance owns its own state; telemetry totals are shared, but the baseline-subtraction isolates per-query spend.
- The 3-iteration retry cap is calibrated for the project's typical latency budget; longer caps would be a meaningful behavior change requiring spec revision.
- Off-topic phrase list (`_OFF_TOPIC_PHRASES`) is curated for Traditional Chinese financial-analysis phrasing; English or Simplified Chinese inputs would need list maintenance.
- Constitution Principle I (layered: agent depends only on core), II (fault tolerance), III (parallelism / cost cap), and VI (HTML escape / URL whitelist) are mandatory acceptance criteria.
