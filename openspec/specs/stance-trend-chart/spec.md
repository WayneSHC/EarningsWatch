# stance-trend-chart Specification

## Purpose

定義 `src/ui/chart.py` 對矛盾偵測結果 → Plotly 時間序列的轉換邏輯：每季只取最顯著立場、保留無關 / boilerplate 季度為佔位點、累積分數計算、多公司同軸對比、捲動容器 HTML 包裝。本 spec 確保趨勢圖的 X 軸時間完整、立場優先級正確、累積邏輯只算有方向性的 delta。

## Requirements

### Requirement: `build_stance_series` SHALL 以 `quarter_b` 為主鍵去重

`build_stance_series(contradictions)` MUST 對每個 contradiction 條目以 `analysis.quarter_b` 為主鍵；缺 `quarter_b` 的條目 MUST 跳過。同一 `quarter_b` 有多個比對時 MUST 保留 `abs(delta)` 最大者（即立場轉變最顯著的）。

#### Scenario: 缺 quarter_b 被跳過
- **GIVEN** contradiction `{"quarter_a": "2024Q1", "quarter_b": ""}`
- **WHEN** `build_stance_series([item])` 被呼叫
- **THEN** 回傳 `[]`

#### Scenario: 同季多比對取最顯著
- **GIVEN** 兩個比對都對應 `quarter_b == "2024Q3"`：A 為 `"維持不變"`、B 為 `"更樂觀"`
- **WHEN** `build_stance_series([A, B])` 被呼叫
- **THEN** 回傳 1 個 entry、`stance == "更樂觀"`（abs(delta)=1 > 0）

### Requirement: 無關 / boilerplate 季度 SHALL 以 `delta=0` 佔位

`build_stance_series` MUST 在下列情境記錄 `quarter_b` 的佔位 entry（`delta=0`、`stance="無關"`、`is_relevant=False`）：
1. `stance_change` 不在 `STANCE_SCORE` 字典中（含 `"無關"` 或意外字串）
2. `evidence_early` 與 `evidence_later` 經 strip 後為非空且完全相同（boilerplate 重複引文）

該季 MUST 仍出現在最終 series（保留時間軸完整性），但 `is_relevant` 必為 `False`。

#### Scenario: 「無關」立場佔位
- **GIVEN** contradiction `{"quarter_b": "2024Q3", "analysis": {"stance_change": "無關"}}`
- **WHEN** `build_stance_series([...])` 被呼叫
- **THEN** 回傳含一個 entry，`quarter == "2024Q3"`、`delta == 0`、`stance == "無關"`、`is_relevant == False`

#### Scenario: Boilerplate 引文佔位
- **GIVEN** 條目的 `evidence_early == evidence_later == "前瞻性陳述..."` 且非空
- **WHEN** `build_stance_series(...)` 被呼叫
- **THEN** 該 `quarter_b` 為佔位 entry（`delta=0`、`is_relevant=False`）

### Requirement: 立場 SHALL 對映固定 delta 值

`STANCE_SCORE` 模組級 dict MUST 包含：
- `"更樂觀"` → `+1`
- `"維持不變"` → `0`
- `"更保守"` → `-1`

任何其他字串（含 `"無關"`）MUST 被視為佔位（走上述 `delta=0` 佔位邏輯）。

#### Scenario: STANCE_SCORE 鎖定值
- **WHEN** 讀取 `chart.STANCE_SCORE`
- **THEN** dict 含 `"更樂觀" -> 1`、`"維持不變" -> 0`、`"更保守" -> -1`

### Requirement: `cumulative` SHALL 累加所有 delta（含 0）

`build_stance_series` MUST 依季度排序後對每個 entry 計算 `cumulative = sum(deltas to date)`。`delta == 0`（無關 / 維持不變）參與累加但不影響值。

#### Scenario: 三季累積
- **GIVEN** 三個 entry 依序為 `delta = [+1, 0, +1]`
- **WHEN** `build_stance_series(...)` 完成
- **THEN** cumulative 依序為 `[1, 1, 2]`

#### Scenario: 包含負 delta
- **GIVEN** 四季 delta `[+1, -1, 0, +1]`
- **WHEN** `build_stance_series(...)` 完成
- **THEN** cumulative `[1, 0, 0, 1]`

### Requirement: Series SHALL 依季度字典序遞增排序

`build_stance_series` MUST 用 `_quarter_sort_key(q) = (q[:4], q[4:])`（年, 季）排序輸出 series，確保 `2023Q4 < 2024Q1`。

#### Scenario: 跨年遞增
- **GIVEN** entries 含 `2024Q1` 與 `2023Q4`
- **WHEN** `build_stance_series(...)` 完成
- **THEN** `2023Q4` 在 `2024Q1` 之前

### Requirement: `render_trend_chart` SHALL 處理空 series 與 mode 切換

`render_trend_chart(series_by_company, topic, mode)` MUST：
- 在所有公司的 series 合併為空時，加入「此主題無立場變化資料」annotation 並回傳 Figure（不 raise）
- `mode="cumulative"` MUST 為實線折線（`mode="lines+markers"`），且把 `is_relevant=False` 的點分離到「主題未提及」trace（空心圓、`showlegend=False`）
- `mode="delta"` MUST 為長條圖、每根 bar 用 `STANCE_COLOR[stance]` 上色（無關 / boilerplate 用淺灰 `#dfe6e9`）

#### Scenario: 空資料時顯示 annotation
- **WHEN** `render_trend_chart({"A": []}, "AI", "cumulative")` 被呼叫
- **THEN** 回傳的 Figure 含至少一個 annotation 且 text 含 `無立場變化資料`
- **AND** 不 raise

#### Scenario: cumulative 模式分離 relevant / irrelevant
- **GIVEN** series 含 2 個 `is_relevant=True` + 1 個 `is_relevant=False`
- **WHEN** `render_trend_chart({"A": series}, "AI", "cumulative")` 被呼叫
- **THEN** Figure 含 2 條 trace（一條主線、一條無關點 trace）

### Requirement: `chart_to_scrollable_html` SHALL 包水平捲動 div

`chart_to_scrollable_html(fig)` MUST 回傳一段 HTML 字串，其外層為 `<div style="overflow-x:auto;overflow-y:hidden;...">{plotly_html}</div>`，使長 X 軸圖表在窄容器中可水平捲動。內部 Plotly HTML 用 `include_plotlyjs="cdn"`（減小 payload）。

#### Scenario: 包裹 div 結構
- **WHEN** `chart_to_scrollable_html(fig)` 被呼叫
- **THEN** 回傳字串以 `<div style="overflow-x:auto` 起始
- **AND** 結尾為 `</div>`
- **AND** 內含 Plotly CDN script 引用（`plotly` 字樣）
