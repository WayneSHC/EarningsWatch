# Feature Specification: Contradiction Detection

**Feature Branch**: `001-contradiction-detection`

**Created**: 2026-05-22

**Status**: Draft (as-built — reverse-engineered from `src/core/contradiction.py`)

**Input**: User description: "As-built spec for the Contradiction Detection subsystem of EarningsWatch — `batch_detect`, `detect_promises`, `_extract_json`, boilerplate filtering, evidence verifier, content/empty checks, content truncation, and type validation."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Detect stance shifts across quarters (Priority: P1)

A retail investor or analyst selects a company and a topic (e.g. "AI 需求", "毛利率"), then asks the system to compare how management talked about that topic across several earnings calls. The system returns, for each pair of quarters, whether the management's stance got more optimistic, more conservative, stayed the same, or was unrelated — together with the exact quoted evidence from each quarter and a confidence score.

**Why this priority**: This is the product's core value proposition. Without it, the system has nothing to show the user.

**Independent Test**: Provide 2+ quarters of statement chunks for one company and one topic, run detection, and verify the output contains a per-pair analysis with `stance_change`, `has_contradiction`, evidence quoted from both quarters, and a `confidence` in [0,1].

**Acceptance Scenarios**:

1. **Given** statements from three quarters where management goes from "需求強勁" → "需求穩健" → "庫存調整", **When** the user requests contradiction detection for topic "需求", **Then** the system returns adjacent-quarter comparisons whose `stance_change` reflects the increasingly conservative tone.
2. **Given** two quarters discussing the same topic with no real change, **When** detection runs, **Then** `stance_change` is "維持不變" and `has_contradiction` is false.
3. **Given** two quarters of statements that don't actually discuss the user's topic, **When** detection runs, **Then** `same_topic` is false and `stance_change` is "無關".
4. **Given** a per-pair LLM call fails (network error, quota), **When** detection runs across multiple pairs, **Then** the failing pair returns a degraded result with `confidence: 0.0` and the other pairs still complete successfully.

---

### User Story 2 — Reject hallucinated evidence (Priority: P1)

When the system reports a contradiction, it must quote real text from the source statements, not a plausible-sounding sentence the LLM invented. If the LLM returns a quote that does not appear in the source, that quote must be flagged or removed before the result reaches the user.

**Why this priority**: Hallucinated evidence destroys trust and could mislead investment decisions. This is a NON-NEGOTIABLE quality gate — Principle VI (Security First) of the constitution.

**Independent Test**: Inject a stub LLM response whose `evidence_early` quote does not appear in the source content. Verify the returned analysis has the quote cleared, `verification_failed=True`, and `confidence` reduced by 0.2.

**Acceptance Scenarios**:

1. **Given** an LLM response whose evidence quote exactly appears in the source content, **When** verification runs, **Then** the quote is kept and not flagged.
2. **Given** an LLM response whose evidence quote is a near-paraphrase of source content (≥ 85% similar over a sliding window), **When** verification runs, **Then** the quote is kept and marked as fuzzy-matched.
3. **Given** an LLM response whose evidence quote does not appear in (or near) the source, **When** verification runs, **Then** the quote is cleared, `verification_failed` is set, and confidence is reduced by 0.2.
4. **Given** an LLM-returned quote shorter than the minimum quote length, **When** verification runs, **Then** the quote is accepted without verification (short strings produce too many false rejections).

---

### User Story 3 — Filter out legal boilerplate (Priority: P1)

Earnings call transcripts and slide decks repeat the same forward-looking-statement disclaimers on many pages. The retrieval layer's coverage sweep can match these as if they were topic content. The system must skip pair comparisons in which both quarters' content is just boilerplate, because comparing identical legalese has no analytical value.

**Why this priority**: Without this, ~30% of low-coverage quarters produce noise pairs that crowd out genuine findings.

**Independent Test**: Feed two quarters whose content contains only "前瞻性陳述... 實際結果可能與..." text. Verify the pair is skipped and produces no result.

**Acceptance Scenarios**:

1. **Given** two quarters where both contents match a known boilerplate signature (in English or Chinese, regardless of whitespace and case), **When** `batch_detect` runs, **Then** the pair is skipped with a logged warning.
2. **Given** one quarter is boilerplate and one is real content, **When** `batch_detect` runs, **Then** the pair proceeds normally (only both-boilerplate pairs are filtered).

---

### User Story 4 — Track forward-looking promises (Priority: P2)

For each pair of adjacent quarters, the system identifies whether the earlier quarter contained a specific forward-looking promise (e.g. capex guidance, revenue range, margin target). If so, it judges from the later quarter whether that promise was met, missed, or remains unclear.

**Why this priority**: A useful secondary signal that complements the primary stance-change analysis. Lower priority than P1 because the report is still valuable without it.

**Independent Test**: Provide two quarters where the first contains an explicit numeric guidance and the second discusses actual results. Verify the output includes a promise entry with `status` set to one of 達標/未兌現/不明 and a confidence score.

**Acceptance Scenarios**:

1. **Given** the earlier quarter contains explicit guidance and the later quarter reports actuals that meet it, **When** promise detection runs, **Then** `status` is "達標" with a status emoji prefix.
2. **Given** the earlier quarter does not contain any trackable promise, **When** detection runs, **Then** no entry is returned for that pair.
3. **Given** a per-pair LLM call fails, **When** detection runs, **Then** the failing pair is silently dropped and other pairs still return.

---

### User Story 5 — Cap LLM cost and latency (Priority: P2)

The system must bound the cost and latency of contradiction analysis so a single user query cannot trigger an arbitrarily expensive LLM workload. Each per-quarter content sent to the LLM is truncated to a fixed character ceiling, and the default pairing strategy is linear (adjacent quarters) rather than quadratic (all pairs).

**Why this priority**: Cost and latency are correctness concerns under the constitution's Principle III; without them the feature is operationally unviable.

**Independent Test**: Provide a quarter whose content exceeds the truncation limit. Verify the content actually sent to the LLM is bounded by that limit. With N quarters and default `pair_mode`, verify exactly N−1 pairs are evaluated.

**Acceptance Scenarios**:

1. **Given** N quarters with default `pair_mode="adjacent"`, **When** detection runs, **Then** exactly N−1 quarter pairs are evaluated.
2. **Given** explicit `pair_mode="all_pairs"`, **When** detection runs, **Then** N×(N−1)/2 pairs are evaluated (opt-in to quadratic cost).
3. **Given** per-quarter content longer than `_MAX_CONTENT`, **When** detection runs, **Then** the content actually sent to the LLM is truncated to that bound.

---

### Edge Cases

- **Empty/missing input** — One or both quarters have no chunks, or chunks whose `content` is empty. The pair is skipped with a logged warning rather than raising.
- **Non-dict input** — Caller passes `None` or a non-dict to `detect_contradiction`. The function raises `ValueError` immediately rather than failing deep in the prompt builder.
- **LLM returns malformed JSON** — Response is unparseable as JSON, or wrapped in a markdown fence, or contains prose before/after the JSON. Three-layer fallback (direct parse → fence extraction → greedy `{...}`/`[...]` scan) handles each case. If all three fail, a default object with `confidence: 0.0` is returned and a warning is logged.
- **LLM returns a list when a dict is expected** (or vice versa) — `_extract_json` accepts both root types; downstream callers must handle the actual shape.
- **`tenacity.RetryError`** — The retry wrapper hides the original exception type. The system unwraps it so logs/UI show the root cause (auth, quota, etc.) rather than the generic retry wrapper.
- **Concurrent LLM rate limits** — Parallel pair detection is capped at a small worker count (default 2, env-overridable via `LLM_PAIR_WORKERS`) to avoid 429s on free-tier LLM backends.
- **Topic argument is not a string** — Coerced to string and trimmed; empty string is allowed (LLM does a generic comparison).
- **Quarter ordering swap** — If caller passes a later quarter as `stmt_a`, the function swaps so `stmt_a` is always the chronologically earlier one (string-sorted: "2024Q1" < "2024Q3").

## Requirements *(mandatory)*

### Functional Requirements

**Pair generation**

- **FR-001**: System MUST sort quarter keys lexicographically and, in default mode, generate exactly N−1 adjacent pairs.
- **FR-002**: System MUST support an opt-in `all_pairs` mode that generates every unordered pair (N×(N−1)/2).
- **FR-003**: System MUST skip any pair where either quarter has no chunks or where the joined chunk content is empty after stripping whitespace.

**LLM analysis**

- **FR-004**: For each pair, system MUST send the earlier and later quarter's content (joined from up to `chunks_per_pair` chunks, default 4) to an LLM with a structured prompt and parse the response as JSON.
- **FR-005**: The parsed analysis MUST contain the fields: `same_topic` (bool), `stance_change` (one of 更樂觀/更保守/維持不變/無關), `has_contradiction` (bool), `change_detail` (string), `evidence_early` (string), `evidence_later` (string), `follow_up_question` (string), `confidence` (float in [0,1]).
- **FR-006**: If an LLM call fails after retry, system MUST return a degraded analysis with `confidence: 0.0` and a `change_detail` describing the failure type, rather than aborting the batch.
- **FR-007**: System MUST unwrap `tenacity.RetryError` so logged/displayed error messages reflect the underlying API error, and MUST truncate error messages to 120 chars to avoid leaking endpoints or keys.

**Evidence verification**

- **FR-008**: System MUST verify each non-empty evidence quote against the source content of its quarter using (a) exact substring match, then (b) a sliding-window fuzzy match with similarity threshold ≥ 0.85.
- **FR-009**: When a quote fails verification, system MUST clear the quote, set `verification_failed: True`, and reduce `confidence` by 0.2 (floored at 0.0).
- **FR-010**: When a quote passes only by fuzzy match, system MUST mark `evidence_<key>_fuzzy: True` so the UI can render a soft-match indicator.
- **FR-011**: System MUST skip verification for quotes shorter than the minimum quote length (too short to verify without false rejections).
- **FR-012**: System MUST bound the source text used for fuzzy comparison to a fixed cap to keep verification time bounded regardless of caller input.

**Boilerplate filtering**

- **FR-013**: System MUST recognise a configurable set of forward-looking-statement disclaimer signatures in both English and Chinese (台股法說會 phrasing).
- **FR-014**: Boilerplate detection MUST be case-insensitive and whitespace-insensitive (so half-width vs full-width spaces and line wrapping do not defeat it).
- **FR-015**: When both quarters in a pair match a boilerplate signature, system MUST skip the pair with a logged warning. When only one quarter matches, the pair MUST proceed.

**JSON extraction**

- **FR-016**: `_extract_json` MUST attempt three parsing strategies in order: direct `json.loads`, markdown fence extraction (` ```json ... ``` ` or unlabeled), and greedy `{...}`/`[...]` substring scan; the earliest candidate wins.
- **FR-017**: When all three strategies fail, system MUST log the first 200 characters of the raw response and return a default object with `confidence: 0.0` and `stance_change: "無關"`.
- **FR-018**: `_extract_json` MUST accept both `dict` and `list` root types and return whichever the LLM produced.

**Input validation**

- **FR-019**: `detect_contradiction` MUST raise `ValueError` immediately when either statement argument is not a dict.
- **FR-020**: `detect_contradiction` MUST normalise pair order so the chronologically earlier quarter is always passed to the LLM as `stmt_a` (lexicographic comparison on the `quarter` field).
- **FR-021**: `batch_detect` MUST coerce the topic argument to a string and trim whitespace; empty topic MUST be permitted.

**Cost & concurrency**

- **FR-022**: System MUST truncate per-quarter LLM-bound content to a fixed character ceiling (`_MAX_CONTENT=2000`).
- **FR-023**: Per-pair LLM calls MUST run concurrently with a small bounded worker count (default 2), overridable via the `LLM_PAIR_WORKERS` environment variable, to respect free-tier LLM rate limits while still parallelising I/O.

**Output shape**

- **FR-024**: `batch_detect` MUST return a list of `{quarter_a, quarter_b, analysis, sources_a, sources_b}` objects sorted by original pair order; `sources_*` MUST be deduplicated `{file, page}` records suitable for report citations.
- **FR-025**: `detect_promises` MUST return only entries where the LLM reports `has_promise: true`; each entry MUST include `promise_quarter`, `followup_quarter`, `content`, `status` (one of 達標/未兌現/不明 with a leading status emoji), `detail`, and `confidence`.

### Key Entities

- **Quarter Statement**: A list of retrieved chunks for a single quarter. Each chunk has a `payload` dict with `content` (text), `date` (ISO date), `source_file`, and `source_page`.
- **Pair Analysis**: The structured JSON the LLM returns for one quarter pair. Fields listed in FR-005, augmented post-verification with `verification_failed` and `evidence_*_fuzzy` flags.
- **Source Citation**: A `{file, page}` record used by the report layer to render footnote-style citations.
- **Promise Record**: A forward-looking guidance from one quarter together with the follow-up quarter's verdict on whether it was met.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For the benchmark suite of curated quarter-pair fixtures, the system's `stance_change` classification matches the human-labelled ground truth in **≥ 80%** of pairs.
- **SC-002**: Quoted evidence in returned analyses appears in the source content in **≥ 95%** of pairs (i.e. hallucinated quotes are filtered to ≤ 5%).
- **SC-003**: When a single pair's LLM call fails, **0** other pairs in the same batch are aborted.
- **SC-004**: For a 5-quarter input with default settings, the user receives results in less time than running the same 5 pairs sequentially (parallel speedup is observable, bounded by the rate-limit cap).
- **SC-005**: When the user provides 5 quarters in default mode, the system performs exactly **4** LLM pair calls (linear, not quadratic).
- **SC-006**: For pairs where both quarters' content matches known disclaimer signatures, **0** LLM pair calls are made.
- **SC-007**: For inputs with malformed LLM JSON across all three fallback layers, the system returns a result object (never raises) and surfaces a `confidence: 0.0` signal so the agent's self-reflection loop can detect and retry.

## Assumptions

- The retrieval layer upstream supplies chunks whose `payload.content` is the raw transcript text; no pre-cleaning is assumed.
- Quarter keys are strings of the form `YYYYQn` (e.g. `2024Q3`) so lexicographic sort matches chronological order.
- At least one LLM backend in the project's cascade is available; transient failures are normal and handled by retry + degraded result.
- Free-tier LLM backends impose ~10 RPM rate limits; the default concurrency of 2 is tuned for this and is overridable for paid tiers.
- The boilerplate signature list covers the common disclaimer phrasing in Taiwanese-listed companies' earnings calls and major US transcripts (TSMC, Apple, Nvidia patterns); rare or new phrasings may slip through and require list maintenance.
- The constitution's Principle II (fault tolerance) and Principle VI (security: bounded inputs, no information leakage in logs) are mandatory acceptance criteria for any change to this subsystem.
