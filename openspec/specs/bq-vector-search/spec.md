# bq-vector-search Specification

## Purpose

定義 `src/core/bq_client.py` 與 `src/core/retriever.py` 對 BigQuery `VECTOR_SEARCH` 的使用約定：BigQuery client 單例、資料集與表 schema 自動建立、`VECTOR_SEARCH` 的 filter pushdown 模式、cosine distance → similarity 轉換、Cohere rerank 整合、`retrieve_coverage` 的 PARTITION BY top-k 策略。本 spec 確保（a）filter 在內層 base table 套用避免 BadRequest、（b）所有 client 共用單一連線、（c）schema 變更時有單一定義來源。

## Requirements

### Requirement: BigQuery client SHALL 以 `lru_cache` 單例化，憑證二段解析

`get_bq_client()`（`src/core/bq_client.py`）MUST 使用 `lru_cache(maxsize=1)` 回傳單例 `bigquery.Client`。憑證解析二段式：
1. 若 `st.secrets["gcp_service_account"]` 存在（Streamlit Cloud，無 ADC）→ 以 `service_account.Credentials.from_service_account_info(...)` 建構並傳入 `credentials` 參數
2. 否則 MUST 不傳 credentials 參數（依賴 ADC / GCP runtime 預設認證）

`streamlit` import MUST 是 guarded soft-import（ImportError / 無 secrets → 走 ADC），此為 core 層唯一獲准的 streamlit 依賴（constitution v1.0.2 Principle I 例外）。金鑰存在但格式損壞 MUST 大聲失敗，不得靜默退回 ADC。

`PROJECT_ID` MUST 依序解析：`GOOGLE_CLOUD_PROJECT` 環境變數 → SA 金鑰內的 `project_id` → `"earningswatch-demo"`；client 與 `get_table_path()` MUST 使用同一解析結果。

#### Scenario: 連續呼叫共用 client
- **WHEN** `get_bq_client()` 連續呼叫兩次
- **THEN** 兩次回傳同一物件（`is` 相同）

#### Scenario: 環境變數覆寫專案
- **GIVEN** `GOOGLE_CLOUD_PROJECT=my-proj`
- **WHEN** 模組被重新載入並呼叫 `get_bq_client()`
- **THEN** client 的 `project == "my-proj"`

#### Scenario: st.secrets 有 SA 金鑰時用 SA 憑證
- **GIVEN** `st.secrets["gcp_service_account"]` 為有效 SA dict
- **WHEN** `get_bq_client()` 被呼叫
- **THEN** `bigquery.Client` 收到 `credentials` 參數

#### Scenario: 無 SA 金鑰時走 ADC
- **GIVEN** 無 `secrets.toml`（或不含 `gcp_service_account`）
- **WHEN** `get_bq_client()` 被呼叫
- **THEN** `bigquery.Client` 建構參數不含 `credentials`

#### Scenario: 只設 SA 金鑰時 project 退回 SA project_id
- **GIVEN** `GOOGLE_CLOUD_PROJECT` 未設、SA dict 內 `project_id == "sa-proj"`
- **WHEN** `_resolve_project_id()` 被呼叫
- **THEN** 回傳 `"sa-proj"`

### Requirement: `get_table_path()` SHALL 回傳完整路徑

`get_table_path()` MUST 回傳 `"{PROJECT_ID}.earnings_data.earnings_calls"` 格式的完整 BigQuery 路徑字串。

#### Scenario: 預設專案的完整路徑
- **GIVEN** `PROJECT_ID = "earningswatch-demo"`
- **WHEN** `get_table_path()` 被呼叫
- **THEN** 回傳 `"earningswatch-demo.earnings_data.earnings_calls"`

### Requirement: `ensure_dataset_and_table()` SHALL 是 idempotent

`ensure_dataset_and_table(client=None)` MUST 在 Dataset / Table 已存在時不重建、不 raise；不存在時建立。Table schema MUST 包含至少：`id (STRING REQUIRED)`、`company` / `quarter` / `section` / `content` / `source_file` (STRING NULLABLE)、`source_page` / `chunk_index` (INTEGER NULLABLE)、`embedding (FLOAT64 REPEATED)`。

#### Scenario: 既存 Dataset 不重建
- **GIVEN** Dataset 已存在
- **WHEN** `ensure_dataset_and_table()` 被呼叫
- **THEN** `client.create_dataset` 不被呼叫
- **AND** stdout 含 `Dataset earnings_data 已存在` 字樣

#### Scenario: 缺 Table 時建立含 embedding REPEATED 欄位
- **GIVEN** Dataset 已存在但 Table 不存在
- **WHEN** `ensure_dataset_and_table()` 被呼叫
- **THEN** `client.create_table` 被呼叫
- **AND** 傳入的 schema 含 `embedding` 欄位且 mode 為 `REPEATED`

### Requirement: `vector_search` SHALL 將 filter 推至 `VECTOR_SEARCH` 外層 WHERE 用 `base.X` 引用

`vector_search(query, company, quarters, section, top_k)` MUST 使用 BigQuery `VECTOR_SEARCH`：
- `base_table_query` 為 `TABLE \`{table_path}\``（不可用 query parameter — BQ 限制）
- 內層 `top_k => max(top_k * 20, 200)` 為**字面值**（VECTOR_SEARCH named arg 不接受 parameter），放寬 inner k 後外層 LIMIT 截至最終 `top_k`
- 外層 `WHERE` 用 `base.company` / `base.quarter` / `base.section` 篩選（因 VECTOR_SEARCH 把原始欄位包進 `base` struct）
- `distance_type => 'COSINE'`
- `ORDER BY distance` 升冪、`LIMIT {top_k}`

#### Scenario: 含 company filter 時 SQL 含 `base.company`
- **WHEN** `vector_search("...", company="台積電", top_k=5)` 被呼叫
- **THEN** 傳給 `client.query` 的 SQL 含 `base.company = @company` 字串
- **AND** 字面值 inner `top_k => 200`（因 `5 * 20 = 100 < 200`）

#### Scenario: 含 quarters filter 時用 UNNEST
- **WHEN** `vector_search("...", quarters=["2024Q1", "2024Q2"], top_k=5)` 被呼叫
- **THEN** SQL 含 `base.quarter IN UNNEST(@quarters)` 字串

#### Scenario: 無 filter 時外層 WHERE 為 TRUE
- **WHEN** `vector_search("...", top_k=5)` 被呼叫（無 company / quarters / section）
- **THEN** SQL 的外層 WHERE 為 `TRUE`

### Requirement: cosine distance → similarity 轉換

`vector_search` 與 `retrieve_coverage` MUST 將 BigQuery 回傳的 `distance` 欄位轉為 similarity：`score = 1.0 - distance`。回傳的 hit dict MUST 含 `score`（float）、`id`、`payload`（含 company / quarter / section / content / source_file / source_page / chunk_index）。

#### Scenario: distance 0.2 對應 score 0.8
- **GIVEN** BigQuery 回傳一筆 `distance == 0.2`
- **WHEN** `vector_search(...)` 處理
- **THEN** 回傳 hit 的 `score == 0.8`

### Requirement: `rerank` SHALL 使用 Cohere `rerank-v3.5`，無 client 時短路

`rerank(query, candidates, top_n)` MUST 透過 `_get_cohere_client()` 取得 client；client 為 `None`（無 COHERE_API_KEY）或 candidates 為空時 MUST 直接回傳 `candidates[:top_n]`，不發起 Cohere 呼叫。重新排序後 MUST 保留原本 candidate 的所有欄位並附加 `rerank_score`。

#### Scenario: 無 Cohere key 時短路
- **GIVEN** `COHERE_API_KEY` 未設、`candidates` 有 10 筆
- **WHEN** `rerank("...", candidates, top_n=5)` 被呼叫
- **THEN** 回傳 `candidates[:5]`、Cohere SDK 不被呼叫

#### Scenario: 空 candidates 直接回傳
- **WHEN** `rerank("...", [], top_n=5)` 被呼叫
- **THEN** 回傳 `[]`

#### Scenario: rerank 結果含 rerank_score
- **GIVEN** Cohere 回傳重新排序的 results
- **WHEN** `rerank(...)` 完成
- **THEN** 回傳列表每個 item 含 `rerank_score`（float）且其餘欄位來自原 candidate

### Requirement: `rerank` Cohere 呼叫失敗 MUST 降級而非崩潰

`rerank` MUST 對 `client.rerank(...)` 包 try/except。任何例外（429 Trial-key 速率限制、金鑰失效、服務中斷、SDK 錯誤等）MUST 被捕獲，函數 MUST 改回傳 `candidates[:top_n]`（vector-search 原始相似度排序），並 print 一則含例外類別名稱的降級警告。

理由：rerank 只是「精修」步驟，`vector_search` 已回傳按 cosine 相似度排序的候選。若 rerank 例外向上傳播，`retrieve()` 整個失敗，`parallel_retrieval` 的該子查詢結果整批丟失，連帶造成季度覆蓋不足、`contradiction_detect` 因 `len(retrieved) < 2` 被跳過。rerank 失敗 MUST NOT 造成整條檢索鏈崩潰。

#### Scenario: Cohere 429 時降級為 vector-search 排序
- **GIVEN** `client.rerank(...)` raise（例如 `429 Trial key` 速率限制）、`candidates` 有 10 筆
- **WHEN** `rerank("...", candidates, top_n=5)` 被呼叫
- **THEN** 回傳 `candidates[:5]`（未重排），不 raise
- **AND** stdout 含 `rerank 失敗` 與 `降級` 字樣

#### Scenario: 降級不影響後續檢索
- **GIVEN** 某 bigquery 子查詢的 `retrieve()` 內部 rerank 失敗
- **WHEN** `parallel_retrieval` 處理該子查詢
- **THEN** 該子查詢仍取得 vector-search 結果（不被當作整體失敗丟棄）

### Requirement: `rerank` 呼叫前 MUST 經 Cohere 速率節流器

`rerank` MUST 在呼叫 `client.rerank(...)` 之前先 `_cohere_throttle.acquire()`。`_CohereThrottle` 為 thread-safe 的 sliding-window 速率限制器：60 秒視窗內前 `COHERE_MAX_RPM`（預設 `10`）次呼叫無延遲通過，超出的呼叫 block 至視窗有空位。`COHERE_MAX_RPM` 由環境變數設定，非整數 / 負數時 fallback 至 `10` 並印警告；設為 `0` MUST 完全停用節流（production key 上限較高時用）。

理由：Cohere Trial key 限 10 calls/min。互動式單次查詢只發少數 rerank 呼叫、不會觸及上限；但爆量負載（benchmark、多公司並行）會超限觸發 429 → rerank 全面降級、檢索品質下降。節流器讓免費 key 在所有情境都正確運作 —— 互動零延遲、爆量自動排隊。

#### Scenario: 視窗內未達上限不延遲
- **GIVEN** `_CohereThrottle(max_rpm=10)`
- **WHEN** 連續 `acquire()` 10 次
- **THEN** `time.sleep` 不被呼叫

#### Scenario: 超出上限的呼叫 block
- **GIVEN** `_CohereThrottle(max_rpm=3)`、已 `acquire()` 3 次
- **WHEN** 第 4 次 `acquire()`
- **THEN** `time.sleep` 被呼叫一次，等待時間在 `(0, 60]` 秒

#### Scenario: COHERE_MAX_RPM=0 停用節流
- **GIVEN** `_CohereThrottle(max_rpm=0)`
- **WHEN** 連續 `acquire()` 多次
- **THEN** `time.sleep` 不被呼叫

#### Scenario: 非法 COHERE_MAX_RPM fallback
- **GIVEN** `COHERE_MAX_RPM=banana`（或負數）
- **WHEN** `_load_cohere_max_rpm()` 被呼叫
- **THEN** 回傳 `10`

#### Scenario: rerank 先取節流額度再呼叫 Cohere
- **WHEN** `rerank(...)` 被呼叫且 Cohere client 可用、candidates 非空
- **THEN** `_cohere_throttle.acquire()` 在 `client.rerank(...)` 之前被呼叫

### Requirement: `retrieve_coverage` SHALL 用 PARTITION BY 一次取多季度 top-k

`retrieve_coverage(query, company, missing_quarters, top_k_per_quarter, min_score, max_quarters, use_rerank)` MUST：
1. 對 `missing_quarters` 截斷至 `max_quarters`（預設 8）的最新季度
2. 用單一 SQL 透過 `ROW_NUMBER() OVER(PARTITION BY base.quarter ORDER BY distance)` 在 BigQuery 端為每季取 top-k
3. 內層 `VECTOR_SEARCH(... top_k => 200)` 放寬整體量、外層用 `rn <= @fetch_k` 截斷
4. `distance <= @max_distance`（`max_distance = 1.0 - min_score`）過濾低相似度
5. 若 `use_rerank == True` 且某季 candidates 超過 `top_k_per_quarter`，呼叫 `rerank()` 做最終截斷
6. 缺結果的季度 MUST print 警告（含分數門檻字樣）並從回傳結果中省略

#### Scenario: 空 missing_quarters 直接回傳空 dict
- **WHEN** `retrieve_coverage("...", "台積電", [])` 被呼叫
- **THEN** 回傳 `{}`、BigQuery 不被呼叫

#### Scenario: 超過 max_quarters 時取最新
- **GIVEN** `missing_quarters = ["2024Q1", "2024Q2", ..., "2026Q1"]`（共 10 個）、`max_quarters=8`
- **WHEN** `retrieve_coverage(...)` 被呼叫
- **THEN** 實際查詢的季度為排序後最後 8 個

#### Scenario: 分數不足的季度被警告
- **GIVEN** 某季所有 chunk 的 `1 - distance < min_score`
- **WHEN** `retrieve_coverage(...)` 完成
- **THEN** 回傳結果不含該季
- **AND** stdout 含 `分數不足 {min_score}` 字樣
