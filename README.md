# 🕵️ EarningsWatch

> **法說會 RAG Agent 一致性審計平台**
> 追蹤管理層跨季發言 · 找矛盾 · 追承諾 · 抓話術

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.56-red)](https://streamlit.io/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.1-green)](https://langchain-ai.github.io/langgraph/)
[![BigQuery](https://img.shields.io/badge/BigQuery-Vector%20Search-blue)](https://cloud.google.com/bigquery)
[![Gemini Embedding](https://img.shields.io/badge/Gemini-embedding--2-orange)](https://ai.google.dev/gemini-api/docs/embeddings)
[![OpenSpec](https://img.shields.io/badge/Spec-OpenSpec-purple)](https://github.com/Fission-AI/OpenSpec)

---

## 📌 專案簡介

EarningsWatch 透過 **7 節點 LangGraph RAG Agent** 自動分析上市公司法說會逐字稿，解決傳統閱讀時難以跨季比對的痛點：

- **矛盾偵測**：找出管理層前後說法不一致之處
- **承諾追蹤**：記錄前瞻指引，下季自動驗收是否兌現
- **立場趨勢圖**：視覺化呈現逐季態度變化（更樂觀 / 維持不變 / 更保守）
- **多公司比較**：並行分析最多 3 家公司，生成跨公司對照表
- **Self-Reflection**：Agent 自動評估信心度，不足則重查（最多 3 輪）
- **🎤 語音輸入**：自訂查詢欄位支援瀏覽器原生 Web Speech API
- **☁ Serverless 後端**：向量庫遷移至 BigQuery Vector Search + Gemini Embedding，免維護 Docker / Qdrant

---

## 🆕 最近更新（2026-05-08 起）

| 日期 | 主題 |
|---|---|
| 2026-05-19 | 採用 [OpenSpec](https://github.com/Fission-AI/OpenSpec) 管理規格變更；`COVERAGE_MIN_SCORE` 環境變數可調 coverage sweep 門檻（#26）|
| 2026-05-17 | Embedder 升級為 `gemini-embedding-2`（MRL 截斷 768 維），新增 429 retry + RPM throttle（#24, #25）|
| 2026-05-16 | BigQuery `VECTOR_SEARCH` filter pushdown，避免 BadRequest；UI 季度下拉依公司動態過濾 |
| 2026-05-15 | Tavily 新聞改為依公司名稱過濾並依時間排序；UI 跳脫 `$` 避免 LaTeX 渲染 |
| 2026-05-14 | Gemini 主後端切換至 `gemini-3.1-flash-lite` (preview)，免費額度更大 |
| 2026-05-13 | 前瞻型查詢自動路由到 Tavily 即時新聞；自訂查詢欄位支援語音輸入 |
| 2026-05-13 | GCP Secret Manager 整合 + `rotate_secret.sh` / `setup_gcp_secrets.sh` 助手腳本 |
| 2026-05-13 | Anthropic backend 回歸（Claude Sonnet 4.6 / Haiku 4.5） |
| 2026-05-13 | 多層 API key 洩漏防護（gitleaks + pre-commit + 錯誤訊息脫敏） |
| 2026-05-12 | UI 改套 Google Material Design；loading spinner 改為 infinity SVG |
| 2026-05-10 | GCP Cloud Run 部署設定 + demo cache 保底機制 |
| 2026-05-09 | `app.py` 拆分為 `views/single.py` + `views/multi.py`；UIState dataclass 重構 |
| 2026-05-09 | 主題改為可選（自動推導）；CI 新增 Streamlit smoke test |
| 2026-05-08 | LLM 模型名稱校正：`gpt-5` / `gemini-2.5-flash` |

詳細差異請見 [docs/CHANGES_SINCE_2026-05-08.md](docs/CHANGES_SINCE_2026-05-08.md)。

---

## 🏗 系統架構

```
PDF 法說會逐字稿
  → smart_parser（pdfplumber）
  → chunker（QA-pair / sliding-window）
  → embedder（Gemini `gemini-embedding-2`，MRL 截斷至 768 維；429 retry + RPM throttle）
  → BigQuery Vector Search（Serverless，免維護向量庫）

使用者查詢（Streamlit UI）
  → LangGraph 7 節點 Agent
       ① 分析意圖  ② 分解問題  ③ 選擇工具
       ④ 並行檢索（BigQuery VECTOR_SEARCH + Cohere Rerank + Coverage Sweep）
       ⑤ 矛盾偵測（LLM 語意比對）
       ⑥ Self-Reflection（信心度 < 0.75 → 重查）
       ⑦ 生成報告
  → 矛盾卡片 / 承諾追蹤 / 趨勢圖 / CSV / PDF 匯出
```

---

## 🚀 快速開始

### 環境需求

- Python 3.10+
- GCP 專案（已啟用 BigQuery + Vertex AI；本機開發用 `gcloud auth application-default login`）
- 至少一個 LLM API Key（推薦 Gemini，免費）

### 安裝

```bash
git clone https://github.com/YOUR_USERNAME/EarningsWatch.git
cd EarningsWatch

# 建立虛擬環境
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 安裝套件
pip install -r requirements.txt
```

### 設定環境變數

```bash
cp .env.example .env
# 用文字編輯器填入 API Keys
```

最少只需填入一個 LLM Key：

```env
GEMINI_API_KEY=你的金鑰     # Google AI Studio 免費取得
```

### 匯入法說會 PDF

```bash
# 將 PDF 放入 data/raw_pdfs/
# 支援格式：MOPS格式（233020230112M001.pdf）或英文逐字稿（TSMC 2Q24 Transcript.pdf）

python scripts/run_ingestion.py           # 匯入全部
python scripts/run_ingestion.py --dry-run # 預覽（不執行）
python scripts/run_ingestion.py --pdf TSMC\ 2Q24\ Transcript.pdf  # 單一檔案
```

### 啟動

```bash
./start.sh
# 開啟瀏覽器 → http://localhost:8501
```

---

## 🔑 環境變數說明

| 變數 | 必要 | 說明 |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | 必填 | GCP 專案 ID（BigQuery + Vertex AI 都在這個專案下）|
| `OPENAI_API_KEY` | 擇一 | GPT-5 / GPT-5-mini ★ 主力 |
| `GEMINI_API_KEY` | 擇一 | Gemini 3.1 Flash Lite (preview)（免費額度大）+ `gemini-embedding-2`（ingestion / query embedding）|
| `ANTHROPIC_API_KEY` | 擇一 | Claude Sonnet 4.6 / Haiku 4.5（付費；topup 後啟用）|
| `COHERE_API_KEY` | 擇一 | Command R+（同時用於 Rerank）|
| `TAVILY_API_KEY` | 選填 | 即時新聞搜尋 |
| `APP_PASSWORD` | 選填 | 對外部署時設定存取密碼 |
| `LLM_BACKEND` | 選填 | 強制指定後端（openai / gemini / anthropic / cohere）|
| `GCP_SECRET_PROJECT` | 選填 | 啟用 Secret Manager；填了之後所有金鑰會從 GCP Secret Manager 讀取，env var 為 fallback |
| `LANGSMITH_API_KEY` | 選填 | LangSmith tracing（用 Secret Manager 時會自動橋接到 env）|
| `COVERAGE_MIN_SCORE` | 選填 | Coverage sweep 餘弦相似度門檻（預設 `0.25`；非 float / 不在 [0,1] 範圍會降回預設並印出警告）|

> 🔄 **自動降級：** 主後端配額用完 / 觸發 429 / 模型下線 / 503 時，會印出友善訊息並自動切到下一個後端（順序：openai → gemini → anthropic → cohere）。
> 🔐 **密鑰管理：** 部署到 GCP 時推薦設 `GCP_SECRET_PROJECT`，金鑰透過 Secret Manager 統一管控；本機開發仍可用 `.env`。
> ⛔ **已移除：** groq（無 API Key）。

---

## ☁️ 雲端部署

> 📍 **目前線上部署：Streamlit Community Cloud**（前端），後端 BigQuery 資料集 `earningswatch-demo.earnings_data.earnings_calls` 仍在運作。
> 原 GCP Cloud Run 服務已於 2026-06 下線以節省成本；下方 Cloud Run 設定與 [docs/DEPLOY_GCP.md](docs/DEPLOY_GCP.md) 保留作為「選擇性重新部署」的食譜，`Dockerfile` 同理保留。

**現行：Streamlit Cloud**

1. Fork 此 repo
2. 在 Streamlit Cloud 連結 GitHub repo
3. 在 **Secrets** 頁面填入環境變數（取代 `.env`）：

```toml
# .streamlit/secrets.toml（勿上傳至 git）
GOOGLE_CLOUD_PROJECT = "your-gcp-project"
GEMINI_API_KEY = "你的金鑰"
APP_PASSWORD = "設定存取密碼"
```

> ⚠️ Streamlit Cloud 仍需 BigQuery + Vertex AI 認證，請將 service-account JSON 放進 Streamlit secrets 的 `[gcp_service_account]` 區塊（Streamlit Cloud 無 ADC）。此 SA 金鑰即目前唯一常駐的後端憑證，退役時記得一併撤銷。

**選擇性：GCP Cloud Run**（同 GCP 內走 BigQuery + Vertex AI 最順，需重新部署）

```bash
# 詳見 docs/DEPLOY_GCP.md
gcloud run deploy earningswatch --source . --region asia-east1
```

Cloud Run Service Account 需要 `BigQuery Data Editor`、`AI Platform User`、`Secret Manager Secret Accessor` 三個角色。完整流程（含 `setup_gcp_secrets.sh` 一鍵建立 Secret Manager 條目、`rotate_secret.sh` 輪換金鑰）請參考 [docs/DEPLOY_GCP.md](docs/DEPLOY_GCP.md)。

---

## 📁 專案結構

```
EarningsWatch/
├── src/
│   ├── agent/          # LangGraph Agent（graph.py, nodes.py, state.py, tools.py）
│   ├── core/           # 核心邏輯（llm_client, bq_client, retriever, contradiction,
│   │                   #          comparison, secrets, telemetry, rate_limiter, ragas_eval）
│   ├── ingestion/      # PDF 匯入流水線（smart_parser, chunker, embedder）
│   └── ui/             # Streamlit UI（app.py + views/single.py / views/multi.py）
├── scripts/
│   ├── run_ingestion.py        # PDF 匯入腳本（寫入 BigQuery）
│   ├── build_demo_cache.py     # 預跑常見組合產生 demo 保底快取
│   ├── probe_llm_models.py     # 探測各 LLM 後端可用模型清單
│   ├── setup_gcp_secrets.sh    # 一鍵建立 Secret Manager 條目
│   └── rotate_secret.sh        # 輪換 / 初次寫入 Secret Manager 金鑰
├── tests/
│   └── benchmark.py            # 30 題量化 Benchmark
├── docs/
│   ├── DEPLOY_GCP.md           # GCP Cloud Run 部署流程（選擇性；目前未啟用）
│   ├── PROJECT_OVERVIEW.md     # 30 分鐘 onboarding
│   ├── ROADMAP.md              # 技術債 / 待辦
│   └── system_architecture.md  # 系統架構
├── .streamlit/
│   └── config.toml             # Streamlit 安全設定
├── .env.example                # 環境變數範本
├── Dockerfile                  # Cloud Run 用 image（選擇性重新部署時使用）
├── requirements.txt
└── start.sh                    # 本機啟動 Streamlit（資料庫已 Serverless 化）
```

---

## 🛡 安全設計

| 威脅 | 防禦措施 |
|---|---|
| XSS | 所有 LLM 輸出插入 HTML 前 `html.escape()` |
| Prompt Injection | 輸入截斷 500 字 + 移除 HTML 標籤 |
| API Key 洩漏 | 多層防護：`.gitleaks.toml` 偵測 + `.githooks/pre-commit` 阻擋 + 錯誤訊息只顯示 exception type |
| 密鑰管理 | GCP Secret Manager 集中保管，runtime 透過 ADC 拉取；本機開發 fallback `.env` |
| 向量庫存取 | BigQuery IAM 控管（Service Account 最小權限：BigQuery Data Editor + AI Platform User）|
| API 濫用 | Rate Limiting 雙層（session-based 10s + IP-based 10s）+ 白名單驗證 |
| 未授權存取 | `APP_PASSWORD` 環境變數控制密碼保護 |

---

## 🤖 技術棧

| 分類 | 技術 | 版本 |
|---|---|---|
| Agent 框架 | LangGraph | ≥ 0.2 |
| UI | Streamlit | ≥ 1.39 |
| 向量資料庫 | BigQuery `VECTOR_SEARCH`（filter pushdown） | Serverless |
| Embedding | Gemini `gemini-embedding-2`（MRL 截斷 768 維） | google-genai ≥ 1.0 |
| LLM 後端 | OpenAI `gpt-5` / `gpt-5-mini`、Gemini `gemini-3.1-flash-lite`、Anthropic `claude-sonnet-4-6` / `claude-haiku-4-5`、Cohere `command-r-plus` | — |
| Rerank | Cohere `rerank-multilingual-v3.0` | — |
| 規格管理 | [OpenSpec](https://github.com/Fission-AI/OpenSpec) | — |
| 密鑰管理 | GCP Secret Manager | — |
| 新聞檢索 | Tavily Search（公司名稱過濾 + 時間排序） | ≥ 0.5 |
| 股價資料 | yfinance | ≥ 0.2.40 |
| PDF 解析 | pdfplumber | ≥ 0.11 |
| 圖表 | Plotly | ≥ 5.0 |
| PDF 匯出 | fpdf2 | ≥ 2.7 |
| 部署 | GCP Cloud Run | — |

---

## ⚠️ 免責聲明

EarningsWatch 為文件分析工具，**不提供投資建議或股價預測**。  
資料來源：公開資訊觀測站（MOPS）法說會逐字稿。  
分析結果僅供參考，投資決策請自行判斷。
