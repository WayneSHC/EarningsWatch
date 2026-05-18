## Context

`retrieve_coverage()` 是 LangGraph 7 節點中 `retrieve` 節點的後置補洞機制：當初步 top-k 結果遺漏某些季度時，會以 `min_score=0.25` 為門檻補抓。這個門檻是 quality/recall trade-off 的關鍵旋鈕，但目前寫死在函數簽名裡，每次調整都要改 code、重 deploy、重跑 benchmark。

對單人專案而言，這阻礙了「資料品質實驗」的快速迭代節奏（CLAUDE.md `coverage sweep 門檻` 一節已明文標示這是效能 / 品質權衡的關鍵）。

## Goals / Non-Goals

**Goals:**
- `min_score` 可從 `.env` 設定，部署時無需改 code
- 預設行為與目前完全一致（向後相容）
- 非法值（非 float / 超出 `[0.0, 1.0]`）不能讓服務崩潰
- 顯式傳入的 caller 參數仍優先於環境變數（測試 / 特殊呼叫場景）

**Non-Goals:**
- 不引入 admin UI / 動態熱更新（重啟即可生效，足夠）
- 不擴展為「多套 profile 切換」機制（YAGNI）
- 不改 `max_quarters`、`top_k_per_quarter` 等其他參數（只做 min_score 一個，避免 scope creep）

## Decisions

### D1：環境變數命名 `COVERAGE_MIN_SCORE`
**選用理由**：與既有 `LLM_*`、`QDRANT_*` 等 prefix 命名風格一致（功能群組為 prefix）。

**替代方案**：`RETRIEVAL_COVERAGE_MIN_SCORE` — 更精確但過長；`MIN_SCORE` — 太泛、與其他 retriever 函數可能衝突。

### D2：讀取時機在函數預設值，而非模組頂層常數
```python
def retrieve_coverage(
    ...,
    min_score: float | None = None,
    ...,
):
    if min_score is None:
        min_score = _load_min_score_from_env()
```

**選用理由**：呼叫端可顯式覆寫（測試友善）；測試 monkeypatch `os.environ` 後重新呼叫立即生效，無需重 import。

**替代方案**：模組載入時讀一次成常數 — 性能更好但無法 monkeypatch，測試麻煩。`min_score` 路徑每 query 只跑一次，效能差距可忽略。

### D3：非法值 fallback 而非 raise
- 非數字、超出 `[0.0, 1.0]` → 印 warning，回傳預設 `0.25`
- 不擋啟動，避免 prod 因 typo 整個服務掛掉

**理由**：retrieval 是核心讀路徑，比起拒絕服務，給安全預設更穩健。warning 由 ops 在 log 看到後修。

### D4：預設值集中常數 `_DEFAULT_MIN_SCORE = 0.25`
模組頂層定義一個常數，函數簽名與 env loader 都引用它。避免「兩個 0.25 各自漂移」的維護風險。

## Risks / Trade-offs

- **[Risk] 環境變數打錯字（如 `COVERAGE_MIN_SOCRE`）→ 靜默用預設值** → Mitigation：啟動時若 env 中存在 `COVERAGE_MIN_*` 但不是 `COVERAGE_MIN_SCORE`，印 hint。簡單作法：unit test 覆蓋拼錯場景。
- **[Risk] 使用者調太高（如 0.9）→ coverage sweep 幾乎不補任何季度，回退到單純 top-k** → Mitigation：在 `.env.example` 註解標明「實測 0.2–0.35 為甜蜜點」。
- **[Risk] 使用者調太低（如 0.05）→ 補入大量低相關 chunk，LLM token cost 暴增** → Mitigation：同上，文件警告；長期可加 metric 監控 coverage chunk 數。
- **[Trade-off] 每次呼叫讀一次 env**（雖然 `os.getenv` 是 dict 查詢，~ns 級）對熱路徑影響可忽略；換到模組常數會省這點開銷但失去測試彈性。選測試彈性。

## Migration Plan

無 schema / DB / API 變動，純 in-process 設定。

- **Deploy**：merge → 既有 `.env` 沒有該變數 → 自動用預設 `0.25` → 行為不變
- **啟用**：要 tune 時，在 `.env` 加一行 `COVERAGE_MIN_SCORE=0.30` → 重啟服務生效
- **Rollback**：移除 `.env` 那行即可；不需要 revert code
