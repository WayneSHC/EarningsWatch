## 1. Implementation

- [x] 1.1 在 `src/core/retriever.py` 模組頂層新增常數 `_DEFAULT_MIN_SCORE = 0.25`
- [x] 1.2 在 `src/core/retriever.py` 新增私有 helper `_load_min_score_from_env() -> float`：讀 `os.getenv("COVERAGE_MIN_SCORE")`、嘗試 `float()`、檢查 `[0.0, 1.0]` 區間、非法時 `print` 警告並回傳 `_DEFAULT_MIN_SCORE`
- [x] 1.3 將 `retrieve_coverage()` 簽名 `min_score: float = 0.25` 改為 `min_score: float | None = None`，函數開頭加 `if min_score is None: min_score = _load_min_score_from_env()`
- [x] 1.4 確認其他內部變數（如 `max_distance = 1.0 - min_score`、`print(f"...分數不足 {min_score}...")`）邏輯依然正確

## 2. Config & Docs

- [x] 2.1 `.env.example` 新增 `COVERAGE_MIN_SCORE=0.25` 與一行註解（含建議範圍 0.2–0.35）
- [x] 2.2 更新 `CLAUDE.md` 「Coverage Sweep」段落，註明門檻可由 `COVERAGE_MIN_SCORE` 設定
- [x] 2.3 更新 `CLAUDE.md` 「Environment Variables」段落，把 `COVERAGE_MIN_SCORE` 列入 Optional 區塊

## 3. Tests

- [x] 3.1 在 `tests/test_retriever.py` 新增測試：未設環境變數時 `_load_min_score_from_env()` 回傳 0.25
- [x] 3.2 新增測試：`COVERAGE_MIN_SCORE=0.35`（用 `monkeypatch.setenv`）→ 回傳 0.35
- [x] 3.3 新增測試：`COVERAGE_MIN_SCORE=abc` → 印警告（用 `capsys`）+ 回傳 0.25
- [x] 3.4 新增測試：`COVERAGE_MIN_SCORE=1.5`、`-0.1` → fallback 0.25
- [x] 3.5 新增測試：呼叫 `retrieve_coverage(..., min_score=0.10)` 顯式傳入時無視環境變數（mock 掉 BigQuery 部分，只驗證參數傳遞）

## 4. Verification

- [x] 4.1 本地跑 `pytest tests/test_retriever.py -v` 全綠（16/16）
- [x] 4.2 本地跑 `pytest tests/ -v` 全綠（156/156，無 regression）
- [x] 4.3 `python -m py_compile src/core/retriever.py` 通過
- [x] 4.4 手動 smoke test：unset → 0.25 / `=0.35` → 0.35 / `=banana` → 警告 + 0.25

## 5. Wrap-up

- [x] 5.1 `git diff` 自我 review，確認無多餘改動（4 檔，+171/-4，僅 OpenSpec 目錄為新增 untracked）
- [x] 5.2 `openspec validate configurable-coverage-min-score` 通過
- [ ] 5.3 commit（含 OpenSpec change 目錄）— 等使用者指示
- [ ] 5.4 `/opsx:archive` 將本 change 歸檔到 `openspec/changes/archive/` — 等使用者指示
