# query-decomposition Specification

## Purpose

定義 LangGraph `decompose` 節點 `query_decomposer`（`src/agent/nodes.py`）的行為：用 LLM 把使用者問題拆解為 2~5 條子查詢、季度 scope 注入、英文縮寫保留、欄位驗證與截斷、LLM 失敗時降級為固定樣板。本 spec 確保子查詢拆解可預測、子查詢結構契約穩定（供 `parallel_retrieval` 消費）、LLM 故障不會中斷 pipeline。

## Requirements

### Requirement: `query_decomposer` SHALL 用 LLM 產生 2~5 條結構化子查詢

`query_decomposer(state)` MUST 呼叫 LLM 將 `query` / `company` / `topic` 拆解為子查詢列表。每條子查詢 MUST 為 dict，包含：
- `id`: 字串識別（LLM 缺值時以 `sq_{index}` 補）
- `query`: 實際送進 retriever / Tavily 的查詢字串
- `purpose`: 該子查詢目的（log 顯示用）
- `tool`: `"bigquery"` 或 `"tavily"`
- `section_filter`: 選填，限制 BigQuery section（如 `"guidance"`）

回傳 dict MUST 含 `sub_queries`（list）與 `steps_log`（list）。

#### Scenario: LLM 正常回傳多條子查詢
- **GIVEN** LLM 回傳合法 JSON `{"sub_queries": [{id, query, purpose, tool}, ...]}`（2 條以上）
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 回傳的 `sub_queries` 含 LLM 提供的條目
- **AND** 每條都有 `id` / `query` / `purpose` / `tool`

### Requirement: 子查詢驗證 MUST 過濾不合法條目並截斷過長欄位

`query_decomposer` MUST 對 LLM 回傳的每條 candidate 套用驗證：
- candidate 非 dict → 跳過
- `query` 經 strip 後為空 → 跳過
- `tool` 不在 `{"bigquery", "tavily"}` → 跳過
- `query` MUST 截斷至 120 字元（防 prompt 爆量）
- `purpose` MUST 截斷至 40 字元；空值時以 `"子查詢"` 補
- `section_filter` 非空字串時才寫入 entry

#### Scenario: 非 dict candidate 被跳過
- **GIVEN** LLM 回傳的 `sub_queries` 含字串 / None 等非 dict 元素
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 那些元素不出現在最終 `sub_queries`，不 raise

#### Scenario: 不合法 tool 被跳過
- **GIVEN** 某 candidate `tool == "yfinance"`（不在白名單）
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 該 candidate 被丟棄

#### Scenario: 過長 query 被截斷
- **GIVEN** 某 candidate `query` 長度超過 120 字元
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 該子查詢的 `query` 長度 `<= 120`

#### Scenario: 空 purpose 補預設值
- **GIVEN** 某 candidate `purpose` 為空字串
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 該子查詢的 `purpose == "子查詢"`

### Requirement: 季度 scope SHALL 注入 LLM prompt

`query_decomposer` MUST 在 prompt 中加入季度範圍指示：
- `state["quarters"]` 非空時：prompt 含具體季度清單與推導出的年份
- `state["quarters"]` 為空時：prompt 明確指示「子查詢 query 字串不要寫入任何特定年份」

目的：避免 LLM 在 query 字串裡自行套用「2024」這類預設年份污染檢索。

#### Scenario: 指定季度時 prompt 含季度清單
- **GIVEN** `state["quarters"] == ["2024Q1", "2024Q3"]`
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 送進 LLM 的 prompt 含 `2024Q1` 與 `2024Q3`

#### Scenario: 未指定季度時 prompt 含「不要寫年份」指示
- **GIVEN** `state["quarters"]` 為空 list
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** prompt 含指示子查詢不得寫入特定年份的文字

### Requirement: LLM 失敗 MUST 降級為固定樣板

`query_decomposer` MUST 在 LLM 呼叫 raise、或回傳結果經驗證後 `sub_queries` 為空時，降級為 `_fallback_sub_queries(company, topic)` 的 3 條樣板（cross_quarter / guidance / news），並在 `steps_log` 記錄降級。MUST 不 raise。

`_fallback_sub_queries` 的 3 條樣板 MUST 分別為：
- `cross_quarter`：`tool="bigquery"`、query 含「各季比對」
- `guidance`：`tool="bigquery"`、`section_filter="guidance"`
- `news`：`tool="tavily"`

#### Scenario: LLM raise 時降級
- **GIVEN** `_llm(...)` raise
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 回傳的 `sub_queries` 為 3 條樣板
- **AND** `steps_log` 含「降級為樣板」字樣
- **AND** 不 raise

#### Scenario: LLM 回傳全部不合法時降級
- **GIVEN** LLM 回傳的 candidates 全部驗證失敗（空 query / 壞 tool）
- **WHEN** `query_decomposer(state)` 被呼叫
- **THEN** 回傳 3 條樣板子查詢

#### Scenario: 樣板涵蓋三種分析角度
- **WHEN** `_fallback_sub_queries("台積電", "AI需求")` 被呼叫
- **THEN** 回傳 3 條，其中恰有 1 條 `tool == "tavily"`、2 條 `tool == "bigquery"`
- **AND** 有 1 條 `section_filter == "guidance"`
