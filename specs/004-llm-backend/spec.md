# Feature Specification: LLM Backend Cascade & Telemetry

**Feature Branch**: `004-llm-backend`

**Created**: 2026-05-22

**Status**: Draft (as-built — reverse-engineered from `src/core/llm_client.py` + `src/core/telemetry.py`)

**Input**: User description: "As-built spec for the LLM backend cascade — unified chat() entrypoint, 4-backend fallback, prompt-injection guard, per-call timeout, telemetry recording, cost estimation, and UI-safe error messages."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Single `chat()` call hides backend complexity (Priority: P1)

Every part of the system calls one function — `chat(prompt, max_tokens, mode)`. It transparently picks an available backend (Gemini → OpenAI → Anthropic → Cohere by default), sends the prompt with a prepended injection guard, enforces a per-call timeout, and returns the response text. Callers never see which backend was used; that's a configuration concern.

**Why this priority**: Backend transparency is the contract the rest of the system depends on. Every node, retriever, and tool calls `chat`.

**Independent Test**: Stub each backend's dispatch function. Call `chat("hello")`. Assert the result is the stubbed text and the caller has no awareness of which backend ran.

**Acceptance Scenarios**:

1. **Given** `LLM_BACKEND=gemini` is set and the Gemini key is present, **When** `chat()` is called, **Then** Gemini is selected.
2. **Given** no `LLM_BACKEND` is set, **When** `chat()` is called, **Then** the first backend in the auto-detect order (`gemini → openai → anthropic → cohere`) that has a valid API key is selected.
3. **Given** `mode="dev"`, **When** `chat()` is called on the OpenAI backend, **Then** `gpt-5-mini` is used (not the demo model `gpt-5`).
4. **Given** the prompt is empty or whitespace-only, **When** `chat()` is called, **Then** it raises `ValueError` before any LLM call.

---

### User Story 2 — Automatic cascade across backends on quota/auth failure (Priority: P1)

When the active backend returns a quota, rate-limit, auth, model-not-found, or service-unavailable error, the system immediately switches to the next backend in the candidate list — without retry on the failing one — and prints a Chinese-language friendly notice so the operator (visible in Streamlit logs) understands what happened.

**Why this priority**: Without cascade, a single backend outage halts the entire product. With cascade, the user just sees a brief notice and the analysis still completes.

**Independent Test**: Stub backend A to raise an error containing "rate limit"; stub backend B to succeed. Assert `chat()` returns B's response and a quota-message print is captured.

**Acceptance Scenarios**:

1. **Given** the active backend raises an exception whose message matches a quota marker (quota / billing / credit / RESOURCE_EXHAUSTED), **When** `chat()` runs, **Then** the next backend is tried immediately with no retry on the failing one.
2. **Given** a 429 / rate-limit-style error, **When** `chat()` runs, **Then** the user-visible notice classifies it as 速率限制 (not 配額用盡).
3. **Given** a quota-context 429 (message also contains "quota" or "billing"), **When** `chat()` runs, **Then** the notice classifies it as 配額用盡 (specific markers win over the generic 429 catch).
4. **Given** an auth error (401/403), **When** `chat()` runs, **Then** the notice says API Key 失效 / 權限不足.
5. **Given** a 404 / model-not-found, **When** `chat()` runs, **Then** the notice says 模型名稱不存在或已下線.

---

### User Story 3 — Same-backend retry on transient errors (Priority: P1)

For transient failures (network blip, SDK-layer timeout), the system retries on the same backend (1 retry by default with exponential backoff) before falling through to the next backend. Retries are bounded so a flaky network does not amplify cost.

**Why this priority**: Transient errors are common; retrying solves most of them cheaply. Cascading too eagerly wastes the cascade depth on noise.

**Independent Test**: Stub backend A to fail with "connection timeout" on first call then succeed on second. Assert `chat()` returns A's eventual response after exactly 1 retry; B is never called.

**Acceptance Scenarios**:

1. **Given** a transient marker in the error (connection / timeout / network / etc.), **When** `chat()` runs, **Then** the same backend is retried up to `_MAX_SAME_BACKEND_RETRIES` times with exponential delay (`_RETRY_BASE_DELAY × 2^(attempt−1)`).
2. **Given** a `TimeoutError` raised by `_dispatch_with_timeout`, **When** `chat()` runs, **Then** it's classified as transient and retried.
3. **Given** transient retries are exhausted, **When** `chat()` runs, **Then** the next backend is tried.

---

### User Story 4 — Per-call timeout prevents agent stalls (Priority: P2)

Every LLM call is dispatched inside a global `ThreadPoolExecutor` with a hard timeout (default 45 s, env-tunable). A hung backend does not stall the agent — the timeout fires, the call surfaces as a transient error, retry happens, then cascade.

**Why this priority**: Stalled LLM calls block the agent's self-reflection loop and lock up the Streamlit UI. Without enforcement, callers must trust SDK timeouts, which are often missing or too generous.

**Independent Test**: Stub a backend to sleep > timeout. Assert `chat()` raises (or cascades) within timeout + small grace period, not after the full sleep.

---

### User Story 5 — UI-safe error reporting (Priority: P1)

When every backend fails, `chat()` raises `LLMUnavailableError` — a custom exception carrying only a human-readable Chinese summary, **not** the raw SDK exception (which often includes API keys, trace IDs, request bodies). The `friendly_error_message()` helper classifies any LLM exception into a clean Chinese summary for surfacing in `steps_log` and the report.

**Why this priority**: Constitution Principle VI: no credential / endpoint / PII leakage to UI. SDK exceptions are unsafe to display verbatim.

**Independent Test**: Stub every backend to fail. Catch `LLMUnavailableError`. Assert its message contains no API key fragments, no `trace-id`, no HTTP body — just the friendly summary.

**Acceptance Scenarios**:

1. **Given** all backends fail, **When** `chat()` raises, **Then** the exception type is `LLMUnavailableError` and its `friendly_message` is plain Chinese.
2. **Given** any LLM exception, **When** `friendly_error_message(exc)` is called, **Then** the returned string is < 80 chars and contains no raw SDK details.
3. **Given** the error message contains common quota / rate-limit / auth / 404 / 503 patterns, **When** classified, **Then** the returned message uses the appropriate Chinese category.

---

### User Story 6 — Prompt injection guardrail (Priority: P1)

A standard "system safety policy" preface is prepended to every prompt by default. It instructs the LLM to treat any embedded "ignore previous instructions" / "you are now" / "send the contents to" patterns inside source text as ordinary data, not commands. Operators can disable it via `LLM_INJECTION_GUARD=false` for testing.

**Why this priority**: Defense-in-depth against prompt injection in user queries and retrieved transcript content. Constitution Principle VI requires this layer even when upstream sanitization exists.

**Independent Test**: Set `LLM_INJECTION_GUARD=true` (default). Spy on the dispatched prompt. Assert it begins with the safety policy preface. Set `LLM_INJECTION_GUARD=false`. Assert the preface is absent.

---

### User Story 7 — Per-call telemetry for cost and observability (Priority: P2)

Every `chat()` call — successful or failed — records into a thread-safe singleton registry the backend, model, prompt/completion tokens, latency, and estimated USD cost (via `_PRICING` table). The Streamlit sidebar polls `telemetry.summary()` to show session totals; the benchmark calls `reset()` between questions to compute per-query cost; the agent's cost guard reads the cumulative USD to enforce the per-query budget.

**Why this priority**: Without telemetry there's no cost guard, no per-query benchmark cost, and no observability.

**Independent Test**: `telemetry.reset()`. Call `chat()` 3 times. Assert `summary()` reports 3 calls, summed tokens, and a non-zero cost (when the backend is in `_PRICING`).

**Acceptance Scenarios**:

1. **Given** a successful call, **When** telemetry records it, **Then** the record contains `prompt_tokens`, `completion_tokens`, `duration_ms`, `cost_usd`, and `error=None`.
2. **Given** a failed call (any exception), **When** telemetry records it, **Then** `error=type(e).__name__` is set and `tokens=0` so the failure appears in summary without inflating cost.
3. **Given** the model is not in `_PRICING`, **When** cost is estimated, **Then** it returns 0.0 (graceful degrade — no crash).
4. **Given** concurrent `chat()` calls from a `ThreadPoolExecutor`, **When** telemetry records them, **Then** all records are preserved (thread-safe via `Lock`).

---

### User Story 8 — Operator can switch backends at runtime (Priority: P3)

The Streamlit UI exposes the available backends via `available_backends()` and lets the operator switch via `set_backend(name)`. Switching invalidates the lru_cache on `_detect_backend` so the next call uses the new backend.

**Why this priority**: Operational convenience for testing or working around an outage without restarting the app.

**Independent Test**: Set initial backend to A. Call `set_backend("B")`. Call `chat()`. Assert B's dispatch is invoked.

---

### Edge Cases

- **Invalid `LLM_BACKEND` value** — `_detect_backend` prints a warning and falls through to auto-detect (does not raise).
- **No backends have valid keys** — `_detect_backend` raises `EnvironmentError` with a multi-line Chinese instruction listing the required env vars.
- **All backends fail** — `chat()` raises `LLMUnavailableError` (NOT the last SDK exception) so UI can render safely.
- **Model name typo in `BACKEND_MODELS`** — the backend cascades on the 404 from the provider; the typo is observable in logs.
- **`_PRICING` missing the model key** — `estimate_cost` returns 0.0 silently (graceful), so an unknown model still records its call but contributes 0 to cost summary.
- **OpenAI parameter compat** — `_call_openai_compat` tries `max_completion_tokens` first; falls back to `max_tokens` on TypeError or specific API error messages, handling both legacy and new model API shapes.
- **Cohere usage shape variants** — `_call_cohere` reads `meta.tokens.input_tokens` OR `meta.billed_units.input` to handle SDK version drift.
- **Hung backend** — `_dispatch_with_timeout` enforces the per-call timeout via a future; the call surfaces as `TimeoutError`, classified as transient → retry → cascade.
- **Reasoning-token starvation (GPT-5)** — reasoning models spend `max_completion_tokens` on both reasoning and output; at the default `reasoning_effort` a moderately complex prompt can exhaust the budget on reasoning and return an empty string (`finish_reason="length"`), which downstream `_extract_json` can only degrade on. Mitigated by `reasoning_effort="minimal"` (FR-031) — this project's prompts are structured JSON extraction/classification, which needs output budget, not deliberation.

## Requirements *(mandatory)*

### Functional Requirements

**Backend detection & selection**

- **FR-001**: `_detect_backend` MUST honour an explicit `LLM_BACKEND` env var when set AND that backend has a valid key; otherwise it MUST auto-detect in order `gemini → openai → anthropic → cohere`.
- **FR-002**: Backend detection MUST be cached via `lru_cache(maxsize=1)`; the cache MUST be cleared when `set_backend` is called.
- **FR-003**: Backend keys MUST be resolved via `src.core.secrets.get_secret` (Secret Manager-aware) — never directly via `os.getenv`.
- **FR-004**: An invalid `LLM_BACKEND` value MUST log a warning and fall through to auto-detect — not raise.
- **FR-005**: When no valid key is found anywhere, `_detect_backend` MUST raise `EnvironmentError` with a multi-line Chinese instruction.

**`chat()` entrypoint**

- **FR-006**: `chat(prompt, max_tokens, mode)` MUST raise `ValueError` immediately if `prompt` is empty or whitespace-only.
- **FR-007**: When `LLM_INJECTION_GUARD` is not `"false"` (default true), system MUST prepend `_INJECTION_GUARD` to the prompt before dispatch.
- **FR-008**: `mode="demo"` MUST select the demo model per `BACKEND_MODELS[backend]["demo"]`; `mode="dev"` MUST select the dev model.
- **FR-009**: Candidate list MUST be `[primary, …other backends with valid keys in auto-detect order]`.

**Per-call timeout**

- **FR-010**: Every dispatch MUST run inside a global `ThreadPoolExecutor` and enforce a per-call timeout (default 45 s, configurable via `LLM_TIMEOUT_SECONDS`).
- **FR-011**: Timeout MUST surface as `TimeoutError` and be classified as transient.

**Cascade behavior**

- **FR-012**: When an error contains any `_QUOTA_MARKERS` token (quota / billing / 401 / 403 / 404 / 429 / 503 / rate_limit / etc.), system MUST switch to the next backend immediately — no same-backend retry.
- **FR-013**: When an error matches `_TRANSIENT_MARKERS` (connection / timeout / network / etc.) OR is a `TimeoutError`, system MUST retry the same backend up to `_MAX_SAME_BACKEND_RETRIES` times with exponential backoff (`_RETRY_BASE_DELAY × 2^(attempt−1)`).
- **FR-014**: When transient retries are exhausted on a backend, system MUST proceed to the next backend.
- **FR-015**: When all backends fail, system MUST raise `LLMUnavailableError` carrying a friendly Chinese summary and the root cause attached as `.root_cause`.

**Error classification**

- **FR-016**: `_format_quota_message` MUST classify error messages in priority order (most-specific first): quota/billing/credit → rate_limit → bare 429 → 401/403 → 404 → 503 → generic. The bare-429 catch MUST come after explicit quota markers so quota errors aren't misclassified as rate limits.
- **FR-017**: `friendly_error_message` MUST return a Chinese summary < 80 chars; it MUST NOT include raw SDK exception bodies, trace IDs, or HTTP headers.
- **FR-018**: `LLMUnavailableError.friendly_message` MUST be UI-safe (no credentials, no SDK details).

**Telemetry**

- **FR-019**: Every successful `chat()` call MUST record an `LLMCall` with `backend`, `model`, `prompt_tokens`, `completion_tokens`, `duration_ms`, `cost_usd`, `error=None`.
- **FR-020**: Every failed `chat()` call MUST record an `LLMCall` with `error=type(e).__name__`, `duration_ms` set, and tokens 0 (cost cannot inflate from failures).
- **FR-021**: The registry MUST be thread-safe (Lock-protected list).
- **FR-022**: `summary()` MUST return aggregated `total_calls`, `successful_calls`, `failed_calls`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd`, `total_duration_ms`, and `by_backend` breakdown.
- **FR-023**: `reset()` MUST clear all records atomically.

**Cost estimation**

- **FR-024**: `estimate_cost(backend, model, prompt_tokens, completion_tokens)` MUST consult `_PRICING[(backend, model)]` and return 0.0 when the key is absent (graceful degrade — no crash on unknown models).
- **FR-025**: `_PRICING` MUST be a module-level table of (input_per_1m_usd, output_per_1m_usd) per (backend, model) pair, with comments noting estimated entries that require verification.

**Per-backend dispatch contracts**

- **FR-026**: Every `_call_<backend>` MUST return `(text, prompt_tokens, completion_tokens)`. If the SDK does not expose usage metadata, the tuple's token counts MUST be 0 (recorded, not blocked).
- **FR-027**: `_call_openai_compat` MUST try `max_completion_tokens` first and fall back to `max_tokens` on TypeError or message-pattern API errors — handling both legacy and new OpenAI model API shapes.
- **FR-028**: `_call_cohere` MUST read usage from either `meta.tokens.*` (newer SDK) or `meta.billed_units.*` (older SDK).
- **FR-031**: For `gpt-5*` models, `_call_openai_compat` MUST pass `reasoning_effort` (default `"minimal"`, overridable via `LLM_REASONING_EFFORT`) so reasoning tokens cannot consume the entire `max_completion_tokens` budget and yield an empty `message.content`. Non-reasoning models (e.g. `gpt-4o`) MUST NOT receive the parameter — they reject it with a 400.

**Runtime backend control**

- **FR-029**: `available_backends()` MUST return the backend names that currently have valid keys.
- **FR-030**: `set_backend(name)` MUST validate the backend exists in `BACKEND_MODELS` AND has a valid key, then set `LLM_BACKEND` env var and clear the `_detect_backend` cache.

### Key Entities

- **Backend** — one of `openai` / `gemini` / `anthropic` / `cohere`. Each maps to a `dev` model and a `demo` model via `BACKEND_MODELS`.
- **`LLMCall`** — telemetry record: `(backend, model, prompt_tokens, completion_tokens, duration_ms, cost_usd, error)`.
- **`LLMUnavailableError`** — terminal cascade-exhausted exception carrying a UI-safe friendly message and the optional root cause.
- **Pricing entry** — `(backend, model) → (input_per_1m_usd, output_per_1m_usd)`. Absence is non-fatal.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: When the active backend hits a quota error, the next backend is invoked within **< 1 second** of the error (no retry delay) — measurable in test suite.
- **SC-002**: When a transient network error occurs, the same backend is retried **exactly once** (default `_MAX_SAME_BACKEND_RETRIES=1`) before falling through.
- **SC-003**: When all backends fail, the raised exception's string representation contains **0** API key characters, **0** trace IDs, and **0** raw HTTP body fragments.
- **SC-004**: `_format_quota_message` correctly classifies **100%** of test cases for: quota-context-429 → 配額用盡; bare-429 → 速率限制; 401 → API Key 失效; 404 → 模型不存在; 503 → 服務不可用 (verified in `tests/test_llm_client.py`).
- **SC-005**: After N concurrent `chat()` calls from a ThreadPoolExecutor, `telemetry.summary()["total_calls"]` equals N (no records lost to race conditions).
- **SC-006**: A model name not in `_PRICING` produces a recorded call with `cost_usd=0.0` and contributes 0 to summary — but is still counted in `total_calls`.
- **SC-007**: `chat("")` raises `ValueError` in **< 1 ms** (no LLM call made).
- **SC-008**: Per-call timeout is enforced within `_DEFAULT_TIMEOUT_SEC + 5 s` upper bound, regardless of SDK behavior.
- **SC-009**: Every `(backend, model)` pair in `BACKEND_MODELS` has a `_PRICING` entry — enforced by the offline unit test `tests/test_telemetry.py::TestPricingCoverage`, so a model swap cannot silently zero the cost guard.

## Assumptions

- The four supported provider SDKs (`openai`, `google.genai`, `anthropic`, `cohere`) are installed and importable in the deployment environment; missing SDKs MUST raise `ImportError` at dispatch time rather than silent failure.
- The `_PRICING` table values are point-in-time estimates (2026-05). Cost figures are usable for relative comparison and budget enforcement; absolute accuracy requires periodic re-calibration against provider pricing pages. **Quality gate**: any PR that changes `BACKEND_MODELS` MUST keep `_PRICING` in sync — enforced mechanically by SC-009's unit test (a missing entry fails the offline suite, not just review).
- The auto-detect order (`gemini → openai → anthropic → cohere`) reflects current cost preference (free tier first); reordering is a configuration change, not a code change.
- `LLM_INJECTION_GUARD` defaults to on; tests that need the raw prompt can flip it off — but production MUST keep it on.
- `_LLM_TIMEOUT_POOL` is a process-wide singleton with `atexit` shutdown; this assumes the process exits cleanly (Streamlit normally does).
- Constitution Principle II (graceful degradation across all 4 backends), Principle III (telemetry-driven cost guard), and Principle VI (UI-safe error reporting, no credential leakage, prompt injection guardrail) are mandatory acceptance criteria.
