# gemini-embedding Specification

## Purpose

定義 `src/ingestion/embedder.py` 對 `gemini-embedding-2` 的呼叫行為：模型 ID 與輸出維度（MRL 截斷至 768）、document / query 兩種 prompt prefix、單筆批次（`BATCH_SIZE=1`）、跨呼叫 RPM 節流（`_INTER_CALL_SLEEP`）、429 / `RESOURCE_EXHAUSTED` 指數退避重試、`upsert_chunks` 寫入 BigQuery 的 deterministic ID 生成。本 spec 確保 ingestion 路徑在 Gemini 免費級距內穩定運作，且 embedding payload 與既有 BigQuery schema 維度相容。

## Requirements

### Requirement: 模型 ID SHALL 為 `gemini-embedding-2`，輸出維度 SHALL 為 768

`EMBEDDING_MODEL` 常數 MUST 等於字串 `"gemini-embedding-2"`。`EMBED_DIM` 常數 MUST 等於 `768`。呼叫 `client.models.embed_content(...)` 時 MUST 以 `EmbedContentConfig(output_dimensionality=EMBED_DIM)` 設定 MRL 截斷，使輸出向量為 768 維。

#### Scenario: 模型常數鎖定
- **WHEN** 讀取 `embedder.EMBEDDING_MODEL`
- **THEN** 等於 `"gemini-embedding-2"`

#### Scenario: 輸出維度鎖定
- **WHEN** 讀取 `embedder.EMBED_DIM`
- **THEN** 等於 `768`

### Requirement: 批次大小 SHALL 為 1（單筆呼叫）

`BATCH_SIZE` MUST 等於 `1`，且 `_embed()` 對輸入 texts list 以該批次大小逐筆呼叫 SDK。原因：`gemini-embedding-2` 是多模態模型，contents 為 list 時被視為一個多 part 輸入，必須一次傳一個文字才能得到逐筆 embedding。

#### Scenario: 三筆文字產生三次 SDK 呼叫
- **GIVEN** 輸入 3 筆文字
- **WHEN** `_embed(["a", "b", "c"])` 被呼叫
- **THEN** `client.models.embed_content` 被呼叫 3 次

### Requirement: Document 與 query embedding SHALL 套用不同 prompt prefix

`embed_documents(texts)` MUST 對每個 text 套用前綴 `"title: none | text: "`；`embed_query_texts(texts)` MUST 對每個 text 套用前綴 `"task: search result | query: "`。`embed_texts(texts)` MUST 為向後相容別名，等同於 `embed_documents(texts)`。

#### Scenario: Document 前綴正確
- **WHEN** `embed_documents(["AI 需求強勁"])` 被呼叫
- **THEN** 實際傳給 SDK 的 contents 為 `["title: none | text: AI 需求強勁"]`

#### Scenario: Query 前綴正確
- **WHEN** `embed_query_texts(["毛利率"])` 被呼叫
- **THEN** 實際傳給 SDK 的 contents 為 `["task: search result | query: 毛利率"]`

#### Scenario: 向後相容別名
- **WHEN** `embed_texts(["x"])` 被呼叫
- **THEN** SDK 收到的 contents 為 `["title: none | text: x"]`（與 `embed_documents` 同）

### Requirement: 429 / RESOURCE_EXHAUSTED MUST 觸發指數退避重試

`_embed_once(client, batch_texts, config)` MUST 在 SDK 拋出 `google.genai.errors.ClientError`，且：
- `code == 429`，或
- `status_code == 429`，或
- 錯誤訊息含 `"RESOURCE_EXHAUSTED"` 或 `"429"`

時，等待 `_RETRY_BASE_SLEEP * 2^attempt` 秒後重試，最多重試 `_RETRY_MAX` 次（預設 3 次，總等待 8s/16s/32s ≈ 56s 跨越多數 RPM 視窗）。重試耗盡 MUST raise 最後一次例外。

#### Scenario: 第一次 429 後重試成功
- **GIVEN** SDK 第一次呼叫 raise `ClientError(code=429)`
- **AND** 第二次呼叫成功回傳 embeddings
- **WHEN** `_embed_once(...)` 被呼叫
- **THEN** 回傳第二次的結果，不 raise

#### Scenario: RESOURCE_EXHAUSTED 訊息也觸發重試
- **GIVEN** SDK raise `ClientError("RESOURCE_EXHAUSTED quota exceeded")`
- **WHEN** `_embed_once(...)` 被呼叫
- **THEN** 進入重試分支（不直接 raise）

#### Scenario: 非 429 ClientError 立刻 raise
- **GIVEN** SDK raise `ClientError(code=401, "Unauthorized")`
- **WHEN** `_embed_once(...)` 被呼叫
- **THEN** raise 原始 `ClientError`，不重試

#### Scenario: 重試耗盡後 raise
- **GIVEN** SDK 每次都 raise 429
- **WHEN** `_embed_once(...)` 被呼叫
- **THEN** 經過 `_RETRY_MAX` 次重試後 raise 最後一次例外

### Requirement: 連續呼叫之間 SHALL 套用 RPM 節流 sleep

`_embed(texts)` MUST 在每兩次連續 SDK 呼叫之間（即 `i + BATCH_SIZE < len(texts)`）`time.sleep(_INTER_CALL_SLEEP)` 秒。`_INTER_CALL_SLEEP` 預設值為 `0.6` 秒（環境變數 `EMBED_INTER_CALL_SLEEP` 可覆寫），對應 ~100 RPM 上限，留在 Gemini 免費級距內。`_INTER_CALL_SLEEP <= 0` 時 MUST 不 sleep。

#### Scenario: 三筆呼叫之間插入兩次 sleep
- **GIVEN** `_INTER_CALL_SLEEP = 0.6`
- **WHEN** `_embed(["a", "b", "c"])` 被呼叫
- **THEN** `time.sleep(0.6)` 被呼叫恰好 2 次（在第 1→2、2→3 之間）

#### Scenario: 最後一筆後不 sleep
- **GIVEN** `_INTER_CALL_SLEEP = 0.6`
- **WHEN** `_embed(["a"])` 被呼叫（單筆）
- **THEN** `time.sleep` 不被呼叫

#### Scenario: 環境變數覆寫節流時間
- **GIVEN** `EMBED_INTER_CALL_SLEEP=0`
- **WHEN** 模組重新載入並執行 `_embed(["a", "b"])`
- **THEN** `time.sleep` 不被呼叫

### Requirement: 客戶端 SHALL 以 `lru_cache` 單例化

`_get_client()` MUST 使用 `lru_cache(maxsize=1)`，第一次呼叫初始化 `genai.Client(api_key=...)`，後續呼叫直接回傳同一物件。API key MUST 透過 `src.core.secrets.get_secret("GEMINI_API_KEY")` 取得，缺 key 時 fall back 至 `os.getenv("GOOGLE_API_KEY")`，仍無則 raise `RuntimeError`。

#### Scenario: 重複呼叫共用單一 client
- **GIVEN** 環境含有效 `GEMINI_API_KEY`
- **WHEN** `_get_client()` 連續呼叫兩次
- **THEN** 兩次回傳同一個物件（`is` 相同）

#### Scenario: 缺 key raise
- **GIVEN** 環境無 `GEMINI_API_KEY` 也無 `GOOGLE_API_KEY`
- **WHEN** `_get_client()` 被呼叫（且 lru_cache 已清）
- **THEN** raise `RuntimeError` 含「缺少 GEMINI_API_KEY」字樣

### Requirement: `upsert_chunks` SHALL 使用 deterministic UUID 作為 chunk ID

`upsert_chunks(chunks, show_progress=True)` MUST 為每個 chunk 生成 deterministic ID：以 `f"{source_file}::{source_page}::{chunk_index}"` 為種子計算 SHA-256，取前 32 字元並格式化為 UUID 形狀 `8-4-4-4-12`。相同的 (source_file, source_page, chunk_index) MUST 永遠映射到相同的 ID，使重複匯入同一 PDF 不會產生重複資料。

#### Scenario: 相同種子產生相同 ID
- **GIVEN** 兩個 chunk dict 含相同的 `source_file`、`source_page`、`chunk_index`
- **WHEN** `upsert_chunks([c1])` 與 `upsert_chunks([c2])` 分別被呼叫
- **THEN** 寫入 BigQuery 的 `id` 欄相同

#### Scenario: ID 為 UUID 格式
- **WHEN** chunk 被 upsert
- **THEN** 對應的 `id` 字串符合 regex `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`

### Requirement: `upsert_chunks` MUST 以 `UPSERT_BATCH=100` 分批寫入 BigQuery

`upsert_chunks` MUST 將生成的 rows 依 `UPSERT_BATCH`（=100）大小分批呼叫 `client.insert_rows_json(table_id, batch)`。任一批 `errors` 非空時 MUST 印出錯誤但繼續下一批；只有成功批次計入 `total_written`。

#### Scenario: 250 筆分三批
- **GIVEN** 輸入 250 個 chunks，BigQuery 全部成功（`errors == []`）
- **WHEN** `upsert_chunks(chunks)` 被呼叫
- **THEN** `client.insert_rows_json` 被呼叫 3 次（100 + 100 + 50）
- **AND** 回傳 `total_written == 250`

#### Scenario: 部分批失敗時其他批仍寫入
- **GIVEN** 200 個 chunks，第二批 `insert_rows_json` 回傳 `errors=[{...}]`
- **WHEN** `upsert_chunks(chunks)` 被呼叫
- **THEN** 回傳 `total_written == 100`（只有第一批計入）
- **AND** stdout 印出包含 `BigQuery 寫入錯誤` 字樣

### Requirement: 空輸入 MUST 直接回傳 0

`upsert_chunks([])` MUST 不呼叫任何 BigQuery API，直接回傳 `0`，並印出「沒有 chunk 可寫入」字樣。

#### Scenario: 空輸入
- **WHEN** `upsert_chunks([])` 被呼叫
- **THEN** 回傳 `0`
- **AND** `client.insert_rows_json` 不被呼叫
