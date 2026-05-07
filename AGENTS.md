# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# Start everything (Qdrant + Streamlit in foreground)
./start.sh

# Start Streamlit directly (assumes Qdrant is already running)
source venv/bin/activate
streamlit run src/ui/app.py --server.port 8501 --server.headless true

# Ingest PDFs into Qdrant
python scripts/run_ingestion.py               # all unprocessed PDFs
python scripts/run_ingestion.py --dry-run     # preview only
python scripts/run_ingestion.py --force       # re-ingest all
python scripts/run_ingestion.py --pdf FILE    # single file

# Migrate local Qdrant → Qdrant Cloud
python scripts/migrate_to_cloud.py

# Verify individual modules
python src/core/llm_client.py        # prints active backend + test call
python src/core/qdrant_client.py     # health check + ensure collection
python src/agent/graph.py            # compiles and prints graph structure

# Syntax check a file
source venv/bin/activate && python -m py_compile src/ui/app.py

# Start Qdrant alone (Docker)
docker start qdrant 2>/dev/null || docker run -d --name qdrant \
  -p 6333:6333 -v $(pwd)/qdrant_storage:/qdrant/storage qdrant/qdrant
```

There are no automated tests (`tests/` is empty). Manual verification is done by running the Streamlit app.

## Architecture

### Data Flow

```
PDF files (data/raw_pdfs/)
  → smart_parser.py    # pdfplumber for text; LlamaParse fallback for tables
  → chunker.py         # QA-pair splitting / sliding-window / whole-page for tables
  → embedder.py        # paraphrase-multilingual-mpnet-base-v2 (768-dim), local
  → Qdrant             # collection "earnings_calls", cosine distance
                       # payload keys: company, quarter, section, content, page

Query (Streamlit UI)
  → LangGraph Agent (7 nodes)
  → Qdrant vector search + Cohere Rerank
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

After the initial top-k retrieval, `parallel_retrieval` calls `get_company_quarters()` (Qdrant facet API) then `retrieve_coverage()` for any quarters missing from the result set. Coverage sweep uses a shared embedding vector and applies a `min_score=0.25` gate to skip quarters with no relevant content.

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

Single `chat(prompt, max_tokens, mode)` entrypoint. Three active backends — auto-detect order: `openai → gemini → cohere`. `mode="dev"` uses cheaper/faster models; `mode="demo"` uses best quality.

Models (2026-05): OpenAI `gpt-5o` / `gpt-5o-mini`, Gemini `gemini-3.0-flash`, Cohere `command-r-plus-08-2024`.

When a backend hits quota / 429 rate limit / 401-403 auth / 404 model-not-found / 503 unavailable, `chat()` prints a friendly Chinese message (e.g. `⚠️  OpenAI (GPT-5o) 今日 token / 配額已用完，自動切換下一個後端…`) and falls through to the next backend. Network/timeout errors retry once on the same backend before falling through. Non-transient errors raise immediately.

Anthropic and Groq backends were removed (no API key) — `LLM_BACKEND=anthropic` / `groq` is rejected with a warning.

### Qdrant Client (`src/core/qdrant_client.py`)

Singleton via `lru_cache`. Uses `QDRANT_URL` + `QDRANT_API_KEY` for cloud; falls back to `localhost:6333` for local Docker. All modules must import via `get_qdrant_client()` — never instantiate `QdrantClient` directly.

### Streamlit UI (`src/ui/app.py`)

**Session state pattern** — all results are stored in `st.session_state` after the agent runs so that widget interactions (radio buttons, tab clicks) don't wipe the display:
- `st.session_state["last_mode"]` — `"single"` or `"multi"`
- `st.session_state["last_result"]` / `["last_meta"]` — single-company result
- `st.session_state["last_multi_results"]` / `["last_multi_companies"]` / `["last_multi_topic"]` — multi-company
- `st.session_state["_single_pdf_bytes"]` / `["_multi_pdf_bytes"]` — cached export bytes

**Fragment pattern** — `@st.fragment` is applied to trend chart radio buttons so clicking them only re-runs the chart block, not the full page (prevents tab reset).

Multi-company mode: `run_multi_company()` in `src/core/comparison.py` uses `ThreadPoolExecutor(max_workers=2)` to run agents in parallel.

### Chart (`src/ui/chart.py`)

`build_stance_series(contradictions)` groups by `quarter_b`, takes the most significant non-zero delta per quarter, and includes "無關" quarters as `delta=0` (so the full timeline is visible). Rendered via `chart_to_scrollable_html()` which wraps the Plotly figure in `overflow-x: auto` for wide charts.

### Export (`src/ui/export.py`)

- CSV: `utf-8-sig` encoding (BOM for Excel compatibility)
- PDF: `fpdf2` + STHeiti font (`/System/Library/Fonts/STHeiti Light.ttc`). Emoji are replaced with ASCII via `_strip_emoji()` before writing because STHeiti lacks emoji glyphs.

## Environment Variables

Required in `.env`:

```
# At least one LLM key (active backends: openai / gemini / cohere)
OPENAI_API_KEY=...       # GPT-5o / GPT-5o-mini ★ primary
GEMINI_API_KEY=...       # Gemini 3.0 Flash (large free tier)
COHERE_API_KEY=...       # Command R+ (also used for Rerank)

# Optional
LLM_BACKEND=openai       # force a specific backend (openai / gemini / cohere)
QDRANT_URL=...           # Qdrant Cloud URL (omit for local Docker)
QDRANT_API_KEY=...       # Qdrant Cloud key
LLAMA_CLOUD_API_KEY=...  # enables LlamaParse for table-heavy PDFs
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
| Agent 層 | `src/agent/` | LangGraph 7 節點；orchestration only，不直接呼叫 Qdrant |
| Core 層 | `src/core/` | LLM client、Qdrant client、retriever、矛盾偵測；可獨立測試 |

關鍵設計決策：
- `get_qdrant_client()` 單例（`lru_cache`）→ 全域只有一個連線，避免資源浪費
- `AgentState` TypedDict → 節點間傳遞有型別約束，IDE 可做靜態檢查
- `@st.fragment` → UI re-render 隔離，radio 按鈕不觸發全頁重繪

### b. 程式容錯與防呆

| 位置 | 機制 | 說明 |
|---|---|---|
| `contradiction.py` `_extract_json()` | 三層 JSON parse fallback | 直接 parse → markdown fence → greedy `{}` → 降級回傳 confidence=0 |
| `contradiction.py` `batch_detect()` | 每組季度對獨立 try/except | 單一 LLM 呼叫失敗不中止整批偵測 |
| `contradiction.py` `batch_detect()` | content 空值檢查 | 兩季任一無內容 → `print` 警告後 `continue`，不進入 LLM 呼叫 |
| `contradiction.py` `detect_contradiction()` | `isinstance` 型別驗證 | 傳入非 dict 時 raise ValueError，快速暴露呼叫方錯誤 |
| `nodes.py` `parallel_retrieval()` | `as_completed` + try/except | Tavily / yfinance 個別失敗不影響 RAG 結果 |
| `app.py` Agent 執行 | try/except + Demo 快取 fallback | Agent 失敗時嘗試載入快取，保底顯示結果 |
| `app.py` `get_available_quarters()` | facet API → scroll → hardcoded fallback | Qdrant 版本不支援或未啟動時仍能運作 |
| `retriever.py` `retrieve_coverage()` | `min_score=0.25` gate | 向量相似度不足的季度不強行補充，避免雜訊 |

### c. 程式效率

| 機制 | 實作位置 | 說明 |
|---|---|---|
| **並行 I/O** | `nodes.py` `parallel_retrieval()` | `ThreadPoolExecutor(max_workers=8)`：RAG + Tavily + yfinance 三路同時發出 |
| **並行分析** | `comparison.py` `run_multi_company()` | `ThreadPoolExecutor(max_workers=2)`：多公司 Agent 同時執行 |
| **Qdrant Facet API** | `app.py` `get_available_quarters()` | v1.10+ 單次查詢取所有唯一季度值，降級才用 scroll（限 2000 筆） |
| **PDF bytes 快取** | `app.py` session_state | PDF 僅在結果改變時重新生成，以 `{mode}_{company}_{topic}` 為 key |
| **Streamlit 函數快取** | `app.py` `@st.cache_data(ttl=300)` | 季度列表快取 5 分鐘，不每次重查 Qdrant |
| **LLM token 截斷** | `contradiction.py` `_MAX_CONTENT=2000` | 每季內容上限 2000 字，防止 prompt 過長導致 API 超時或費用暴增 |
| **Coverage sweep 門檻** | `retriever.py` `min_score=0.25` | 相似度過低的季度直接跳過，不浪費 LLM 呼叫 |

### d. 適當註解

本專案註解原則：
- **模組開頭 docstring**：說明模組職責、設計決策（`why`）、不說 `what`（程式碼本身就是 `what`）
- **函數 docstring**：Args / Returns 型別、容錯行為、副作用
- **行內注釋**：標示安全標籤 `[f]`、容錯標籤 `[b]`、效能標籤 `[c]`，方便 code review 快速定位
- **TODO/FIXME 禁用**：已知技術債以 GitHub Issue 追蹤，不散落在程式碼中

安全相關注釋統一標記為 `# [f]`，例如：
```python
# [f] 對任意值套用 html.escape，防止 XSS
return html.escape(str(val)) if val is not None else ""
```

### e. 能解釋每一行程式碼

關鍵設計選擇說明：

**為何用 LLM 做矛盾偵測而非規則？**
「需求強勁」→「庫存調整」的語氣轉變需要語境理解，if/else 規則無法處理細微措辭變化。LLM 回傳 structured JSON（含 `confidence`），讓 Self-Reflection 可量化評估。

**為何 `batch_detect` 只比相鄰季度而非全組合？**
N 季有 N*(N-1)/2 組合，10 季 = 45 次 LLM 呼叫。相鄰比對（N-1 次）在偵測「趨勢轉變」上已足夠，且 cost 線性而非平方。

**為何 `agent.stream()` 而非 `agent.invoke()`？**
`stream_mode="updates"` 讓每個節點完成後立即推送 state diff 到 UI，使用者能即時看到 Agent 進度，不需等到全部完成。

**為何 `operator.add` 在 `steps_log`？**
LangGraph 預設後節點的 state key 會覆蓋前節點。`Annotated[list, operator.add]` 改為累加語意，每個節點的 log 都能保留，不互相覆蓋。

### f. 高安全性

| 威脅 | 防禦措施 | 實作位置 |
|---|---|---|
| **XSS（跨站腳本）** | 所有 LLM 輸出插入 HTML 前統一 `html.escape()` | `app.py` `_sanitize_str()` + `[f]` 標記 |
| **Prompt Injection** | 使用者輸入截斷至 500 字 + 移除 HTML 標籤 | `app.py` `run_btn` 區塊 |
| **API 濫用 / DoS** | Rate limiting：兩次查詢間最短 10 秒冷卻 | `app.py` `_COOLDOWN_SEC = 10` |
| **非法參數注入** | 公司 / 主題白名單驗證（即使 Streamlit widget 被繞過） | `app.py` `if company not in COMPANIES` |
| **Token 爆炸攻擊** | LLM prompt 內容截斷（每季 2000 字上限） | `contradiction.py` `_MAX_CONTENT` |
| **Qdrant 注入** | 公司名稱只能從 COMPANIES 列表取得，不允許任意字串 filter | `app.py` 白名單驗證 |
| **投資建議責任** | UI 明顯標示「不提供選股建議」+ 頁腳免責聲明 | `app.py` sidebar + footer |
