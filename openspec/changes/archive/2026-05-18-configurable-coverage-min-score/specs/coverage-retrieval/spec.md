## ADDED Requirements

### Requirement: Coverage sweep min_score 可由環境變數設定

`retrieve_coverage()` 的 `min_score` 門檻 SHALL 在未由呼叫端顯式傳入時，從環境變數 `COVERAGE_MIN_SCORE` 讀取。當該環境變數不存在或格式非法時，系統 MUST fallback 至預設值 `0.25`，且 MUST 不 raise exception。

#### Scenario: 環境變數未設定時使用預設值
- **WHEN** `os.environ` 中無 `COVERAGE_MIN_SCORE`
- **AND** 呼叫端呼叫 `retrieve_coverage(...)` 而未傳入 `min_score` 參數
- **THEN** 實際使用的 `min_score` 等於 `0.25`

#### Scenario: 環境變數為合法 float 時生效
- **WHEN** `COVERAGE_MIN_SCORE=0.35` 已設定
- **AND** 呼叫端呼叫 `retrieve_coverage(...)` 而未傳入 `min_score` 參數
- **THEN** 實際使用的 `min_score` 等於 `0.35`
- **AND** BigQuery `max_distance` 參數對應地計算為 `1.0 - 0.35 = 0.65`

#### Scenario: 環境變數為非數字時 fallback 並印警告
- **WHEN** `COVERAGE_MIN_SCORE=abc` 已設定
- **AND** 呼叫端呼叫 `retrieve_coverage(...)`
- **THEN** 系統 SHALL print 一則包含 `COVERAGE_MIN_SCORE` 與 `0.25` 字樣的警告訊息
- **AND** 實際使用的 `min_score` 等於 `0.25`
- **AND** 函數正常完成，不 raise

#### Scenario: 環境變數超出合理範圍時 fallback
- **WHEN** `COVERAGE_MIN_SCORE=1.5`（或 `-0.1`、`2.0` 等不在 `[0.0, 1.0]` 區間的值）
- **AND** 呼叫端呼叫 `retrieve_coverage(...)`
- **THEN** 系統 SHALL print 警告
- **AND** 實際使用的 `min_score` 等於 `0.25`

### Requirement: 呼叫端顯式參數優先於環境變數

`retrieve_coverage()` MUST 在呼叫端顯式傳入 `min_score` 時，無視環境變數，採用該顯式值。

#### Scenario: 顯式參數覆寫環境變數
- **WHEN** `COVERAGE_MIN_SCORE=0.50` 已設定
- **AND** 呼叫端呼叫 `retrieve_coverage(..., min_score=0.10)`
- **THEN** 實際使用的 `min_score` 等於 `0.10`

### Requirement: 預設值單一來源

模組 MUST 將預設值 `0.25` 定義為單一常數（`_DEFAULT_MIN_SCORE`），函數預設行為與環境變數 fallback 路徑皆參照此常數。

#### Scenario: 預設值修改集中
- **WHEN** 開發者將 `_DEFAULT_MIN_SCORE` 改為其他值
- **THEN** 「環境變數未設」與「環境變數非法」兩條路徑皆使用該新值，無需多處修改
