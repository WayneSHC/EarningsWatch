# parallel-retrieval Specification

## Purpose

定義 LangGraph `retrieve` 節點 `parallel_retrieval`（`src/agent/nodes.py`）的行為：依 `sub_queries` 並行發出 BigQuery / Tavily / yfinance 三路檢索、自適應 top_k、per-tool 錯誤隔離、retry 輪以前輪結果為基底、chunk 去重、季度覆蓋補洞觸發。本 spec 確保檢索並行不互相阻塞、單一工具失敗不影響其他、retry 不覆蓋已取得的資料。

## Requirements

### Requirement: `parallel_retrieval` SHALL 用 `ThreadPoolExecutor` 並行三路檢索

`parallel_retrieval(state)` MUST 依 `tool_plan` 把任務提交至 `ThreadPoolExecutor`：
- 每條 `tool == "bigquery"` 的 sub_query 各一個 task
- `"tavily"` in tool_plan 時一個 `search_news` task
- `"yfinance"` in tool_plan 時一個 `get_stock_price` task

`max_workers` MUST 為 `min(n_bigquery_subqueries + 2, 6)`。結果以 `as_completed` 收集。回傳 dict MUST 含 `retrieved` / `news_context` / `stock_data` / `steps_log`。

#### Scenario: 多條 bigquery 子查詢並行
- **GIVEN** `sub_queries` 含 2 條 `tool=="bigquery"`、`tool_plan` 含 `"bigquery"`
- **WHEN** `parallel_retrieval(state)` 被呼叫
- **THEN** `retrieve()` 被各子查詢呼叫一次

#### Scenario: tool_plan 未含某工具則不呼叫
- **GIVEN** `tool_plan == ["bigquery"]`（無 tavily / yfinance）
- **WHEN** `parallel_retrieval(state)` 被呼叫
- **THEN** `search_news` 與 `get_stock_price` 不被呼叫

### Requirement: 自適應 top_k SHALL 依子查詢類型調整

`parallel_retrieval` 內部 `_adaptive_top_k(sq)` MUST 依下列規則決定每條 bigquery 子查詢的 `top_k`：
- 基準 `base = 5`
- `id == "cross_quarter"` 或無 `section_filter` → `base = max(5, min(10, n_quarters + 2))`（`n_quarters` 為使用者選的季度數，未指定時假設 6）
- `section_filter == "guidance"` → `base = 4`
- `tool_hint == "coverage_fill"` → `base = 8`
- `iteration > 0`（retry 輪）→ `base = min(12, base + 2)`

#### Scenario: guidance 子查詢用較小 top_k
- **GIVEN** 某 sub_query `section_filter == "guidance"`、`iteration == 0`
- **WHEN** `parallel_retrieval` 計算該子查詢的 top_k
- **THEN** top_k 為 `4`

#### Scenario: coverage_fill 子查詢用較大 top_k
- **GIVEN** 某 sub_query `tool_hint == "coverage_fill"`、`iteration == 0`
- **WHEN** `parallel_retrieval` 計算 top_k
- **THEN** top_k 為 `8`

#### Scenario: retry 輪擴大 top_k
- **GIVEN** 某 guidance 子查詢、`iteration == 1`
- **WHEN** `parallel_retrieval` 計算 top_k
- **THEN** top_k 為 `min(12, 4 + 2) == 6`

### Requirement: `target_quarter` MUST 覆蓋使用者季度過濾

當某 bigquery 子查詢含 `target_quarter`（self_reflect 標記的弱季專屬查詢），`parallel_retrieval` MUST 把 `retrieve()` 的 `quarters` 參數設為 `[target_quarter]`，覆蓋使用者層的 `quarters_filter`。

#### Scenario: 弱季專屬查詢改寫季度範圍
- **GIVEN** `state["quarters"] == ["2024Q1", "2024Q2", "2024Q3"]`、某 sub_query 含 `target_quarter == "2024Q2"`
- **WHEN** `parallel_retrieval` 對該子查詢呼叫 `retrieve()`
- **THEN** 傳給 `retrieve()` 的 `quarters` 為 `["2024Q2"]`

### Requirement: 單一工具失敗 MUST 不影響其他工具

`parallel_retrieval` MUST 對每個 future 的結果獨立 try/except；任一工具 raise 時 MUST 捕獲，在 `steps_log` 記錄失敗（只記 `type(e).__name__`，不洩漏 API key 片段），並繼續處理其他工具的結果。

#### Scenario: Tavily 失敗不影響 BigQuery
- **GIVEN** `search_news` raise、bigquery 檢索成功
- **WHEN** `parallel_retrieval(state)` 被呼叫
- **THEN** 回傳的 `retrieved` 仍含 bigquery 結果
- **AND** `steps_log` 含一則含 `查詢失敗` 的訊息（不含金鑰片段）

### Requirement: retry 輪 SHALL 以前輪結果為基底，新結果去重合併

`parallel_retrieval` MUST 在 retry 輪（`iteration > 0`）以 `state` 既有的 `retrieved` / `news_context` / `stock_data` 為基底（copy），新檢索結果合併進去：
- BigQuery chunks 依 `chunk.id` 去重後 append（避免重複追加相同 chunk）
- 工具成功則覆寫對應結果，失敗時保留舊值

#### Scenario: retry 不覆蓋前輪已取得的季度
- **GIVEN** `state["retrieved"]` 已含 `2024Q1` 的 chunks、`iteration == 1`
- **WHEN** `parallel_retrieval(state)` 被呼叫
- **THEN** 回傳的 `retrieved` 仍含原 `2024Q1` chunks

#### Scenario: 相同 chunk id 不重複追加
- **GIVEN** 新一輪檢索回傳的某 chunk `id` 已存在於該季度
- **WHEN** `parallel_retrieval` 合併結果
- **THEN** 該 chunk 不被重複加入

### Requirement: 季度覆蓋補洞 SHALL 僅在未指定季度時觸發

`parallel_retrieval` MUST 在 `quarters_filter` 為 `None`（使用者選「全部」）且 `retrieved` 非空時，呼叫 `get_company_quarters()` 找出公司所有季度，對遺漏的季度呼叫 `retrieve_coverage()` 補抓（`top_k_per_quarter=2`）。使用者指定特定季度時 MUST 不觸發補洞。

#### Scenario: 未指定季度時補洞
- **GIVEN** `state["quarters"]` 為空、初步檢索取得 `{2024Q1, 2024Q3}`、公司實際有 `{2024Q1, 2024Q2, 2024Q3}`
- **WHEN** `parallel_retrieval(state)` 被呼叫
- **THEN** `retrieve_coverage()` 被呼叫，`missing_quarters` 含 `2024Q2`

#### Scenario: 指定季度時不補洞
- **GIVEN** `state["quarters"] == ["2024Q1"]`
- **WHEN** `parallel_retrieval(state)` 被呼叫
- **THEN** `retrieve_coverage()` 不被呼叫

### Requirement: 無檢索結果 SHALL 記錄警告

`parallel_retrieval` MUST 在最終 `retrieved` 為空 dict 時，於 `steps_log` 加入一則提示（含「知識庫無結果」字樣），不 raise。

#### Scenario: 完全無結果時記錄提示
- **GIVEN** 所有 bigquery 子查詢回傳空、無其他工具
- **WHEN** `parallel_retrieval(state)` 被呼叫
- **THEN** `steps_log` 含含「知識庫無結果」字樣的訊息
- **AND** 回傳的 `retrieved` 為空 dict
