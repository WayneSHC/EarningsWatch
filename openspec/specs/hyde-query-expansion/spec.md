# hyde-query-expansion Specification

## Purpose

定義 `src/core/retriever.py` 的 HyDE（Hypothetical Document Embeddings）查詢擴展行為：用 LLM 生成「假設性回答」作為實際 embed 對象，提升短查詢對長文檔的 recall。本 spec 規範啟用旗標、查詢長度門檻、LRU 快取、LLM 失敗時的降級，確保功能可關閉、可預測、不會在失敗時破壞檢索主路徑。

## Requirements

### Requirement: HyDE SHALL 預設關閉，由環境變數 `LLM_HYDE_ENABLED` 啟用

`_HYDE_ENABLED` 模組級常數 MUST 在模組載入時讀取 `LLM_HYDE_ENABLED` 環境變數，並僅在小寫值為 `true` / `1` / `yes` 中之一時設為 `True`。`_maybe_expand(query)` MUST 在 `_HYDE_ENABLED` 為 `False` 時直接回傳原 query，不發起 LLM 呼叫。

#### Scenario: 環境變數未設時不啟用
- **GIVEN** `LLM_HYDE_ENABLED` 未設定
- **WHEN** 模組被載入並呼叫 `_maybe_expand("台積電 AI 需求")`
- **THEN** 回傳值等於原 query
- **AND** `_hyde_expand` 不被呼叫

#### Scenario: 任何非真值字串都不啟用
- **GIVEN** `LLM_HYDE_ENABLED=no` 或 `LLM_HYDE_ENABLED=off`
- **WHEN** `_maybe_expand("...")` 被呼叫
- **THEN** 不觸發 LLM 呼叫

### Requirement: 短查詢 SHALL 不觸發 HyDE 擴展

`_maybe_expand(query)` MUST 在 `query.strip()` 字元數 `< _HYDE_MIN_QUERY_LEN`（預設 `6`）時直接回傳原 query，即便 `_HYDE_ENABLED == True`。原因：極短查詢往往本身就是關鍵字而非語意完整的問題，HyDE 對其無加值且浪費 LLM 呼叫。

#### Scenario: 5 字元以下不擴展
- **GIVEN** `_HYDE_ENABLED == True`
- **WHEN** `_maybe_expand("毛利")` 被呼叫（4 字元）
- **THEN** 回傳 `"毛利"`，`_hyde_expand` 不被呼叫

#### Scenario: 6 字元以上觸發
- **GIVEN** `_HYDE_ENABLED == True`
- **WHEN** `_maybe_expand("台積電 2024 毛利率")` 被呼叫
- **THEN** `_hyde_expand` 被呼叫

### Requirement: `_hyde_expand` MUST 呼叫 LLM 並使用 `mode="dev"`

`_hyde_expand(query)` MUST 將 prompt 透過 `llm_client.chat()` 送出，並指定 `max_tokens=200`、`mode="dev"`（使用便宜模型，避免 demo 模型成本）。prompt MUST 要求生成「80~150 字繁體中文」、「法說會逐字稿口吻」、「不要引言或標題」。

#### Scenario: LLM 回傳的內容作為擴展結果
- **GIVEN** LLM 回傳 `"AI 需求動能延續，公司預期...先進製程..."`
- **WHEN** `_hyde_expand("台積電 AI 需求")` 被呼叫
- **THEN** 回傳 LLM 的回應字串（`strip()` 後）

#### Scenario: LLM 回應為空時降級為原 query
- **GIVEN** LLM 回傳 `""` 或全空白
- **WHEN** `_hyde_expand("...")` 被呼叫
- **THEN** 回傳原 query

### Requirement: LLM 失敗 MUST 降級為原 query，不 raise

`_hyde_expand` MUST 對 LLM 呼叫包裝 try/except；任何 LLM 例外 MUST 被吞掉，回傳原 query 並 print 警告（含例外類別名稱）。此設計保證 HyDE 失敗不會中斷檢索主路徑。

#### Scenario: LLM raise 時降級
- **GIVEN** `llm_chat(...)` raise `LLMUnavailableError`
- **WHEN** `_hyde_expand("台積電 AI 需求")` 被呼叫
- **THEN** 回傳 `"台積電 AI 需求"`
- **AND** stdout 含 `HyDE 生成失敗` 與例外類別名稱字樣

### Requirement: HyDE 結果 MUST 以 LRU 快取以查詢字串為 key

`_hyde_expand` MUST 使用 `lru_cache(maxsize=128)` 裝飾；相同 query 字串在同一個 process 內 MUST 只觸發一次 LLM 呼叫，後續直接讀快取。

#### Scenario: 重複查詢命中快取
- **GIVEN** `_hyde_expand("台積電 AI 需求")` 已被呼叫一次（LLM 已被呼叫）
- **WHEN** 同一 query 再次被傳入
- **THEN** `llm_chat` 不再被呼叫，回傳上一次的擴展結果

### Requirement: HyDE 整合至 `vector_search` 與 `retrieve_coverage` 的 embedding 流程

`vector_search(query, ...)` 與 `retrieve_coverage(query, ...)` MUST 在 embed 前呼叫 `_maybe_expand(query)`，並將結果作為 `embed_query(...)` 的輸入。`rerank()` MUST 仍使用原 query（非擴展後）作為 reranker 的 query 字串，因為 reranker 比對語意相關性，原 query 較精確。

#### Scenario: vector_search 用擴展後的 query 做 embedding
- **GIVEN** `_HYDE_ENABLED == True` 且 `_hyde_expand("...")` 回傳擴展字串 `X`
- **WHEN** `vector_search("...", ...)` 被呼叫
- **THEN** 傳給 `embed_query()` 的字串為 `X`

#### Scenario: HyDE 關閉時 vector_search 用原 query
- **GIVEN** `_HYDE_ENABLED == False`
- **WHEN** `vector_search("台積電 AI", ...)` 被呼叫
- **THEN** 傳給 `embed_query()` 的字串為 `"台積電 AI"`
