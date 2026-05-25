# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# Start Streamlit (no local DB needed — vector store is GCP BigQuery)
./start.sh

# Start Streamlit directly
streamlit run src/ui/app.py --server.port 8501 --server.headless true

# Ingest PDFs into BigQuery
python scripts/run_ingestion.py               # all unprocessed PDFs
python scripts/run_ingestion.py --dry-run     # preview only
python scripts/run_ingestion.py --force       # re-ingest all
python scripts/run_ingestion.py --pdf FILE    # single file

# Verify individual modules
python src/core/llm_client.py        # prints active backend + test call
python src/core/bq_client.py         # health check + ensure dataset/table
python src/agent/graph.py            # compiles and prints graph structure

# Syntax check a file
python -m py_compile src/ui/app.py
```

Tests live in `tests/`:

```bash
# Run unit tests (offline; no API keys needed)
pytest tests/ -v

# Run end-to-end benchmark (30 questions, requires LLM API + BigQuery)
python tests/benchmark.py

# Benchmark with RAGAS metrics (faithfulness / relevancy / precision)
pip install -r requirements-dev.txt
python tests/benchmark.py --ragas --ragas-sample 5  # smoke test on 5 queries
python tests/benchmark.py --ragas                   # full RAGAS run (~$1 USD)
```

## Architecture

### Data Flow

```
PDF files (data/raw_pdfs/)
  → smart_parser.py    # pdfplumber for text; LlamaParse fallback for tables
  → chunker.py         # QA-pair splitting / sliding-window / whole-page for tables
  → embedder.py        # gemini-embedding-2 (768-dim, Vertex AI), MRL truncated
  → BigQuery           # table "earnings_data.earnings_calls", cosine distance
                       # columns: company, quarter, section, content, page, embedding

Query (Streamlit UI)
  → LangGraph Agent (7 nodes)
  → BigQuery VECTOR_SEARCH + Cohere Rerank
  → LLM contradiction detection
  → Streamlit display + CSV/PDF export
```

### LangGraph Agent (`src/agent/`)

Seven nodes in `graph.py`, implemented in `nodes.py`, typed via `state.py`:

| Node | Function | Key behaviour |
|---|---|---|
| `classify` | `intent_classifier` | Extracts company/topic/quarters from query (uses UI values directly if set) |
| `decompose` | `query_decomposer` | Breaks query into sub-queries per quarter |
| `route` | `dynamic_tool_router` | Decides which tools to use (RAG / news / stock) |
| `retrieve` | `parallel_retrieval` | Vector search + **coverage sweep** for missing quarters |
| `detect` | `contradiction_detect` | Calls `batch_detect()` + `detect_promises()` |
| `reflect` | `self_reflect` | Scores confidence; loops back to `retrieve` (max 3 iterations) |
| `report` | `report_generator` | Produces final Markdown report |

The conditional edge `reflect → retrieve` (retry) or `reflect → report` (end) is the self-reflection loop.

### Coverage Sweep (`src/core/retriever.py`)

After the initial top-k retrieval, `parallel_retrieval` calls `get_company_quarters()` (BigQuery `SELECT DISTINCT quarter`) then `retrieve_coverage()` for any quarters missing from the result set. Coverage sweep uses a shared embedding vector and applies a `min_score=0.25` gate to skip quarters with no relevant content.

### Contradiction Detection (`src/core/contradiction.py`)

`batch_detect(retrieved, topic)` sends sequential quarter-pair comparisons to the LLM (all pairs formed from sorted quarter keys). Each comparison returns structured JSON:
- `stance_change`: 更樂觀 / 維持不變 / 更保守 / 無關
- `has_contradiction`: bool
- `evidence_early` / `evidence_later`: quoted text
- `confidence`: float

`detect_promises(retrieved)` does a separate LLM pass looking for forward guidance and whether it was met.

Boilerplate filtering (two layers):
1. `min_score=0.25` in `retrieve_coverage()` — retrieval gate
2. `ev_a == ev_b` identity check in the UI and `build_stance_series()` — removes repeated legal disclaimers

### LLM Backend (`src/core/llm_client.py`)

Single `chat(prompt, max_tokens, mode)` entrypoint. Four active backends — auto-detect order: `gemini → openai → anthropic → cohere`. `mode="dev"` uses cheaper/faster models; `mode="demo"` uses best quality.

Models (2026-05): OpenAI `gpt-5` / `gpt-5-mini`, Gemini `gemini-2.5-flash`, Anthropic `claude-sonnet-4-6` / `claude-haiku-4-5-20251001`, Cohere `command-r-plus-08-2024`.

When a backend hits quota / 429 rate limit / 401-403 auth / 404 model-not-found / 503 unavailable, `chat()` prints a friendly Chinese message and falls through to the next backend. Groq is rejected with a warning.

### BigQuery Client (`src/core/bq_client.py`)

Singleton via `lru_cache`. Uses Application Default Credentials (ADC). `GOOGLE_CLOUD_PROJECT` sets the project (fallback: `"earningswatch-demo"`). All modules must import via `get_bq_client()` — never instantiate `bigquery.Client` directly. Table path: `{project}.earnings_data.earnings_calls`.

### Secrets Management (`src/core/secrets.py`)

`get_secret(name)` resolves API keys in order:
1. If `GCP_SECRET_PROJECT` is set → GCP Secret Manager
2. Otherwise → `os.environ` / `.env`

### Streamlit UI (`src/ui/app.py`)

`app.py` is a thin shell. Rendering lives in `src/ui/views/single.py` and `src/ui/views/multi.py`.

**UIState pattern** (`src/ui/state.py`) — all session fields in a single `@dataclass`; `UIState.get()` returns the singleton stored in `st.session_state["_ui_state"]`.

Multi-company mode: `run_multi_company()` in `src/core/comparison.py` uses `ThreadPoolExecutor(max_workers=2)` to run agents in parallel.

### Chart (`src/ui/chart.py`)

`build_stance_series(contradictions)` groups by `quarter_b`, takes the most significant non-zero delta per quarter, and includes "無關" quarters as `delta=0` (so the full timeline is visible).

### Export (`src/ui/export.py`)

- CSV: `utf-8-sig` encoding (BOM for Excel compatibility)
- PDF: `fpdf2` + CJK-font cascade (`_resolve_cjk_font()`). Falls back STHeiti → Noto → WQY. Raises `RuntimeError` with remediation message if no font found. Emoji stripped via `_strip_emoji()`.

## Environment Variables

Required in `.env`:

```
# GCP (required for BigQuery + Vertex AI embedding)
GOOGLE_CLOUD_PROJECT=earningswatch-demo

# At least one LLM key (active backends: gemini / openai / anthropic / cohere)
GEMINI_API_KEY=...       # Gemini 2.5 Flash (large free tier) ★ primary
OPENAI_API_KEY=...       # GPT-5 / GPT-5-mini
ANTHROPIC_API_KEY=...    # Claude Sonnet 4.6 / Haiku 4.5 (paid)
COHERE_API_KEY=...       # Command R+ (also used for Rerank)

# Optional
LLM_BACKEND=gemini       # force a specific backend
GCP_SECRET_PROJECT=...   # GCP project for Secret Manager (omit to use .env)
LLAMA_CLOUD_API_KEY=...  # enables LlamaParse for table-heavy PDFs
COVERAGE_MIN_SCORE=0.25  # coverage sweep cosine-similarity gate
```

## PDF Ingestion Filename Conventions

Two formats are recognised by `run_ingestion.py`:
- **MOPS format**: `{4-digit stock code}{YYYYMMDD}{M|E}{3-digit seq}.pdf` — e.g. `233020260115M001.pdf`
- **English transcript**: `TSMC {Q}{2-digit year} Transcript{suffix}.pdf` — e.g. `TSMC 4Q25 Transcript.pdf`

Place PDFs in `data/raw_pdfs/`. Processed state is tracked in `data/processed/ingestion_log.json`.

## 程式規範對應

### a. 程式架構合理性

層次分明的三層架構，各層職責單一、不互相滲透：

| 層 | 目錄 | 職責 |
|---|---|---|
| UI 層 | `src/ui/` | Streamlit 頁面、圖表、匯出；不含任何業務邏輯 |
| Agent 層 | `src/agent/` | LangGraph 7 節點；orchestration only，不直接呼叫 BigQuery |
| Core 層 | `src/core/` | LLM client、BigQuery client、retriever、矛盾偵測；可獨立測試 |

關鍵設計決策：
- `get_bq_client()` 單例（`lru_cache`）→ 全域只有一個連線，避免資源浪費
- `AgentState` TypedDict → 節點間傳遞有型別約束，IDE 可做靜態檢查
- `@st.fragment` → UI re-render 隔離，radio 按鈕不觸發全頁重繪

### b. 程式容錯與防呆

| 位置 | 機制 | 說明 |
|---|---|---|
| `contradiction.py` `_extract_json()` | 三層 JSON parse fallback | 直接 parse → markdown fence → greedy `{}` → 降級回傳 confidence=0 |
| `contradiction.py` `batch_detect()` | 每組季度對獨立 try/except | 單一 LLM 呼叫失敗不中止整批偵測 |
| `nodes.py` `parallel_retrieval()` | `as_completed` + try/except | Tavily / yfinance 個別失敗不影響 RAG 結果 |
| `app.py` Agent 執行 | try/except + Demo 快取 fallback | Agent 失敗時嘗試載入快取，保底顯示結果 |
| `quarters.py` `get_available_quarters()` | BigQuery SELECT DISTINCT → hardcoded fallback | BQ 連線失敗時仍能顯示預設季度列表 |
| `retriever.py` `retrieve_coverage()` | `min_score=0.25` gate | 向量相似度不足的季度不強行補充，避免雜訊 |

### c. 程式效率

| 機制 | 實作位置 | 說明 |
|---|---|---|
| **並行 I/O** | `nodes.py` `parallel_retrieval()` | `ThreadPoolExecutor(max_workers=8)`：RAG + Tavily + yfinance 三路同時發出 |
| **並行分析** | `comparison.py` `run_multi_company()` | `ThreadPoolExecutor(max_workers=2)`：多公司 Agent 同時執行 |
| **BigQuery SELECT DISTINCT** | `quarters.py` `get_available_quarters()` | 單次 SQL 查詢取所有唯一季度值，BQ 失敗才降級 hardcoded list |
| **Streamlit 函數快取** | `quarters.py` `@st.cache_data(ttl=300)` | 季度列表快取 5 分鐘，不每次重查 BigQuery |
| **LLM token 截斷** | `contradiction.py` `_MAX_CONTENT=2000` | 每季內容上限 2000 字，防止 prompt 過長導致 API 超時或費用暴增 |
| **Coverage sweep 門檻** | `retriever.py` `min_score=0.25` | 相似度過低的季度直接跳過，不浪費 LLM 呼叫 |

### d. 適當註解

本專案註解原則：
- **模組開頭 docstring**：說明模組職責、設計決策（`why`）、不說 `what`
- **函數 docstring**：Args / Returns 型別、容錯行為、副作用
- **行內注釋**：標示安全標籤 `[f]`、容錯標籤 `[b]`、效能標籤 `[c]`

### e. 高安全性

| 威脅 | 防禦措施 | 實作位置 |
|---|---|---|
| **XSS（跨站腳本）** | 所有 LLM 輸出插入 HTML 前統一 `html.escape()` | `app.py` `_sanitize_str()` |
| **Prompt Injection** | 使用者輸入截斷至 500 字 + 移除 HTML 標籤 | `app.py` |
| **API 濫用 / DoS** | Rate limiting：兩次查詢間最短 10 秒冷卻 | `rate_limiter.py` |
| **非法參數注入** | 公司 / 主題白名單驗證 | `app.py` |
| **Token 爆炸攻擊** | LLM prompt 內容截斷（每季 2000 字上限） | `contradiction.py` `_MAX_CONTENT` |
| **BigQuery SQL injection** | SQL 使用 parameterized query；公司名稱走白名單 | `quarters.py` |
| **API key 洩漏** | GCP Secret Manager 整合；LLM 錯誤訊息脫敏 | `secrets.py` + `llm_client.py` |
