# 🕵️ EarningsWatch

> **法說會 RAG Agent 一致性審計平台**
> 追蹤管理層跨季發言 · 找矛盾 · 追承諾 · 抓話術

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.56-red)](https://streamlit.io/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.1-green)](https://langchain-ai.github.io/langgraph/)
[![Qdrant](https://img.shields.io/badge/Qdrant-1.17-purple)](https://qdrant.tech/)

---

## 📌 專案簡介

EarningsWatch 透過 **7 節點 LangGraph RAG Agent** 自動分析上市公司法說會逐字稿，解決傳統閱讀時難以跨季比對的痛點：

- **矛盾偵測**：找出管理層前後說法不一致之處
- **承諾追蹤**：記錄前瞻指引，下季自動驗收是否兌現
- **立場趨勢圖**：視覺化呈現逐季態度變化（更樂觀 / 維持不變 / 更保守）
- **多公司比較**：並行分析最多 3 家公司，生成跨公司對照表
- **Self-Reflection**：Agent 自動評估信心度，不足則重查（最多 3 輪）

---

## 🏗 系統架構

```
PDF 法說會逐字稿
  → smart_parser（pdfplumber）
  → chunker（QA-pair / sliding-window）
  → embedder（paraphrase-multilingual-mpnet-base-v2, 768維）
  → Qdrant 向量資料庫

使用者查詢（Streamlit UI）
  → LangGraph 7 節點 Agent
       ① 分析意圖  ② 分解問題  ③ 選擇工具
       ④ 並行檢索（Qdrant + Cohere Rerank + Coverage Sweep）
       ⑤ 矛盾偵測（LLM 語意比對）
       ⑥ Self-Reflection（信心度 < 0.75 → 重查）
       ⑦ 生成報告
  → 矛盾卡片 / 承諾追蹤 / 趨勢圖 / CSV / PDF 匯出
```

---

## 🚀 快速開始

### 環境需求

- Python 3.10+
- Docker（本地 Qdrant）
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
| `OPENAI_API_KEY` | 擇一 | GPT-5o / GPT-5o-mini ★ 主力 |
| `GEMINI_API_KEY` | 擇一 | Gemini 3.0 Flash（免費額度大）|
| `COHERE_API_KEY` | 擇一 | Command R+（同時用於 Rerank）|
| `TAVILY_API_KEY` | 選填 | 即時新聞搜尋 |
| `QDRANT_URL` | 選填 | Qdrant Cloud URL（不填用本地 Docker）|
| `QDRANT_API_KEY` | 選填 | Qdrant Cloud Key |
| `APP_PASSWORD` | 選填 | 對外部署時設定存取密碼 |
| `LLM_BACKEND` | 選填 | 強制指定後端（openai / gemini / cohere）|

> 🔄 **自動降級：** 主後端配額用完 / 觸發 429 / 模型下線 / 503 時，會印出友善訊息並自動切到下一個後端（順序：openai → gemini → cohere）。
> ⛔ **已移除：** anthropic、groq（無 API Key）。

---

## ☁️ Streamlit Cloud 部署

1. Fork 此 repo
2. 在 Streamlit Cloud 連結 GitHub repo
3. 在 **Secrets** 頁面填入環境變數（取代 `.env`）：

```toml
# .streamlit/secrets.toml（勿上傳至 git）
GEMINI_API_KEY = "你的金鑰"
QDRANT_URL = "https://xxx.qdrant.io"
QDRANT_API_KEY = "xxx"
APP_PASSWORD = "設定存取密碼"
```

> ⚠️ 使用 Qdrant Cloud 時，請在 [qdrant.tech](https://qdrant.tech) 建立免費 cluster 並先執行 `python scripts/migrate_to_cloud.py` 遷移資料。

---

## 📁 專案結構

```
EarningsWatch/
├── src/
│   ├── agent/          # LangGraph Agent（graph.py, nodes.py, state.py, tools.py）
│   ├── core/           # 核心邏輯（llm_client, qdrant_client, retriever, contradiction, comparison）
│   ├── ingestion/      # PDF 匯入流水線（smart_parser, chunker, embedder）
│   └── ui/             # Streamlit UI（app.py, chart.py, export.py）
├── scripts/
│   ├── run_ingestion.py        # PDF 匯入腳本
│   └── migrate_to_cloud.py     # 遷移至 Qdrant Cloud
├── tests/
│   └── benchmark.py            # 30 題量化 Benchmark
├── docs/
│   └── system_architecture.md  # 系統架構文件
├── .streamlit/
│   └── config.toml             # Streamlit 安全設定
├── .env.example                # 環境變數範本
├── requirements.txt
└── start.sh                    # 一鍵啟動腳本
```

---

## 🛡 安全設計

| 威脅 | 防禦措施 |
|---|---|
| XSS | 所有 LLM 輸出插入 HTML 前 `html.escape()` |
| Prompt Injection | 輸入截斷 500 字 + 移除 HTML 標籤 |
| API Key 洩漏 | 錯誤訊息只顯示 exception type，完整錯誤僅寫 server log |
| Qdrant 暴露 | Docker 只綁定 `127.0.0.1:6333` |
| API 濫用 | Rate Limiting 10秒冷卻 + 白名單驗證 |
| 未授權存取 | `APP_PASSWORD` 環境變數控制密碼保護 |

---

## 🤖 技術棧

| 分類 | 技術 | 版本 |
|---|---|---|
| Agent 框架 | LangGraph | 1.1.9 |
| UI | Streamlit | 1.56.0 |
| 向量資料庫 | Qdrant | 1.17.1 |
| Embedding | sentence-transformers | 5.4.1 |
| Rerank | Cohere rerank-multilingual-v3.0 | — |
| PDF 解析 | pdfplumber | 0.11.9 |
| 圖表 | Plotly | 6.7.0 |
| PDF 匯出 | fpdf2 | 2.8.7 |

---

## ⚠️ 免責聲明

EarningsWatch 為文件分析工具，**不提供投資建議或股價預測**。  
資料來源：公開資訊觀測站（MOPS）法說會逐字稿。  
分析結果僅供參考，投資決策請自行判斷。
