# ui-quarters Specification

## Purpose

定義 `src/ui/quarters.py` 對 BigQuery 上「實際已匯入季度」的動態讀取：依公司過濾、`@st.cache_data(ttl=300)` 五分鐘快取、BQ 失敗時的 `_FALLBACK_QUARTERS` 兜底、多公司聯集語意。本 spec 確保 sidebar 下拉只列出該公司真有資料的季度，避免使用者選到 0 chunk 的組合。

## Requirements

### Requirement: `get_available_quarters(company=None)` SHALL 動態查詢並依字典序排序

`get_available_quarters(company)` MUST：
1. `company` 非空時，在 BigQuery 對該公司執行 `SELECT DISTINCT quarter`
2. `company` 為 `None` 時，查所有公司的季度聯集（向後相容）
3. 結果經 `sorted(..., key=lambda x: (x[:4], x[4:]))` 排序確保 `2023Q4 < 2024Q1`
4. 為 Streamlit `@st.cache_data(ttl=300, show_spinner=False)` 快取結果

#### Scenario: 指定公司查單一公司季度
- **GIVEN** BigQuery 該公司含季度 `["2024Q1", "2024Q2"]`
- **WHEN** `get_available_quarters("台積電")` 被呼叫
- **THEN** 回傳 `["2024Q1", "2024Q2"]`（排序）
- **AND** 傳給 BQ 的 SQL 含 `company = @company`

#### Scenario: 跨年季度正確排序
- **GIVEN** BQ 回傳 `["2024Q1", "2023Q4", "2024Q2"]`
- **WHEN** `get_available_quarters(...)` 被呼叫
- **THEN** 回傳 `["2023Q4", "2024Q1", "2024Q2"]`

### Requirement: BigQuery 失敗 SHALL fall back 到 `_FALLBACK_QUARTERS`

`get_available_quarters` MUST 在 BigQuery 連線失敗（任何例外）或回傳空 list 時，回傳 `_FALLBACK_QUARTERS` 的副本（list）；MUST 不 raise，但 MUST print 警告含例外類別名稱。

`_FALLBACK_QUARTERS` MUST 含至少 10 個合理的近期季度（涵蓋 2022Q4 至 2026Q1 範圍）。

#### Scenario: BQ 例外時 fallback
- **GIVEN** `get_bq_client()` raise
- **WHEN** `get_available_quarters("X")` 被呼叫
- **THEN** 回傳 list 等於 `_FALLBACK_QUARTERS`
- **AND** stdout 含 `get_available_quarters 意外失敗` 字樣

#### Scenario: BQ 空結果時 fallback
- **GIVEN** BQ 對該公司回傳 0 列
- **WHEN** `get_available_quarters("X")` 被呼叫
- **THEN** 回傳 `_FALLBACK_QUARTERS`

#### Scenario: Fallback 為獨立副本
- **GIVEN** `get_available_quarters("X")` 觸發 fallback
- **WHEN** 呼叫端修改回傳的 list
- **THEN** `_FALLBACK_QUARTERS` 模組常數不受影響（必須回傳 `list(_FALLBACK_QUARTERS)`）

### Requirement: `get_available_quarters_union(companies)` SHALL 取多公司季度聯集

`get_available_quarters_union(companies: tuple)` MUST：
1. `companies` 為空 tuple 時，回傳 `get_available_quarters(None)` 結果
2. 否則對每家公司各呼叫 `get_available_quarters(c)`、聯集所有結果、排序回傳
3. 參數 MUST 為 tuple（非 list）以支援 `@st.cache_data` hashing

#### Scenario: 空 tuple 走 union (=全公司)
- **WHEN** `get_available_quarters_union(())` 被呼叫
- **THEN** 等同於 `get_available_quarters(None)` 的回傳

#### Scenario: 兩公司各有獨家季度
- **GIVEN** 台積電有 `["2024Q1"]`、聯發科有 `["2024Q2"]`
- **WHEN** `get_available_quarters_union(("台積電", "聯發科"))` 被呼叫
- **THEN** 回傳 `["2024Q1", "2024Q2"]`（聯集排序）

#### Scenario: 多公司部分重疊
- **GIVEN** 台積電 `["2024Q1", "2024Q2"]`、聯發科 `["2024Q2", "2024Q3"]`
- **WHEN** `get_available_quarters_union(("台積電", "聯發科"))` 被呼叫
- **THEN** 回傳 `["2024Q1", "2024Q2", "2024Q3"]`（去重）
