# EarningsWatch — 專案總覽（Onboarding 文件）

> **對象**：下一位接手的開發者 / PM
> **版本**：v1.1（2026-05-03 更新）
> **作者**：原始作者 + Agentic RAG Hardening Patch
> **GitHub**：https://github.com/WayneSHC/EarningsWatch

本文件目標：讓新成員在 **30 分鐘內** 理解整個專案的「為什麼、做什麼、怎麼跑、改在哪」，並能獨立進行小型維護（換 LLM Key、加新公司、修 bug）。

---

## 1. 專案是什麼？一句話版本

> **法說會 Agentic RAG 一致性審計平台**：吃進上市公司每季法說會 PDF，自動用 LangGraph 7 節點 Agent 跨季比對管理層說法，找出**矛盾、未兌現的承諾、立場轉變**，並用趨勢圖呈現。

**痛點**：法人/分析師要手動讀 8 季逐字稿才能比較管理層的口風是否一致。
**解法**：把這個 Workflow 自動化成一個 Agent，3 分鐘出報告。
**特色**：**不是普通 RAG**——LLM 會自我反思（Self-Reflection），信心不足時自動重查。

---

## 2. 你需要先理解的 5 個關鍵字

| 關鍵字 | 30 秒解釋 |
|---|---|
| **Agentic RAG** | RAG（檢索增強生成）+ Agent（會自己決定下一步），LangGraph 編排 |
| **LangGraph StateGraph** | 用「節點 + 條件邊」描述 Agent 工作流，可做 if/loop（不像 LangChain 只能線性） |
| **Self-Reflection** | Agent 評估自己給的答案夠不夠好，不夠就跳回 retrieve 節點重查（最多 3 輪）|
| **Coverage Sweep** | 第一輪檢索若漏掉某些季度，第二輪用 facet API 查 missing quarters 補回來 |
| **Contradiction Detection** | LLM 比對 Q-N 與 Q-N+1 的發言，回 structured JSON：`stance_change`、`evidence` |

---

## 3. 整體架構

### 3.1 資料流

```
┌─────────────── 離線（PDF 匯入）────────────────┐
│ data/raw_pdfs/*.pdf                          │
│   ↓ smart_parser.py    pdfplumber + LlamaParse fallback
│   ↓ chunker.py         QA-pair / sliding-window / 整頁（表格）
│   ↓ embedder.py        gemini-embedding-2 (768d, Vertex AI)
│   ↓ BigQuery table "earnings_data.earnings_calls"   columns: company/quarter/section/content/page/embedding
└────────────────────────────────────────────────┘

┌─────────────── 線上（使用者查詢）──────────────┐
│ Streamlit UI (port 8501)                      │
│   ↓ run_agent(query, company, topic, quarters)│
│ LangGraph 7-node Agent                        │
│   ① classify   抽 company/topic/quarters       │
│   ② decompose  拆成多個 sub-query（含季度範圍） │
│   ③ route      LLM 決定要用哪些工具             │
│   ④ retrieve   並行（BigQuery + Tavily + yfinance）│
│   │            + Coverage Sweep 補漏掉的季度    │
│   ⑤ detect     LLM 配對偵測矛盾 + 承諾兌現      │
│   ⑥ reflect    confidence < 0.75 → 跳回 ④      │
│   ⑦ report     生成 Markdown 報告               │
│   ↓                                            │
│ 矛盾卡片 / 承諾追蹤 / 趨勢圖 / CSV / PDF 匯出  │
└────────────────────────────────────────────────┘
```

### 3.2 目錄結構

```
EarningsWatch/
├── src/
│   ├── ui/                    # Streamlit 層（無業務邏輯）
│   │   ├── app.py             #   薄殼主介面（sidebar / dispatch）
│   │   ├── views/single.py    #   單公司結果渲染
│   │   ├── views/multi.py     #   多公司比較結果渲染
│   │   ├── state.py           #   UIState dataclass（session state 集中）
│   │   ├── quarters.py        #   BigQuery SELECT DISTINCT 動態季度列表
│   │   ├── chart.py           #   Plotly 立場趨勢圖
│   │   └── export.py          #   CSV (utf-8-sig) / PDF (fpdf2 + CJK 字型 cascade)
│   ├── agent/                 # LangGraph Orchestration（不直接碰 BigQuery）
│   │   ├── graph.py           #   StateGraph 建構 + LangSmith 開關
│   │   ├── nodes.py           #   7 個節點實作
│   │   ├── state.py           #   AgentState TypedDict
│   │   └── tools.py           #   Tavily / yfinance / LLM tool router
│   ├── core/                  # 業務邏輯（可獨立測試）
│   │   ├── llm_client.py      #   多後端 fallback cascade ★
│   │   ├── bq_client.py       #   BigQuery 單例 client（lru_cache）
│   │   ├── retriever.py       #   VECTOR_SEARCH + Cohere Rerank + Coverage Sweep
│   │   ├── contradiction.py   #   batch_detect + detect_promises ★
│   │   ├── comparison.py      #   多公司並行（max_workers=2）
│   │   ├── secrets.py         #   GCP Secret Manager / .env 橋接
│   │   └── telemetry.py       #   token / cost / latency 統計
│   └── ingestion/             # PDF → BigQuery 流水線
│       ├── smart_parser.py    #   pdfplumber 為主，表格密集頁 fallback LlamaParse
│       ├── chunker.py
│       └── embedder.py        #   gemini-embedding-2 + BigQuery insert
├── scripts/
│   ├── run_ingestion.py       # 匯入 PDF（支援 --dry-run / --force / --pdf）
│   └── setup_gcp_secrets.sh   # 一鍵建立 Secret Manager 條目
├── data/
│   ├── raw_pdfs/              # PDF 來源（gitignored）
│   └── processed/             # ingestion_log.json 追蹤已處理檔（gitignored）
├── docs/
│   ├── system_architecture.md # 詳細架構（v1.0）
│   └── PROJECT_OVERVIEW.md    # ★ 本文件（v1.1）
├── .streamlit/
│   ├── config.toml            # 安全/主題設定（進 git）
│   └── secrets.toml.example   # Cloud 部署用（範本，真實檔不進 git）
├── .env.example               # 本地開發環境變數範本
├── CLAUDE.md                  # Claude Code 用指引（程式規範對應 a-f）
├── README.md                  # 公開 README（中文，使用導向）
├── requirements.txt
└── start.sh                   # 一鍵啟動（Streamlit）
```

---

## 4. 7 個節點各自做什麼？（必讀）

| # | 節點 | 檔案位置 | 一句話職責 | 可能失敗點 |
|---|---|---|---|---|
| 1 | `classify` | `nodes.py:intent_classifier` | 從自然語言抽 company / topic / quarters；UI 已填則直接用 | LLM JSON parse 失敗 → fallback 到 `_extract_json` |
| 2 | `decompose` | `nodes.py:query_decomposer` | 把問題拆成「每季一個 sub-query」；**會把選定季度傳給 LLM 防止年份幻覺** | LLM 寫出未列出的年份（v1.1 已修） |
| 3 | `route` | `nodes.py:dynamic_tool_router` 呼叫 `tools.py:decide_tools` | LLM 看 TOOL_SPECS 決定加 tavily / yfinance；bigquery 一律必選 | LLM 回非法 JSON → 降級為 `decide_tools_by_keyword` |
| 4 | `retrieve` | `nodes.py:parallel_retrieval` | `ThreadPoolExecutor(max_workers=8)` 並行三路；自動 Coverage Sweep | Tavily/yfinance 失敗不影響 RAG |
| 5 | `detect` | `contradiction.py:batch_detect` + `detect_promises` | 相鄰季度兩兩配對送 LLM 比對；structured JSON | tenacity RetryError 包真實錯誤（v1.1 用 `_unwrap()` 還原）|
| 6 | `reflect` | `nodes.py:self_reflect` | 給 confidence 分數；< 0.75 且 iteration < 3 → 跳回 retrieve | — |
| 7 | `report` | `nodes.py:report_generator` | 組 Markdown 報告 | — |

**條件邊**：`reflect → should_continue() → "retry" | "end"`，這就是 Agentic 的核心。

---

## 5. v1.1 重點變更（2026-05 Hardening Patch）

> 這次 patch 解決「報告全部 40% confidence」的根因：所有 LLM 呼叫被 `tenacity.RetryError` 包住，看不到真正錯因（其實是 Gemini 配額用盡）。順便補上多後端自動 fallback。

| 變更 | 檔案 | 為什麼 |
|---|---|---|
| **`_unwrap(e)` 解包 RetryError** | `core/contradiction.py` | tenacity 會把真正的 API 錯誤包在 `e.last_attempt.exception()` 裡，不解包就只看到無資訊的 RetryError |
| **多後端 fallback cascade** | `core/llm_client.py` | OpenAI 配額用盡 → Gemini → Anthropic → Cohere，**配額/404/credit-out 都會觸發切換** |
| **`_QUOTA_MARKERS` 字串清單** | `core/llm_client.py` | 任何錯誤訊息含 `RESOURCE_EXHAUSTED` / `quota` / `credit balance` / `404` / `503` / `529` 都會 cascade |
| **Coverage Sweep + min_score=0.25** | `core/retriever.py` | 相似度太低的季度直接跳過，避免拿無關內容硬填補 |
| **Decomposer 傳入 quarters** | `agent/nodes.py:query_decomposer` | 之前 LLM 自己腦補「2024年第一季」，現在 prompt 明確列出「季度範圍：2025Q3, 2025Q4, 2026Q1」 |
| **BigQuery 遷移** | `src/core/bq_client.py`, `src/core/retriever.py` | 向量存儲由本機 Qdrant 遷移至 GCP BigQuery VECTOR_SEARCH，無需本機 Docker |
| **LLM-driven tool router** | `agent/tools.py` | 取代純關鍵字匹配；TOOL_SPECS schema 給 LLM 看，回 JSON tools list |

詳細 commit：`2f73854 feat: agentic RAG hardening — multi-LLM cascade, coverage sweep, error unwrap`

---

## 6. 30 分鐘把專案跑起來

### 6.1 前置需求

- macOS / Linux（Windows 用 WSL2）
- Python 3.10+
- GCP 帳號（BigQuery + Vertex AI 用；本地開發設定 ADC）
- **至少一個** LLM API Key

### 6.2 步驟

```bash
# 1. Clone
git clone https://github.com/WayneSHC/EarningsWatch.git
cd EarningsWatch

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 環境變數
cp .env.example .env
# 填入 GOOGLE_CLOUD_PROJECT 和至少一個 LLM Key（推薦 GEMINI_API_KEY 免費）

# 4. GCP ADC 認證（本地開發）
gcloud auth application-default login

# 5. 把 PDF 放進去
cp ~/Downloads/TSMC*.pdf data/raw_pdfs/

# 6. Ingestion（第一次跑）
python scripts/run_ingestion.py --dry-run   # 預覽要處理什麼
python scripts/run_ingestion.py             # 真的匯入（寫入 BigQuery）

# 7. 啟動
./start.sh                                   # Streamlit
# 瀏覽器自動開 http://localhost:8501
```

### 6.3 模組單獨驗證（不啟動全 stack）

```bash
python src/core/llm_client.py     # 列出當前 backend + 試打一次
python src/core/bq_client.py      # health check + ensure dataset/table
python src/agent/graph.py         # 驗證 graph 編譯成功
```

---

## 7. 環境變數一覽

> 完整版見 `.env.example`。**真實 Key 永遠不可進 git**，`.env` 已在 `.gitignore`。

### 7.0 GCP 必填

| 變數 | 說明 |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | GCP 專案 ID（BigQuery + Vertex AI 用）|

### 7.1 LLM（擇一）

| 變數 | 模型 | 取得 | 備註 |
|---|---|---|---|
| `GEMINI_API_KEY` | Gemini 2.5 Flash | https://aistudio.google.com/apikey | ⭐ 免費額度大，auto-detect 首選 |
| `OPENAI_API_KEY` | GPT-5 / GPT-5-mini | https://platform.openai.com/api-keys | 付費 |
| `ANTHROPIC_API_KEY` | Claude Sonnet 4.6 / Haiku 4.5 | https://console.anthropic.com | 付費（2026-05 回歸）|
| `COHERE_API_KEY` | Command R+ | https://dashboard.cohere.com | 同時用於 Rerank |

> 🔄 自動降級順序：`gemini → openai → anthropic → cohere`；任一後端 quota / 429 / 401-403 / 404 / 503 → 印出友善中文訊息並切換下一個。

### 7.2 選填

| 變數 | 用途 |
|---|---|
| `LLM_BACKEND` | 強制指定主後端（不填則自動偵測）|
| `GCP_SECRET_PROJECT` | GCP Secret Manager 專案 ID（不設則讀 .env）|
| `TAVILY_API_KEY` | 啟用即時新聞搜尋 |
| `LLAMA_CLOUD_API_KEY` | 啟用 LlamaParse 處理表格密集 PDF |
| `LANGSMITH_TRACING` + `LANGSMITH_API_KEY` | 啟用 LangSmith 觀測 |
| `APP_PASSWORD` | Cloud 部署時加密碼保護 |
| `COVERAGE_MIN_SCORE` | coverage sweep 相似度門檻（預設 0.25）|
| `LLM_BUDGET_USD` | 每次查詢 LLM 預算上限（預設 0.50 USD）|

---

## 8. 常見維護任務

### 8.1 加一家新公司

1. 把 PDF 放 `data/raw_pdfs/`，檔名遵守 MOPS 格式或英文 `{COMPANY} {Q}{YY} Transcript.pdf`
2. 編輯 `src/ui/app.py` 的 `COMPANIES` 白名單（**不加進去 UI 不會出現**）
3. 若是非美股，編輯 `src/agent/tools.py` 的 `STOCK_CODE_MAP`（yfinance 用）
4. `python scripts/run_ingestion.py`

### 8.2 換 LLM 模型版本

只動 `src/core/llm_client.py:BACKEND_MODELS`：

```python
"openai": {
    "dev":  "gpt-5.2-mini",
    "demo": "gpt-5.2",       # ← 改這裡換主力模型
},
```

> ⚠️ Gemini 模型 ID 必須有 `-preview` 後綴（API 規定），否則回 404。

### 8.3 新增 LLM 後端

1. `BACKEND_MODELS` 加一條
2. `_KEY_ENV` 加環境變數對應
3. `_AUTO_DETECT_ORDER` 決定優先級
4. `_dispatch()` 加一個 `elif backend == "xxx":` 分支

### 8.4 GCP Cloud Run 部署

參見 `docs/DEPLOY_GCP.md`。向量存取走 BigQuery（Serverless），無需本機或雲端 Docker 容器。

---

## 9. 容錯設計（壞掉時往哪查）

| 症狀 | 第一個檢查的地方 | 為什麼 |
|---|---|---|
| 報告全 40% confidence | terminal 看「偵測失敗：XXX」 → 一定是 LLM 配額/key 問題 | `_unwrap()` 會印出真實錯誤 |
| `RetryError` 出現 | 該被 v1.1 修掉了，回報 bug | 應該已經 unwrap 成底層錯 |
| `decide_tools LLM 失敗` warning | 自動降級為關鍵字匹配，不影響運作 | 是設計成 graceful degradation |
| 子查詢出現未選的年份 | `query_decomposer` 的 `scope_str` 是否正確帶入 | v1.1 已修，prompt 明確列季度 |
| BigQuery 查詢報 403 | GCP ADC 未設定 或 Service Account 缺權限 | `gcloud auth application-default login`；Cloud Run 補 bigquery.dataEditor / aiplatform.user |
| 多公司比較跑很慢 | `comparison.py` 的 `max_workers=2` | 故意限制，避免 rate limit |

---

## 10. 安全 / 合規重點

| 項目 | 怎麼做的 |
|---|---|
| **不給投資建議** | UI sidebar + footer 都明寫；報告 prompt 也禁止 LLM 給選股建議 |
| **XSS** | 所有 LLM 輸出進 HTML 前 `html.escape()`，標 `# [f]` |
| **Prompt Injection** | 使用者輸入截斷 500 字 + strip HTML tag |
| **Rate Limit** | UI 兩次查詢間 10 秒冷卻 |
| **白名單驗證** | `company` 必須在 `COMPANIES` 列表內，不接受任意字串 |
| **API Key 不外洩** | `.env` gitignored、錯誤訊息只顯示 type + 前 120 字訊息 |

---

## 11. 不存在的東西（避免你白找）

- ❌ **沒有 user 系統**：所有人共用一個 instance，部署時靠 `APP_PASSWORD` 擋。
- ❌ **沒有 Docker Compose**：向量資料庫已 Serverless 化（BigQuery），不需要 Qdrant 容器。
- ✅ **自動化測試**：`tests/` 有 437 個 pytest，`pytest tests/ -v` 離線即可跑。
- ✅ **CI/CD**：GitHub Actions 有 lint + pytest + Streamlit smoke test。

---

## 12. 下一步建議路線圖

短期（1-2 週）：
- 補 pytest 涵蓋 `core/contradiction.py` 的 `_extract_json`、`batch_detect`、`detect_promises`
- 加 GitHub Actions 跑 `python -m py_compile` + pytest
- 把 `app.py` 的 session_state 拆成獨立 dataclass（檔案太長）

中期（1-2 月）：
- 改用 LangGraph Persistence（SqliteSaver）保存 Agent 中間狀態，可以斷線續跑
- 加 user-level rate limit（目前是 process-level）
- Multi-tenant：以公司分群儲存，避免不同 user 看同一份結果

長期：
- 從「事後審計」進化為「即時警報」：法說會結束 24h 內主動 push 矛盾摘要
- 接 SEC EDGAR API，擴展到美股全市場
- 訓練 fine-tune 模型替代部分 LLM 呼叫，降低成本

---

## 13. 對接資源

- **CLAUDE.md** — 給 Claude Code（AI coding agent）看的專案規範與安全標籤約定
- **docs/system_architecture.md** — v1.0 架構文件（更詳細的圖表）
- **README.md** — 公開版使用教學
- **.env.example** — 環境變數逐項說明

有問題先翻 CLAUDE.md 的「程式規範對應」（a-f 六項），那是這份 codebase 的設計憲法。

---

> _Last updated: 2026-05-25_
_Maintainer handover note: v1.1 hardening patch 已驗證 4-tier LLM cascade 在配額用盡 / 模型錯名 / 信用不足三種情況下都能自動降級。請繼續維持「不靜默降級」原則：所有 fallback 必須 print 警告，方便 debug。_
