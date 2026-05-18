## Why

Coverage sweep 的 `min_score` 門檻目前硬編碼為 `0.25`（`src/core/retriever.py:213`），調整時必須改程式碼並重新部署。將其抽到 `.env` 後，可在不同公司 / 主題 / 資料品質情境下快速 tune（例如資料雜訊高時調高、recall 不足時調低），降低實驗成本。

## What Changes

- 新增環境變數 `COVERAGE_MIN_SCORE`（float，預設 `0.25`）
- `retrieve_coverage()` 預設參數改為從環境變數讀取；顯式傳入的呼叫端參數仍優先（向後相容）
- `.env.example` 新增該變數與註解
- 非法值（非 float、超出 `[0.0, 1.0]`）→ fallback 至預設 `0.25` 並印警告，不 raise
- 不影響任何現有 API / 函數簽名 / 預設行為

## Capabilities

### New Capabilities
- `coverage-retrieval`: BigQuery vector-search 的 coverage sweep 機制，含 `min_score` 門檻、`max_quarters` 截斷、top-k 設定的可配置行為

### Modified Capabilities
<!-- 無 -->

## Impact

- **Code**: `src/core/retriever.py`（讀環境變數 + 預設值處理）、`.env.example`（新欄位文件）
- **Tests**: `tests/test_retriever.py` 增 1-2 個單元測試覆蓋 env 讀取與非法值 fallback
- **Docs**: `CLAUDE.md` 的「Coverage Sweep」與「Environment Variables」兩節須補一行
- **Deps**: 無新增
- **Runtime**: 無；預設值不變，現有部署無感升級
