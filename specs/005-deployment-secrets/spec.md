# Feature Specification: Deployment Secrets & GCP Credentials

**Feature Branch**: `005-deployment-secrets`

**Created**: 2026-06-11

**Status**: Draft (as-built — reverse-engineered from `src/core/secrets.py` + `src/core/bq_client.py`, commits `3e623c6` / `4207105` plus the worktree hardening pass)

**Input**: User description: "As-built spec for the deployment-credentials subsystem — st.secrets → os.environ bridge, BigQuery service-account credentials from Streamlit secrets, project-ID resolution order, and the secrets.toml deploy template contract."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Deploy to Streamlit Community Cloud with st.secrets only (Priority: P1)

An operator deploys EarningsWatch to Streamlit Community Cloud, where there is **no ADC and no shell environment** — the only secret channel is `st.secrets`. After pasting the secrets (LLM keys, `GOOGLE_CLOUD_PROJECT`, a `[gcp_service_account]` table), the app boots, BigQuery vector search works, and every `os.getenv()`-reading code path (LLM cascade, LangChain tracing, `GCP_SECRET_PROJECT` detection) sees its keys — without changing any consumer code.

**Why this priority**: This is the only zero-infrastructure public deployment path for the project. Without it, demos require a GCP runtime.

**Independent Test**: With a fake `streamlit` module whose `secrets` contains top-level string keys and a `gcp_service_account` table, boot the bridge + `get_bq_client()`. Assert env vars are populated and the BigQuery client is constructed with service-account credentials.

**Acceptance Scenarios**:

1. **Given** `st.secrets` contains top-level string keys, **When** `bridge_streamlit_secrets_to_env()` runs at startup, **Then** each key is mirrored into `os.environ`.
2. **Given** `st.secrets["gcp_service_account"]` exists, **When** `get_bq_client()` runs, **Then** the client is built from `service_account.Credentials.from_service_account_info(...)` — not ADC.
3. **Given** the operator set `[gcp_service_account]` but forgot a top-level `GOOGLE_CLOUD_PROJECT`, **When** the project ID resolves, **Then** the SA key's own `project_id` is used — the client and `get_table_path()` agree on the same project.

---

### User Story 2 — Local development precedence is preserved (Priority: P1)

A developer runs the app locally with `.env` (or shell exports) and no `secrets.toml`. Nothing changes: `.env` values win, the bridge is a silent no-op, and BigQuery uses ADC. If the developer *also* has a `secrets.toml`, any env var already set stays untouched — secrets only fill gaps.

**Why this priority**: A deployment convenience must never alter local-dev semantics; silent value swaps are the worst kind of config bug.

**Independent Test**: Set an env var, then run the bridge with a fake `st.secrets` carrying a different value for the same key. Assert the env var is unchanged. Run the bridge with no secrets file. Assert zero output and zero env mutations.

**Acceptance Scenarios**:

1. **Given** an env var is already set and non-blank, **When** the bridge runs, **Then** the secret value does NOT overwrite it.
2. **Given** no `secrets.toml` exists (Streamlit raises its secret-not-found error), **When** the bridge runs, **Then** it is a silent no-op — no warning, no crash.
3. **Given** no `gcp_service_account` secret, **When** `get_bq_client()` runs, **Then** the client is constructed without explicit credentials (ADC path).

---

### User Story 3 — Misconfiguration is diagnosable, not silent (Priority: P2)

When the deployment is half-configured, the operator gets a signal pointing at the actual problem instead of a downstream mystery: a malformed `secrets.toml` prints a warning (instead of silently presenting as "all keys missing"); a present-but-corrupt service-account blob fails loudly at client construction (instead of silently falling back to ADC and failing later with a confusing auth error).

**Why this priority**: Constitution Principle II demands graceful degradation, but degradation that *hides* an operator error violates the spirit — fail-soft for absent config, fail-loud for broken config.

**Independent Test**: Run the bridge with a `st.secrets` whose parse raises a non-`FileNotFoundError`. Assert exactly one warning is printed and no exception escapes. Call `_credentials_from_streamlit_secrets()` with a malformed SA dict. Assert it raises.

**Acceptance Scenarios**:

1. **Given** `secrets.toml` exists but cannot be parsed (TOML syntax error), **When** the bridge runs, **Then** a warning naming the exception type is printed and startup continues on `.env` / env vars only.
2. **Given** `gcp_service_account` exists but its contents are not a valid key, **When** credentials are built, **Then** the error propagates (no silent ADC fallback).

---

### Edge Cases

- **Unquoted TOML numbers** (`LLM_BUDGET_USD = 0.5`) — not strings, silently skipped by the bridge. The deploy template instructs operators to quote every value.
- **TOML tables** (`[gcp_service_account]`) — never mirrored to env; `bq_client` reads them from `st.secrets` directly.
- **Whitespace-only env var** — treated as unset; the secret value fills it.
- **`streamlit` not importable** — bridge and credential lookup both return/no-op safely (core stays importable without the UI stack).
- **`StreamlitSecretNotFoundError`** — subclasses `FileNotFoundError` (verified on Streamlit 1.50), so the "no secrets file" silent path catches it.
- **Import-order dependency** — `bq_client.PROJECT_ID` is resolved at module import; `app.py` MUST run the bridge before importing any `src.core`/`src.ui` module. (Mitigated: project-ID resolution also consults the SA secret directly, so even a late bridge cannot produce a client/table-path mismatch.)
- **Non-Streamlit entrypoints** (`run_ingestion.py`, module self-checks) — never run the bridge; they rely on `.env` + ADC as before.

## Requirements *(mandatory)*

### Functional Requirements

**st.secrets → os.environ bridge** (`secrets.py:bridge_streamlit_secrets_to_env`)

- **FR-001**: At app startup — after `load_dotenv`, before any `src.core` module import — system MUST mirror every top-level *string* secret from `st.secrets` into `os.environ`.
- **FR-002**: The bridge MUST NOT overwrite an env var that is already set and non-blank (`.env` / shell always win locally).
- **FR-003**: Non-string values (TOML tables, numbers, booleans) MUST be skipped, not coerced.
- **FR-004**: When no secrets file exists (`FileNotFoundError`, including Streamlit's subclass), the bridge MUST be a silent no-op.
- **FR-005**: Any other read/parse failure MUST print a single warning naming the exception type and continue startup — never crash the boot, never print secret values.

**BigQuery credentials** (`bq_client.py`)

- **FR-006**: When `st.secrets["gcp_service_account"]` is present, `get_bq_client()` MUST construct the client from `service_account.Credentials.from_service_account_info(...)`; otherwise it MUST fall back to ADC.
- **FR-007**: A present-but-malformed `gcp_service_account` blob MUST fail loudly at credential construction — silent ADC fallback on broken config is prohibited (it relocates the error somewhere harder to diagnose).
- **FR-008**: The SA lookup MUST degrade safely to `None` when `streamlit` is not importable or `st.secrets` access fails — core modules MUST remain importable and testable without a Streamlit runtime.

**Project-ID resolution**

- **FR-009**: The active GCP project MUST resolve in order: `GOOGLE_CLOUD_PROJECT` env var → `project_id` inside the SA secret → `"earningswatch-demo"` default. The BigQuery client and `get_table_path()` MUST derive from the same resolved value (a client/table-path project mismatch is unrepresentable).

**Deploy template contract** (`.streamlit/secrets.toml.example`)

- **FR-010**: The template MUST document every supported key, the quoted-string requirement, the top-level-keys-before-table TOML ordering rule, a complete `[gcp_service_account]` skeleton, and the local-dev instruction to delete the SA block entirely. It MUST NOT list retired integrations (Qdrant, Groq).

### Key Entities

- **Secrets bridge** — the one-shot startup mirror from `st.secrets` top-level strings into `os.environ`.
- **SA secret** — the `[gcp_service_account]` TOML table holding a GCP service-account key JSON, consumed directly from `st.secrets` (never via env).
- **Resolved project ID** — the single project string shared by the BigQuery client and all SQL table paths.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A Streamlit Cloud deployment configured *only* via `st.secrets` (no env vars, no ADC) passes the BigQuery health check and completes an end-to-end query.
- **SC-002**: For env vars already set locally, **0%** are overwritten by the bridge (verified by unit test).
- **SC-003**: A missing `secrets.toml` produces **0** lines of output; a malformed one produces exactly **1** warning and **0** crashes.
- **SC-004**: A deploy that sets `[gcp_service_account]` but omits `GOOGLE_CLOUD_PROJECT` still queries the correct project (SA `project_id` fallback, verified by unit test).
- **SC-005**: All bridge and credential paths are covered by offline unit tests (no Streamlit runtime, no GCP) via injected fake `streamlit` modules — currently `tests/test_secrets.py::TestBridgeStreamlitSecretsToEnv` + `tests/test_bq_vector_search.py::TestStreamlitSecretsCredentials`.

## Assumptions

- `StreamlitSecretNotFoundError` subclasses `FileNotFoundError` (true on Streamlit 1.50; revisit on major Streamlit upgrades).
- The `bq_client` → `streamlit` guarded soft-import is the **single sanctioned exception** to the core-layer independence rule (recorded in CLAUDE.md and constitution Principle I); no other core module may import `streamlit`.
- Cloud Run / GCE deployments continue to use ADC + `--set-secrets` env injection; this subsystem only adds the Streamlit Cloud path, it does not replace existing ones.
- Constitution Principle II (fail-soft on absent config) and Principle VI (no secret values in logs or warnings) are mandatory acceptance criteria for any change here.
