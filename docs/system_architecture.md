# EarningsWatch — 系統架構文件

> 版本：v1.0　　最後更新：2026-05-01

---

## 1. 專案概述

**EarningsWatch** 是一個法說會（Earnings Call）Agentic RAG 一致性審計平台。  
系統自動讀取上市公司法說會逐字稿 PDF，透過 7 節點 LangGraph Agent 進行跨季語意比對，偵測管理層發言矛盾、追蹤承諾兌現情況，並產生結構化偵查報告。

**核心特點**
- Agentic RAG：LangGraph 7 節點 StateGraph + Self-Reflection 自動重查迴圈
- 多模型支援：5 種 LLM 後端，單一環境變數切換
- 完全本地 Embedding：無需外部 API 即可建立向量索引
- Streamlit 互動 UI：零技術門檻操作

---

## 2. 執行環境

| 項目 | 版本 |
|---|---|
| **Python** | 3.11+ |
| **作業系統（開發）** | macOS / Linux |
| **向量資料庫** | Google BigQuery（Serverless） |

---

## 3. 整體架構圖

```
┌─────────────────────────────────────────────────────────────────┐
│                        Streamlit UI (Port 8501)                 │
│   app.py · chart.py · export.py                                 │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 呼叫
┌──────────────────────────────▼──────────────────────────────────┐
│                     LangGraph Agent Layer                       │
│                                                                 │
│   graph.py ──→ [classify] → [decompose] → [route] → [retrieve] │
│                          ↗ (retry ≤ 3)                          │
│              [report] ← [reflect] ← [detect]                   │
│                                                                 │
│   nodes.py（節點實作）  state.py（AgentState TypedDict）        │
│   tools.py（Tavily / yfinance / 工具路由）                      │
└──────┬─────────────────────┬──────────────────────────────────--┘
       │ 向量搜尋              │ LLM 呼叫
┌──────▼──────┐        ┌──────▼──────────────────────────────────┐
│  BigQuery   │        │           LLM Backend Layer              │
│ VECTOR_     │        │  llm_client.py                          │
│ SEARCH      │        │  Gemini · OpenAI · Anthropic · Cohere   │
│ (Serverless)│        │ （單一 chat() 入口，自動偵測後端）       │
└──────┬──────┘        └─────────────────────────────────────────┘
       │ 索引建立
┌──────▼──────────────────────────────────────────────────────────┐
│                      Ingestion Pipeline                         │
│   smart_parser.py（pdfplumber / LlamaParse）                    │
│   chunker.py（QA-pair / sliding-window / whole-page）           │
│   embedder.py（gemini-embedding-2，Vertex AI，768 維）          │
└──────┬──────────────────────────────────────────────────────────┘
       │ 讀取
┌──────▼──────────────────────────────────────────────────────────┐
│                     data/raw_pdfs/                              │
│   法說會 PDF 逐字稿（MOPS 格式 / 英文 Transcript 格式）         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. 資料流

```
PDF（data/raw_pdfs/）
  ↓  smart_parser.py    → pdfplumber 解析文字；表格頁自動偵測，
  │                        LlamaParse 可選補救（LLAMA_CLOUD_API_KEY）
  ↓  chunker.py         → 三種切分策略：
  │                        · QA-pair（問答對切分）
  │                        · sliding-window（256 tokens，重疊 64）
  │                        · whole-page（表格頁整頁保留）
  ↓  embedder.py        → gemini-embedding-2（768 維，MRL 截斷）
  │                        Vertex AI API，BATCH_SIZE=1（逐筆）
  ↓  BigQuery           → Table: "earnings_data.earnings_calls"
                           VECTOR_SEARCH，Cosine Distance；
                           Columns: company, quarter, section, content, page, embedding

查詢（Streamlit）
  ↓  classify node       → 萃取 company / topic / quarters
  ↓  decompose node      → 拆解 3 條子查詢（含季度 / 細節 / 對比面向）
  ↓  route node          → decide_tools()：判斷是否需要 tavily / yfinance
  ↓  retrieve node       → 並行：BigQuery VECTOR_SEARCH top-k + Coverage Sweep
  │                        + Cohere Rerank（rerank-multilingual-v3.0）
  ↓  detect node         → batch_detect()：LLM 逐季對比較 → JSON 結構
  │                        detect_promises()：前瞻承諾偵測
  ↓  reflect node        → confidence < 0.75 且 iteration < 3 → 重回 retrieve
  ↓  report node         → Markdown 偵查報告
  ↓  Streamlit 顯示      → 矛盾卡片 / 承諾追蹤 / 趨勢圖 / CSV / PDF 匯出
```

---

## 5. 技術棧與版本

### 5.1 核心框架

| 技術 | 版本 | 用途 |
|---|---|---|
| **LangGraph** | 1.1.9 | 7 節點 StateGraph，Self-Reflection 條件邊 |
| **LangChain Core** | 1.3.2 | LangGraph 底層依賴 |
| **Streamlit** | 1.56.0 | Web UI，含 `@st.fragment` 局部重渲染 |

### 5.2 LLM 後端（多後端並列支援）

| 後端 | SDK | 使用模型（Demo / Dev） | 備註 |
|---|---|---|---|
| **Gemini** ★ | google-genai ≥ 1.0 | gemini-2.5-flash | 免費額度大，auto-detect 首選 |
| **OpenAI** | openai ≥ 1.50 | gpt-5 / gpt-5-mini | 付費，2026-05-08 校正模型名 |
| **Anthropic** | anthropic ≥ 0.25 | claude-sonnet-4-6 / claude-haiku-4-5 | 2026-05 回歸（top-up 後）|
| **Cohere** | cohere ≥ 5.0 | command-r-plus-08-2024 | 同時用於 Rerank |

> ⛔ 已移除：groq（不在支援清單）。  
> 切換方式：設定環境變數 `LLM_BACKEND=gemini`（或 openai / anthropic / cohere）。  
> 未設定時自動偵測順序：`gemini → openai → anthropic → cohere`  
> 🔄 任一後端 quota / 429 rate limit / 401-403 / 404 / 503 → 印出友善中文訊息並切換下一個。網路 / timeout 錯誤同後端重試 1 次後再切換。

### 5.3 向量資料庫

| 技術 | 版本 | 用途 |
|---|---|---|
| **BigQuery** | Serverless | 向量儲存 + VECTOR_SEARCH，Cosine Distance |
| **google-cloud-bigquery** | ≥ 3.0 | Python SDK |

- Table 路徑：`{GOOGLE_CLOUD_PROJECT}.earnings_data.earnings_calls`
- 向量維度：768（對應 gemini-embedding-2 MRL 截斷）
- 欄位：`company`, `quarter`, `section`, `content`, `page`, `embedding`

### 5.4 Embedding 與 Rerank

| 技術 | 版本 | 用途 |
|---|---|---|
| **google-genai** | ≥ 1.0 | Gemini Embedding API（gemini-embedding-2） |
| **google-cloud-aiplatform** | ≥ 1.0 | Vertex AI 底層（ADC 認證）|
| **Embedding 模型** | gemini-embedding-2 | 768 維（MRL 截斷），支援中英文多語言 |
| **Cohere Rerank** | API（cohere 5.21.1） | rerank-multilingual-v3.0，二階段精排 |

### 5.5 PDF 解析

| 技術 | 版本 | 用途 |
|---|---|---|
| **pdfplumber** | 0.11.9 | 主要 PDF 文字與表格解析 |
| **pandas** | 3.0.2 | 表格 DataFrame 轉自然語言 |
| **LlamaParse**（選用）| llama-parse | 表格 PDF 補救，免費 1000頁/天 |

### 5.6 外部工具（Agent 工具層）

| 技術 | 版本 | 用途 |
|---|---|---|
| **tavily-python** | 0.7.23 | 即時新聞搜尋（專為 LLM Agent 設計）|
| **yfinance** | 1.3.0 | 股價資料（Yahoo Finance）|

### 5.7 UI 輔助

| 技術 | 版本 | 用途 |
|---|---|---|
| **plotly** | 6.7.0 | 立場趨勢互動圖表 |
| **fpdf2** | 2.8.7 | PDF 報告匯出（STHeiti 中文字型）|

### 5.8 通用工具

| 技術 | 版本 | 用途 |
|---|---|---|
| **python-dotenv** | 1.2.2 | `.env` 環境變數載入 |
| **tenacity** | 9.1.4 | LLM 呼叫自動重試（指數退避）|
| **tqdm** | 4.67.3 | Embedding 批次進度條 |
| **typing-extensions** | 4.15.0 | TypedDict 等型別擴充 |

---

## 6. 原始碼結構

```
EarningsWatch/
├── src/
│   ├── agent/                     # LangGraph Agent 層
│   │   ├── graph.py               # StateGraph 定義、節點連接、條件邊
│   │   ├── nodes.py               # 7 個節點函式實作
│   │   ├── state.py               # AgentState TypedDict 型別定義
│   │   └── tools.py               # Tavily 新聞、yfinance 股價、工具路由
│   ├── core/                      # 核心業務邏輯層
│   │   ├── llm_client.py          # 統一 LLM 呼叫介面（4 後端）
│   │   ├── bq_client.py           # BigQuery singleton client（lru_cache）
│   │   ├── retriever.py           # 向量搜尋 + Coverage Sweep + Rerank
│   │   ├── contradiction.py       # LLM 矛盾偵測、承諾追蹤
│   │   ├── comparison.py          # 多公司並行分析、比較表、差異摘要
│   │   ├── secrets.py             # GCP Secret Manager / .env 橋接
│   │   ├── telemetry.py           # Token / cost / latency 統計
│   │   └── rate_limiter.py        # IP-based rate limiting
│   ├── ingestion/                 # 資料匯入流水線
│   │   ├── smart_parser.py        # PDF 解析（pdfplumber / LlamaParse）
│   │   ├── chunker.py             # 文本切分（3 種策略）
│   │   └── embedder.py            # Embedding（gemini-embedding-2）+ BigQuery 寫入
│   └── ui/                        # Streamlit UI 層
│       ├── app.py                 # 薄殼主介面（page config / sidebar / dispatch）
│       ├── views/
│       │   ├── single.py          # 單公司結果渲染
│       │   └── multi.py           # 多公司比較結果渲染
│       ├── state.py               # UIState dataclass（session state 集中管理）
│       ├── quarters.py            # BigQuery SELECT DISTINCT 動態季度列表
│       ├── chart.py               # Plotly 趨勢圖
│       ├── export.py              # CSV / PDF 匯出
│       ├── cache.py               # Demo 快取讀寫
│       ├── auth.py                # APP_PASSWORD 驗證
│       └── styles.py              # CUSTOM_CSS
├── scripts/
│   ├── run_ingestion.py           # PDF 匯入執行腳本
│   ├── setup_gcp_secrets.sh       # 一鍵建立 Secret Manager 條目
│   └── rotate_secret.sh           # 輪換 / 初次寫入 secret
├── data/
│   ├── raw_pdfs/                  # 原始 PDF（不提交至 git）
│   └── processed/
│       └── ingestion_log.json     # 已處理 PDF 記錄（防重複匯入）
├── cache/
│   └── demo_cache.json            # Demo 快取（Agent 失敗時保底）
├── docs/
│   └── system_architecture.md    # 本文件
├── requirements.txt
├── CLAUDE.md
├── .env                           # API Keys（不提交 git）
└── start.sh                       # 一鍵啟動腳本
```

---

## 7. LangGraph Agent 節點說明

| # | 節點 key | 函式 | 輸入 → 輸出 |
|---|---|---|---|
| 1 | `classify` | `intent_classifier` | query → company / topic / quarters |
| 2 | `decompose` | `query_decomposer` | query + topic → sub_queries（3 條）|
| 3 | `route` | `dynamic_tool_router` | query + topic → tool_plan（bigquery / tavily / yfinance）|
| 4 | `retrieve` | `parallel_retrieval` | sub_queries → retrieved（{quarter: [chunks]}）+ news + stock |
| 5 | `detect` | `contradiction_detect` | retrieved + topic → contradictions + promises |
| 6 | `reflect` | `self_reflect` | contradictions → confidence；< 0.75 → retry ↩④，≥ 0.75 → continue |
| 7 | `report` | `report_generator` | contradictions + promises + retrieved → final_report（Markdown）|

**Self-Reflection 條件邊**：`reflect` 節點判斷 `confidence < 0.75 AND iteration < 3`，滿足條件則回到 `retrieve` 節點重查（最多 3 輪）。

---

## 8. 向量搜尋策略

### 8.1 兩階段檢索

```
子查詢 × 3
  ↓
BigQuery VECTOR_SEARCH（top-k=20，Cosine Distance）
  ↓
Coverage Sweep（補充初次未覆蓋的季度，min_score ≥ 0.25）
  ↓
Cohere Rerank（rerank-multilingual-v3.0，最終保留 top-5）
```

### 8.2 Coverage Sweep

- 使用 `get_company_quarters()` 執行 `SELECT DISTINCT quarter FROM BigQuery` 取得該公司所有季度
- 對初次檢索未覆蓋的季度補充一輪搜尋
- `min_score=0.25` 門檻：相似度不足的季度直接跳過，避免引入雜訊

---

## 9. 安全機制

| 威脅 | 防禦措施 | 位置 |
|---|---|---|
| XSS | 所有 LLM 輸出插入 HTML 前 `html.escape()` | `app.py` `_sanitize_str()` |
| Prompt Injection | 使用者輸入截斷 500 字 + 移除 HTML 標籤 | `app.py` 輸入驗證區塊 |
| API 濫用 / DoS | 查詢冷卻時間 10 秒 | `app.py` `_COOLDOWN_SEC=10` |
| 非法參數注入 | 公司 / 主題白名單驗證 | `app.py` `COMPANIES` / `TOPICS` 列表 |
| Token 爆炸 | LLM prompt 每季內容上限 2000 字 | `contradiction.py` `_MAX_CONTENT=2000` |
| API Key 洩漏 | 錯誤訊息只輸出 `type(e).__name__`，不含 `str(e)` | `comparison.py` |

---

## 10. 部署模式

### 本地開發

```bash
# 確保已設定 GCP ADC（Application Default Credentials）
gcloud auth application-default login

# 啟動 Streamlit
streamlit run src/ui/app.py --server.port 8501
```

### GCP Cloud Run 部署

參見 `docs/DEPLOY_GCP.md`。向量存取走 BigQuery（Serverless），無需本機容器。

---

## 11. 環境變數一覽

| 變數 | 必要性 | 說明 |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | 必要 | GCP 專案 ID（BigQuery + Vertex AI）|
| `GEMINI_API_KEY` | 至少一個 LLM key ★ | Gemini 2.5 Flash（免費額度大，auto-detect 首選）|
| `OPENAI_API_KEY` | 至少一個 LLM key | GPT-5 / GPT-5-mini |
| `ANTHROPIC_API_KEY` | 至少一個 LLM key | Claude Sonnet 4.6 / Haiku 4.5（付費）|
| `COHERE_API_KEY` | 至少一個 LLM key | Command R+（同時用於 Rerank）|
| `LLM_BACKEND` | 選用 | 強制指定後端（gemini / openai / anthropic / cohere）|
| `GCP_SECRET_PROJECT` | 選用 | GCP 專案 ID，啟用 Secret Manager（不設則用 .env）|
| `LLAMA_CLOUD_API_KEY` | 選用 | LlamaParse 表格補救（免費 1000頁/天）|
| `TAVILY_API_KEY` | 選用 | 即時新聞搜尋 |
| `COVERAGE_MIN_SCORE` | 選用 | coverage sweep 相似度門檻（預設 0.25）|
| `LLM_HYDE_ENABLED` | 選用 | 啟用 HyDE 查詢擴展（預設 false）|
| `LLM_BUDGET_USD` | 選用 | 每次查詢 LLM 預算上限（預設 0.50 USD）|
