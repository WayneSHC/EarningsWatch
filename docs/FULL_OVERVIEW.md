# EarningsWatch — 完整專案說明文件

> **版本**：v1.3（2026-05-05）
> **GitHub**：https://github.com/WayneSHC/EarningsWatch
> **對象**：開發者 / PM / 評審委員

---

## 目錄

1. [專案定位](#1-專案定位)
2. [五個關鍵概念](#2-五個關鍵概念)
3. [系統架構](#3-系統架構)
4. [LangGraph Agent 七節點](#4-langgraph-agent-七節點)
5. [核心模組詳解](#5-核心模組詳解)
6. [UI 模組](#6-ui-模組)
7. [PDF 匯入流水線](#7-pdf-匯入流水線)
8. [安全設計](#8-安全設計)
9. [程式品質規範](#9-程式品質規範)
10. [測試與 CI](#10-測試與-ci)
11. [本地端部署](#11-本地端部署)
12. [雲端部署（Streamlit Cloud）](#12-雲端部署streamlit-cloud)
13. [環境變數完整清單](#13-環境變數完整清單)
14. [技術棧](#14-技術棧)
15. [技術債與 Roadmap](#15-技術債與-roadmap)
16. [免責聲明](#16-免責聲明)

---

## 1. 專案定位

### 一句話版本

> **法說會 Agentic RAG 一致性審計平台**：吃進上市公司每季法說會 PDF，自動用 LangGraph 7 節點 Agent 跨季比對管理層說法，找出矛盾、未兌現承諾、立場轉變，並以趨勢圖呈現。

### 解決的痛點

法人 / 分析師需手動閱讀 8 季逐字稿才能比較管理層口風是否一致，耗時且容易遺漏。EarningsWatch 將此 Workflow 自動化為一個 Agent，3 分鐘出報告。

### 支援公司

`COMPANIES = ["台積電", "聯發科", "鴻海", "台達電"]`

### 支援分析主題

`TOPICS = ["AI需求", "毛利率", "產能與擴產", "庫存狀況", "市場展望", "CoWoS"]`

### 核心功能

| 功能 | 說明 |
|---|---|
| **矛盾偵測** | LLM 語意比對 Q-N vs Q-N+1，找出管理層說法不一致 |
| **承諾追蹤** | 記錄前瞻指引，下季自動驗收是否兌現 |
| **立場趨勢圖** | 視覺化呈現逐季態度變化（更樂觀 / 維持不變 / 更保守） |
| **多公司比較** | 並行分析最多 3 家公司，生成跨公司對照表 |
| **Self-Reflection** | Agent 評估信心度，不足則自動重查（最多 3 輪） |
| **Abstain Path** | 3 輪後信心 < 0.40 → 輸出「資料不足」聲明，不硬生成低品質報告 |
| **Evidence Verifier** | 精確 + 模糊雙層驗證 LLM 引文，防幻覺引用 |

---

## 2. 五個關鍵概念

| 關鍵字 | 30 秒解釋 |
|---|---|
| **Agentic RAG** | RAG（檢索增強生成）+ Agent（會自己決定下一步），LangGraph 編排 |
| **LangGraph StateGraph** | 用「節點 + 條件邊」描述 Agent 工作流，支援 if / loop |
| **Self-Reflection** | Agent 評估答案夠不夠好，不夠就跳回 retrieve 重查（最多 3 輪）|
| **Coverage Sweep** | 第一輪檢索若漏掉某些季度，第二輪用 facet API 補回 |
| **Contradiction Detection** | LLM 比對 Q-N 與 Q-N+1，回傳 structured JSON：`stance_change`、`evidence`、`confidence` |

---

## 3. 系統架構

### 資料流

```
data/raw_pdfs/
  → smart_parser.py   # pdfplumber（主）；LlamaParse 備援（表格密集型 PDF）
  → chunker.py        # QA-pair / sliding-window / 整頁（表格）三種切分
  → embedder.py       # paraphrase-multilingual-mpnet-base-v2（768 維，本地）
  → Qdrant            # collection "earnings_calls"，cosine 距離
                      # payload keys: company, quarter, section, content, page

使用者查詢（Streamlit UI）
  → LangGraph 7 節點 Agent
       ① classify  — 萃取意圖（公司 / 主題 / 季度）
       ② decompose — 拆成每季 sub-query
       ③ route     — 選工具（RAG / Tavily / yfinance）
       ④ retrieve  — 向量 + BM25 + Cohere Rerank + Coverage Sweep
       ⑤ detect    — LLM 矛盾比對 + Evidence Verifier
       ⑥ reflect   — Coverage Matrix 評分；< 0.75 → retry；< 0.40 → abstain
       ⑦ report    — Markdown 報告輸出
  → 矛盾卡片 / 承諾追蹤 / 趨勢圖 / CSV / PDF 匯出
```

### 三層架構

| 層 | 目錄 | 職責 |
|---|---|---|
| **UI 層** | `src/ui/` | Streamlit 頁面、圖表、匯出；不含業務邏輯 |
| **Agent 層** | `src/agent/` | LangGraph 7 節點；orchestration only |
| **Core 層** | `src/core/` | LLM client、Qdrant client、retriever、矛盾偵測；可獨立測試 |

### 目錄結構

```
EarningsWatch/
├── src/
│   ├── agent/          # graph.py, nodes.py, state.py, tools.py
│   ├── core/           # llm_client, qdrant_client, retriever,
│   │                   # contradiction, comparison
│   ├── ingestion/      # smart_parser, chunker, embedder
│   └── ui/             # app.py, chart.py, export.py,
│                       # cache.py, auth.py, styles.py
├── scripts/
│   ├── run_ingestion.py        # PDF 匯入
│   └── migrate_to_cloud.py     # 遷移至 Qdrant Cloud
├── tests/
│   ├── test_contradiction.py
│   ├── test_llm_client.py
│   ├── benchmark.py            # 30 題量化 Benchmark
│   └── conftest.py
├── docs/                       # 本文件所在
├── data/
│   ├── raw_pdfs/               # PDF 放這裡
│   └── processed/
│       └── ingestion_log.json  # 匯入狀態
├── .streamlit/
│   ├── config.toml             # 伺服器安全設定
│   └── secrets.toml.example   # 雲端密鑰範本
├── .github/workflows/ci.yml    # GitHub Actions CI
├── .env.example
├── requirements.txt
├── requirements-dev.txt
└── start.sh                    # 一鍵本地啟動
```

---

## 4. LangGraph Agent 七節點

`src/agent/nodes.py` 實作 ｜ `src/agent/state.py` 型別 ｜ `src/agent/graph.py` 圖結構

| 節點 | 函數 | 行為 |
|---|---|---|
| `classify` | `intent_classifier` | 萃取公司 / 主題 / 季度；UI 已選則直接沿用 |
| `decompose` | `query_decomposer` | 拆成每季獨立 sub-query |
| `route` | `dynamic_tool_router` | 決定工具：RAG / Tavily（新聞）/ yfinance（股價） |
| `retrieve` | `parallel_retrieval` | 向量搜尋 + BM25 + Cohere Rerank；Coverage Sweep 補漏季 |
| `detect` | `contradiction_detect` | `batch_detect()` + `detect_promises()` + Evidence Verifier |
| `reflect` | `self_reflect` | Coverage Matrix 建立；信心 < 0.75 且 iter < 3 → retry；< 0.40 → abstain |
| `report` | `report_generator` | 生成 Markdown；abstain 時輸出「資料不足」 |

**條件邊**：`reflect → retrieve`（retry）或 `reflect → report`（完成）

**`AgentState` 關鍵欄位**：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `query / company / topic / quarters` | str / list | 查詢意圖 |
| `retrieved` | dict | `{quarter: content}` |
| `contradictions` | list | 含 Evidence Verifier 標記 |
| `confidence_score` | float | 信心評分 |
| `iteration` | int | 目前輪次（上限 3）|
| `coverage_matrix` | dict | 每季資料品質矩陣 |
| `abstain` | bool | 是否因資料不足棄權 |
| `steps_log` | `Annotated[list, operator.add]` | 各節點 log 累加 |

---

## 5. 核心模組詳解

### 5.1 LLM Client（`src/core/llm_client.py`）

單一入口：`chat(prompt, max_tokens, mode)`

**後端自動降級**（2026-05 更新）：`openai → gemini → cohere`

**支援模型**：

| 後端 | Demo (主力) | Dev (高頻) |
|---|---|---|
| OpenAI ★ | gpt-5 | gpt-5-mini |
| Gemini | gemini-2.5-flash | gemini-2.5-flash |
| Cohere | command-r-plus-08-2024 | command-r7b-12-2024 |

> ⛔ 已移除：anthropic、groq（無 API Key 不再支援）。

**P2 強化（2026-05-04）**：

| 機制 | 說明 |
|---|---|
| **逐呼叫 Timeout** | `_dispatch_with_timeout()`，預設 45s（`LLM_TIMEOUT_SECONDS` 覆寫），timeout 觸發降級 |
| **同後端重試** | 暫時性錯誤（`_TRANSIENT_MARKERS`）同後端重試 1 次，指數退避 2s；quota / 認證直接切換 |
| **Prompt Injection Guardrail** | `_INJECTION_GUARD` prepend 到所有 `chat()` 呼叫；`LLM_INJECTION_GUARD=false` 可停用（測試用）|
| **友善 Quota 提示** | `_format_quota_message()`：把 `429 / quota / RESOURCE_EXHAUSTED / 401-403 / 404 / 503` 翻成中文，例：`⚠️ OpenAI (GPT-5o) 今日 token / 配額已用完，自動切換下一個後端…`，會出現在 Streamlit logs |
| **Quota Markers** | `429`、`rate limit`、`too many requests` 也納入降級觸發條件（過去只認 `RESOURCE_EXHAUSTED` / `quota`）|

### 5.2 矛盾偵測（`src/core/contradiction.py`）

**`batch_detect(retrieved, topic)`**

相鄰季度對逐一比對（N-1 次，線性成本），回傳：

```json
{
  "stance_change": "更樂觀 / 維持不變 / 更保守 / 無關",
  "has_contradiction": true,
  "evidence_early": "引用早季原文",
  "evidence_later": "引用晚季原文",
  "confidence": 0.85
}
```

每季 LLM 輸入截斷至 `_MAX_CONTENT = 2000` 字，防 token 爆炸。

**`detect_promises(retrieved)`**：獨立 LLM pass，找前瞻指引並驗收兌現。

**P0 Evidence Verifier（2026-05-04）**：`_verify_quote()` 雙層驗證：

1. 精確子串比對（`in`）
2. 模糊滑動視窗（`difflib.SequenceMatcher`，ratio ≥ 0.85）

幻覺引文 → confidence 降 0.2、清空引文、`verification_failed=True`；模糊通過 → `evidence_{key}_fuzzy=True`，報告加 `～`。

**三層 JSON parse fallback**（`_extract_json()`）：直接 parse → markdown fence → greedy `{}` → 降級 confidence=0。

### 5.3 Coverage Sweep（`src/core/retriever.py`）

初次 top-k 後，呼叫 `get_company_quarters()`（Qdrant facet API）取所有季度，對缺漏季度呼叫 `retrieve_coverage()`。`min_score=0.25` gate：相似度不足直接跳過。

**混合檢索**：向量搜尋（cosine, 768 維）+ BM25（jieba 中文分詞）+ Cohere Rerank 精排。

### 5.4 Self-Reflection（`nodes.py:self_reflect`）

**Coverage Matrix（P1）**：為每季建立資料品質矩陣，納入 LLM judge prompt：

```
{quarter}: {chunk_count, max_score, avg_score, source_pages, quote_verified, top_excerpt}
```

**信心邏輯**：

```
score ≥ 0.75 且 LLM judge 同意  →  report
score < 0.75 且 iteration < 3   →  retry（gap sub_queries 依語意選 tavily / qdrant）
3 輪後 score < 0.40             →  abstain=True → 「資料不足」報告
```

### 5.5 Qdrant Client（`src/core/qdrant_client.py`）

```python
COLLECTION_NAME = "earnings_calls"
VECTOR_SIZE = 768  # paraphrase-multilingual-mpnet-base-v2
```

Singleton via `lru_cache`。`QDRANT_URL` 有值 → Qdrant Cloud；否則 `localhost:6333`。

**規範**：所有模組必須透過 `get_qdrant_client()` 取得，禁止直接 `QdrantClient(...)`。

### 5.6 多公司比較（`src/core/comparison.py`）

`ThreadPoolExecutor(max_workers=2)` 並行執行多家公司 Agent，合併生成跨公司對照報告。

---

## 6. UI 模組

| 檔案 | 職責 |
|---|---|
| `app.py` | 主入口；sidebar、查詢表單、Agent 執行、結果路由 |
| `chart.py` | Plotly 趨勢圖：`build_stance_series` + `chart_to_scrollable_html` |
| `export.py` | CSV（utf-8-sig BOM）、PDF（fpdf2 + STHeiti / Noto CJK 降級）匯出 |
| `cache.py` | Demo 快取讀寫（Agent 失敗保底） |
| `auth.py` | `APP_PASSWORD` 密碼保護（timing-safe + 5 次失敗鎖定 5 分鐘） |
| `styles.py` | CSS 注入（配色、字體、卡片樣式） |

### Session State 規範

| Key | 說明 |
|---|---|
| `last_mode` | `"single"` 或 `"multi"` |
| `last_result` / `last_meta` | single 模式結果 |
| `last_multi_results` / `last_multi_companies` / `last_multi_topic` | multi 模式結果 |
| `_single_pdf_bytes` / `_multi_pdf_bytes` | 匯出快取（以 `{mode}_{company}_{topic}` 為 key）|
| `_authenticated` | 密碼驗證狀態 |
| `_last_run_ts` | 上次查詢時間戳（10 秒冷卻）|

**Fragment pattern**：`@st.fragment` 套在趨勢圖 radio，只重繪圖表不觸發全頁 re-run。

---

## 7. PDF 匯入流水線

### 支援的檔名格式

| 格式 | 範例 | 說明 |
|---|---|---|
| **MOPS 格式** | `233020260115M001.pdf` | `{4碼股票代號}{YYYYMMDD}{M\|E}{3碼序號}` |
| **英文逐字稿** | `TSMC 4Q25 Transcript.pdf` | `TSMC {Q}{2碼年} Transcript{後綴}` |
| **台達電 Analyst Meeting** | `1Q25_Analyst Meeting.pdf`（放在 `2308_Delta/` 目錄）| 從父目錄取股票代號；Q1→5月, Q2→8月, Q3→11月, Q4→隔年2月 |

### 常用指令

```bash
python scripts/run_ingestion.py                 # 匯入全部未處理
python scripts/run_ingestion.py --dry-run       # 預覽，不執行
python scripts/run_ingestion.py --force         # 強制重新匯入
python scripts/run_ingestion.py --pdf FILE      # 單一檔案
```

匯入狀態記錄於 `data/processed/ingestion_log.json`。

### Parser / Chunker 策略

| 策略 | 說明 |
|---|---|
| pdfplumber（主）| 純文字 + 表格，免費 |
| LlamaParse（備援）| 表格密集型 PDF，需 `LLAMA_CLOUD_API_KEY`，免費 1000頁/天 |
| QA-pair 切分 | 依 Q: / A: 切分，保留問答上下文 |
| Sliding-window | 一般段落，重疊視窗保留上下文 |
| 整頁（表格）| 整頁作為一個 chunk，保留數字完整性 |

---

## 8. 安全設計

| 威脅 | 防禦措施 | 位置 | 標記 |
|---|---|---|---|
| **XSS** | `html.escape()` 包裹所有 LLM 輸出 | `app.py:_sanitize_str()` | `[f]` |
| **Prompt Injection（外部）** | 輸入截斷 500 字 + 移除 HTML 標籤 | `app.py` run_btn | `[f]` |
| **Prompt Injection（LLM 端）** | `_INJECTION_GUARD` prepend 到每次 LLM 呼叫 | `llm_client.py` | `[f]` |
| **LLM 幻覺引文** | `_verify_quote()` 精確 + 模糊雙層驗證 | `contradiction.py` | `[f]` |
| **API 濫用 / DoS** | 10 秒冷卻（`_COOLDOWN_SEC = 10`） | `app.py` | `[f]` |
| **非法參數注入** | COMPANIES / TOPICS 白名單驗證 | `app.py` | `[f]` |
| **Token 爆炸** | `_MAX_CONTENT = 2000` 截斷每季輸入 | `contradiction.py` | `[f]` |
| **Qdrant 注入** | 公司名稱只能從白名單取得 | `app.py` | `[f]` |
| **未授權存取** | timing-safe 密碼比對 + 失敗鎖定 | `auth.py` | `[f]` |
| **API Key 洩漏** | 錯誤訊息只顯示 exception type | `llm_client.py` | `[f]` |
| **Qdrant 外部暴露** | Docker 只綁定 `127.0.0.1:6333` | `start.sh` | `[f]` |
| **投資建議責任** | UI 明顯標示「不提供選股建議」+ 頁腳免責 | `app.py` sidebar | — |

---

## 9. 程式品質規範

### 容錯防呆

| 位置 | 機制 |
|---|---|
| `contradiction.py:_extract_json()` | 三層 JSON fallback → 降級 confidence=0 |
| `contradiction.py:batch_detect()` | 每組季度對獨立 try/except；空內容直接 skip |
| `nodes.py:parallel_retrieval()` | `as_completed` + try/except；Tavily / yfinance 失敗不影響 RAG |
| `app.py` Agent 執行 | try/except + Demo 快取保底 |
| `app.py:get_available_quarters()` | facet API → scroll → hardcoded 三層 fallback |

### 效率

| 機制 | 說明 |
|---|---|
| `ThreadPoolExecutor(max_workers=8)` | RAG + Tavily + yfinance 並行 |
| `ThreadPoolExecutor(max_workers=2)` | 多公司 Agent 並行 |
| Qdrant Facet API | v1.10+ 單次取所有唯一季度值 |
| `@st.cache_data(ttl=300)` | 季度列表快取 5 分鐘 |
| PDF bytes session 快取 | 結果未變不重新生成 |

### 關鍵設計決策

**為何用 LLM 做矛盾偵測而非規則？** 語氣轉變（「需求強勁」→「庫存調整」）需語境理解，if/else 無法處理。LLM 回傳含 `confidence` 的 structured JSON，讓 Self-Reflection 可量化。

**為何只比相鄰季度？** 10 季全組合 = 45 次 LLM 呼叫；相鄰比對 = 9 次，成本線性且在趨勢偵測上已足夠。

**為何 `agent.stream()` 而非 `invoke()`？** `stream_mode="updates"` 讓每個節點完成後立即推送 state diff，使用者能即時看進度。

**為何 `operator.add` 在 `steps_log`？** LangGraph 預設後節點覆蓋前節點的同名 key；`Annotated[list, operator.add]` 改為累加語意，所有節點 log 都保留。

---

## 10. 測試與 CI

### 本地測試

```bash
pip install -r requirements-dev.txt
pytest tests/ -v          # 全套單元測試
python tests/benchmark.py  # 30 題量化 Benchmark
```

| 檔案 | 行數 | 覆蓋範圍 |
|---|---|---|
| `test_contradiction.py` | 349 | Evidence Verifier、JSON fallback、batch_detect 容錯 |
| `test_llm_client.py` | 245 | timeout、retry、injection guard、後端降級 |
| `conftest.py` | 47 | shared fixtures（mock LLM、mock Qdrant）|

### GitHub Actions CI（`.github/workflows/ci.yml`）

每次 push / PR 到 `main` 自動執行：

```
Python 3.11 on ubuntu-latest
  → pip install requirements.txt + requirements-dev.txt
  → python -m compileall -q src scripts   # 語法檢查
  → pytest tests/ -v --tb=short           # 單元測試
```

> 測試以 monkeypatch mock 所有網路呼叫，不需要真實 API Key。

---

## 11. 本地端部署

### 環境需求

- Python 3.10+
- Docker（Qdrant 用）
- 至少一個 LLM API Key（推薦 `GEMINI_API_KEY`，免費）

### 安裝步驟

```bash
# 1. Clone
git clone https://github.com/WayneSHC/EarningsWatch.git
cd EarningsWatch

# 2. 虛擬環境
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. 套件
pip install -r requirements.txt

# 4. 環境變數
cp .env.example .env
# 用文字編輯器填入至少一個 LLM Key
```

### 匯入法說會 PDF

```bash
# 將 PDF 放入 data/raw_pdfs/
python scripts/run_ingestion.py           # 匯入全部
python scripts/run_ingestion.py --dry-run # 預覽
python scripts/run_ingestion.py --force   # 強制重匯
```

### 啟動

```bash
./start.sh
# 自動完成：
#   1. 啟動 / 恢復 Qdrant Docker（只綁 127.0.0.1:6333）
#   2. 等待 Qdrant 健康檢查通過（最多 30 秒）
#   3. 前台執行 Streamlit → http://localhost:8501
#      （Ctrl+C 停止 Streamlit，Qdrant 繼續在背景運行）
```

### Streamlit 伺服器設定（`.streamlit/config.toml`）

```toml
[server]
address = "127.0.0.1"       # 本機只聽本地；對外改 0.0.0.0 + nginx HTTPS
port = 8501
headless = true
enableXsrfProtection = true  # XSRF 保護（明確設定防被覆蓋）
enableCORS = false

[browser]
gatherUsageStats = false     # 不回傳統計到 Streamlit 伺服器
```

> ⚠️ **對外服務**：須在前端加 nginx 反向代理並啟用 HTTPS；不建議直接暴露 Streamlit port。

### 手動驗證各模組

```bash
python src/core/llm_client.py     # 印出 active backend + 測試呼叫
python src/core/qdrant_client.py  # health check + 確保 collection 存在
python src/agent/graph.py         # 編譯並印出 graph 結構
python -m py_compile src/ui/app.py # 語法檢查
```

---

## 12. 雲端部署（Streamlit Cloud）

### 前置條件

| 項目 | 說明 |
|---|---|
| Qdrant Cloud | 在 [qdrant.tech](https://qdrant.tech) 建立免費 cluster（1GB 免費）|
| LLM API Key | 至少一個（Gemini 免費層推薦）|
| GitHub repo | Fork 此 repo 到自己帳號 |

### 步驟一：資料遷移

先將本地 Qdrant 資料遷移至 Qdrant Cloud：

```bash
# 在 .env 填入雲端資訊
QDRANT_URL=https://xxxx-xxxx.qdrant.io
QDRANT_API_KEY=your-cloud-key

# 執行遷移（自動建立 collection + payload indexes）
python scripts/migrate_to_cloud.py

# 遷移過程：
#   1. 連接本地 localhost:6333
#   2. 確認 collection "earnings_calls" 存在
#   3. 在雲端建立同名 collection（cosine, 768 維）
#   4. 分批 scroll（100 筆/批）上傳至 Qdrant Cloud
#   5. 建立 payload indexes（company / quarter / section）
#      ← Cloud 嚴格模式下沒 index 無法 filter，這步不可省略
```

### 步驟二：Streamlit Cloud 設定

1. 前往 [streamlit.io/cloud](https://streamlit.io/cloud) → New app
2. 連結 GitHub repo，主檔案設為 `src/ui/app.py`
3. 在 **Advanced settings → Secrets** 填入：

```toml
# .streamlit/secrets.toml（不進 git，在 Streamlit Cloud 介面填入）
GEMINI_API_KEY = "your-key"          # 或其他 LLM key
QDRANT_URL = "https://xxxx.qdrant.io"
QDRANT_API_KEY = "your-cloud-key"
APP_PASSWORD = "your-access-password" # 建議設定，防止未授權存取
COHERE_API_KEY = "your-key"          # 可選，啟用 Rerank
TAVILY_API_KEY = "your-key"          # 可選，啟用新聞搜尋
```

4. 點擊 Deploy → 約 3–5 分鐘取得公開 URL

### 雲端 vs 本地差異

| 項目 | 本地端 | Streamlit Cloud |
|---|---|---|
| Qdrant | Docker `localhost:6333` | Qdrant Cloud（`QDRANT_URL`）|
| 環境變數 | `.env` 檔案 | Streamlit Secrets（`secrets.toml`）|
| Embedding | 本地執行（`sentence-transformers`）| 同上（Cloud 也在 container 內執行）|
| 字體（PDF 匯出）| STHeiti（macOS）| Noto CJK（Linux 降級）|
| 存取控制 | 可選（`APP_PASSWORD`）| **強烈建議設定** |
| Qdrant 暴露 | 只綁 `127.0.0.1` | 由 Qdrant Cloud 管控 |

### Qdrant Cloud 注意事項

- Collection 名稱固定：`earnings_calls`
- **payload indexes 必須存在**（`company`, `quarter`, `section`）；`migrate_to_cloud.py` 的最後一步已自動建立
- 免費 cluster 有 idle 限制；若長時間無存取可能需要重新喚醒

---

## 13. 環境變數完整清單

### `.env`（本地）/ Streamlit Secrets（雲端）

| 變數 | 必要性 | 說明 |
|---|---|---|
| `OPENAI_API_KEY` | **擇一** ★ | GPT-5o / GPT-5o-mini（主力，2026-05 更新）|
| `GEMINI_API_KEY` | **擇一** | Gemini 3.0 Flash（免費額度大）|
| `COHERE_API_KEY` | **擇一** | Command R+（同時用於 Rerank）|
| `TAVILY_API_KEY` | 選填 | 即時新聞搜尋 |
| `QDRANT_URL` | 選填 | Qdrant Cloud URL；不填用本地 Docker |
| `QDRANT_API_KEY` | 選填 | Qdrant Cloud Key |
| `QDRANT_HOST` | 選填 | 本地 host（預設 `localhost`）|
| `QDRANT_PORT` | 選填 | 本地 port（預設 `6333`）|
| `LLM_BACKEND` | 選填 | 強制後端（`openai / gemini / cohere`）|
| `LLM_TIMEOUT_SECONDS` | 選填 | LLM 呼叫 timeout（預設 45）|
| `LLM_INJECTION_GUARD` | 選填 | 設 `false` 停用 injection guardrail（測試用）|
| `APP_PASSWORD` | 選填 | 存取密碼；雲端部署**強烈建議**設定 |
| `LLAMA_CLOUD_API_KEY` | 選填 | LlamaParse（表格密集 PDF，免費 1000頁/天）|

---

## 14. 技術棧

| 分類 | 技術 | 版本需求 |
|---|---|---|
| Agent 框架 | LangGraph + LangChain-core | ≥ 0.2 |
| UI | Streamlit | ≥ 1.39 |
| 向量資料庫 | Qdrant | ≥ 1.9 |
| Embedding | sentence-transformers（paraphrase-multilingual-mpnet-base-v2）| ≥ 3.0 |
| 混合檢索 | rank_bm25 + jieba | ≥ 0.2.2 |
| Rerank | Cohere rerank-multilingual-v3.0 | ≥ 5.0 |
| PDF 解析 | pdfplumber（主）/ LlamaParse（備援）| ≥ 0.11 |
| 圖表 | Plotly | ≥ 5.0 |
| PDF 匯出 | fpdf2 | ≥ 2.7 |
| 即時資料 | Tavily（新聞）/ yfinance（股價）| ≥ 0.5 / ≥ 0.2.40 |
| 測試 | pytest | — |
| CI | GitHub Actions（ubuntu-latest, Python 3.11）| — |

---

## 15. 技術債與 Roadmap

詳見 [`docs/ROADMAP.md`](ROADMAP.md)。

| 優先度 | 項目 | 動工觸發條件 |
|---|---|---|
| 🟡 Medium | `src/ui/session.py` — 集中 session-state magic strings | 下次修改 `app.py` rendering 前 |
| 🟢 Low | 拆分 single/multi rendering 進獨立 view 檔案 | 要新增第三種 view 時 |
| 🟢 Low | LangGraph `SqliteSaver` checkpoint | 有人抱怨「Agent 斷掉要重跑」 |
| 🟢 Low | Per-user rate limiting（cookie hash）| 對外公開多人使用時 |
| 🟢 Low | CI Streamlit smoke test（`curl /_stcore/health`）| 發生「CI 綠但生產壞」後 |

**明確不做**：AI 替代分析師寫報告、股價預測、使用者帳號系統、支援所有美股、fine-tune 自有模型。

---

## 16. 免責聲明

EarningsWatch 為**文件分析工具**，不提供投資建議或股價預測。  
資料來源：公開資訊觀測站（MOPS）法說會逐字稿。  
分析結果僅供參考，投資決策請自行判斷。
