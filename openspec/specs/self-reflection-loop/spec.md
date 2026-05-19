# self-reflection-loop Specification

## Purpose

定義 `self_reflect` 節點與條件邊 `should_continue` 的行為：LLM-as-judge 評分、硬性 floor 檢查、gap-driven 與 coverage-driven 兩路重查計畫、`target_quarter` 機制、棄權（abstain）門檻、cost guard 與基線隔離。本 spec 確保 Agent 在資料不足時能精準補強（topic gap + quarter gap 雙軸），同時不讓 retry 迴圈造成費用失控。

## Requirements

### Requirement: LLM judge 評分 MUST 與硬性 floor 檢查共同決定 `confidence`

`self_reflect`（`src/agent/nodes.py`）MUST 同時執行：
- **LLM judge**：呼叫 `_llm()` 取得 `{score, issues, gaps, should_retry}`；解析失敗時保留預設 `score=0.6`
- **硬性 floor**：`_hard_floor_checks(retrieved, contradictions)` 對下列條件扣分並產生 issue：
  - 只有 1 季資料 → `penalty += 0.4`，issue: `只找到 1 季資料，需要至少 2 季才能跨季比對`
  - 總 chunk 數 < 3 → `penalty += 0.2`，issue: `檢索到的相關段落過少（{n} 個）`
  - 矛盾偵測平均 confidence < 0.5 → `penalty += 0.2`

最終 `confidence` MUST 等於 `max(0.0, llm_score - total_penalty)`，並截斷至 `[0.0, 1.0]`，最後 `round(score, 2)`。

#### Scenario: 單季資料時即使 LLM 給高分也被壓低
- **GIVEN** `retrieved` 只有一個季度
- **AND** LLM judge 回傳 `score = 1.0`
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** `confidence == round(max(0.0, 1.0 - 0.4), 2) == 0.6`
- **AND** `reflection_issues` 含「只找到 1 季資料」訊息

#### Scenario: LLM 解析失敗時走預設 0.6 + floor
- **GIVEN** LLM 回傳無法解析的字串
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 不 raise
- **AND** `confidence` 由預設 `0.6` 扣除硬性 floor penalty 後得出
- **AND** `steps_log` 含「LLM judge 失敗」字樣

### Requirement: `coverage_matrix` MUST 為每季產生品質指標

`self_reflect` MUST 為 `retrieved` 中的每個季度建立 entry，包含：
- `chunk_count`: 該季 chunk 數
- `max_score` / `avg_score`: 數字型 `score` 的最大值與平均（無分數時為 `0.0`）
- `source_pages`: 去重排序後的前 5 個 `source_page`
- `quote_verified`: 該季在 `contradictions` 中無 `analysis.verification_failed == True` 時為 `True`
- `top_excerpt`: 該季首個 chunk 的 `content`（截至 80 字元）

`coverage_matrix` MUST 被回寫至 state。

#### Scenario: 引文驗證失敗時 quote_verified 為 False
- **GIVEN** `contradictions` 中某 pair 的 `quarter_a == "2024Q2"` 且 `analysis.verification_failed == True`
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** `coverage_matrix["2024Q2"]["quote_verified"] == False`

### Requirement: 弱季 SHALL 被定義為三條件任一成立

弱季 MUST 滿足下列任一條件即視為「需補強」：
1. `chunk_count < 2`
2. `max_score < 0.4`
3. `quote_verified == False`

#### Scenario: chunk_count < 2 視為弱季
- **GIVEN** 某季只有 1 個 chunk 且 score=0.85
- **WHEN** `self_reflect(state)` 計算弱季
- **THEN** 該季被識別為弱季

#### Scenario: max_score < 0.4 視為弱季
- **GIVEN** 某季有 3 個 chunk，分數均低於 0.4
- **WHEN** `self_reflect(state)` 計算弱季
- **THEN** 該季被識別為弱季

#### Scenario: 強季不被識別為弱季
- **GIVEN** 某季 chunk_count=2、max_score=0.85、quote_verified=True
- **WHEN** `self_reflect(state)` 計算弱季
- **THEN** 該季不被識別為弱季

### Requirement: Retry 觸發條件 MUST 結合 LLM 與迭代上限

`self_reflect` MUST 在以下「全部成立」時觸發 retry（`do_retry = True`）：
1. `confidence < 0.75` OR LLM `should_retry == True`
2. `iteration < 3`

否則 `do_retry = False`。觸發後 MUST 將 `iteration` +1 並寫回 state。

#### Scenario: 信心 0.6、iteration 0 → retry
- **GIVEN** LLM 回傳 `score=0.6`、`should_retry=false`、`iteration=0`
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** `do_retry` 為 true，回傳 `iteration == 1`

#### Scenario: 信心 0.85、iteration 0 → 不 retry
- **GIVEN** 信心 0.85、無資料缺漏
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 不重建 sub_queries，回傳 `iteration == 1`

#### Scenario: iteration 已達 3 即使信心低也不 retry
- **GIVEN** 信心 0.3、iteration=3
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** `do_retry` 為 false

### Requirement: 觸發 retry 時 MUST 同時嘗試 gap-driven 與 coverage-driven 重建

當 `do_retry == True`，`self_reflect` MUST 嘗試**並列**產生兩種 sub_queries：

1. **Gap-driven**：對 LLM `gaps` 中的每個主題產生 1 條 sub_query：
   - `id`: `gap_{i}`
   - `query`: `"{company} {gap}"`
   - `purpose`: `"補強：{gap[:20]}"`
   - `tool`: 若 `gap` 含 `新聞 / 市場 / 最新 / 外部 / 競爭 / 產業 / 未來 / 展望 / 預期 / 預測 / 前景` 中任一詞 → `"tavily"`；否則 `"bigquery"`
   - `tool_hint`: `"gap_fill"`

2. **Coverage-driven**：對弱季（上限 3 個）產生 1 條 sub_query：
   - `id`: `weak_{quarter}_{i}`
   - `query`: `"{company} {quarter} {topic} 發言重點"`
   - `purpose`: `"補強弱季 {quarter}"`
   - `tool`: `"bigquery"`
   - `tool_hint`: `"coverage_fill"`
   - `target_quarter`: 該弱季的識別字串

若兩者皆未產出（既無 gaps 又無弱季），MUST 保留原 `sub_queries`（避免引入雜訊）。

#### Scenario: 弱季 sub_query 帶 target_quarter
- **GIVEN** 2024Q2 為弱季，LLM `gaps == []`
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 新 sub_queries 含一條 `tool_hint == "coverage_fill"`、`target_quarter == "2024Q2"` 的條目

#### Scenario: gap 含前瞻關鍵詞時路由至 Tavily
- **GIVEN** LLM 回傳 `gaps == ["AI 產能未來規劃"]`
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 對應 gap_0 的 sub_query `tool == "tavily"`、`tool_hint == "gap_fill"`

#### Scenario: 弱季數量超過 3 時截斷
- **GIVEN** `retrieved` 含 5 個皆符合弱季條件的季度
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 新 sub_queries 中 `tool_hint == "coverage_fill"` 的條目恰好為 3 條

#### Scenario: 無 gaps 且無弱季時沿用原 sub_queries
- **GIVEN** LLM `gaps == []` 且所有季皆為強季
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 回傳 `sub_queries` 為 state 中原本的 `sub_queries`，未被覆蓋

### Requirement: `target_quarter` MUST 在 `parallel_retrieval` 蓋過 `quarters_filter`

`parallel_retrieval`（`src/agent/nodes.py`）MUST 在處理 sub_query 時，若該 sub_query 含 `target_quarter`，將 retriever 的 `quarters` 參數設為 `[target_quarter]`，覆蓋使用者層的 `quarters_filter`，把檢索火力集中到該弱季。

#### Scenario: 弱季專屬 sub_query 改寫季度範圍
- **GIVEN** `state["quarters"] == ["2024Q1", "2024Q2", "2024Q3"]`
- **AND** 某 sub_query 含 `target_quarter == "2024Q2"`
- **WHEN** `parallel_retrieval(state)` 對該 sub_query 執行 `_do_bigquery(sq)` 時
- **THEN** 傳給 `retrieve()` 的 `quarters` 參數為 `["2024Q2"]`

### Requirement: 棄權（abstain）SHALL 在重試耗盡且信心嚴重不足時觸發

當 `do_retry == False` 且 `confidence < 0.4`，`self_reflect` MUST 設定 `abstain = True`，並在 `steps_log` 留下說明訊息。`report_generator` 對 `abstain == True` 的狀態 MUST 輸出「資料不足，無法完成分析」的棄權報告，而非常規偵查報告。

#### Scenario: 三輪後信心 0.3 → 棄權
- **GIVEN** `iteration=3`、`confidence=0.3`
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 回傳 `abstain == True`

#### Scenario: 三輪後信心 0.5 → 不棄權（仍生成報告，但帶低信心提示）
- **GIVEN** `iteration=3`、`confidence=0.5`
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 回傳 `abstain == False`

### Requirement: Cost Guard MUST 使用 baseline 隔離單次 query 的花費

`self_reflect` MUST 在 `do_retry == True` 時檢查：
```
spent = telemetry.summary()["estimated_cost_usd"] - state["cost_baseline_usd"]
```
若 `spent >= LLM_BUDGET_USD`（環境變數 `LLM_BUDGET_USD`，預設 `0.50` USD），MUST 設 `cost_guard_triggered = True` 並在 `steps_log` 記錄。`intent_classifier` 必須在節點入口快照當下累計 cost 作為 `cost_baseline_usd`，使 multi-company 並行查詢的成本不互相干擾。

#### Scenario: 本次 query 已花 0.6 美元 → 觸發
- **GIVEN** `cost_baseline_usd=0.0`、telemetry 累計 0.6 USD
- **WHEN** `self_reflect(state)` 被呼叫（且 retry 被觸發）
- **THEN** 回傳 `cost_guard_triggered == True`

#### Scenario: 兄弟 query 已花 0.45 美元，本次只花 0.10 → 不觸發
- **GIVEN** `cost_baseline_usd=0.45`、telemetry 累計 0.55 USD
- **WHEN** `self_reflect(state)` 被呼叫（且 retry 被觸發）
- **THEN** 回傳 `cost_guard_triggered == False`

#### Scenario: do_retry 為 false 時不檢查 cost guard
- **GIVEN** 信心高、do_retry == false，但 telemetry 累計 10 USD
- **WHEN** `self_reflect(state)` 被呼叫
- **THEN** 回傳 `cost_guard_triggered == False`

### Requirement: `should_continue` 條件邊 SHALL 純粹讀取 state，不 mutate

`should_continue(state)`（`src/agent/nodes.py`）MUST 為純函數，回傳 `"retry"` 或 `"end"`，MUST 不修改 state。判斷邏輯：
1. `cost_guard_triggered == True` → `"end"`（蓋過其他條件）
2. `confidence < 0.75` 且 `iteration < 3` → `"retry"`
3. 否則 → `"end"`

#### Scenario: 同 [agent-orchestration] 已涵蓋
- **GIVEN** 參見 agent-orchestration 規格中對 `should_continue` 的四個情境
- **THEN** 行為一致
