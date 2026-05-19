# multi-company-comparison Specification

## Purpose

定義 `src/core/comparison.py` 的多公司並行分析、跨公司立場對齊、差異摘要產生：`ThreadPoolExecutor` 並行上限、單一公司失敗的隔離、`build_comparison_table` 的「無關 / boilerplate」過濾與季度排序、`synthesize_diff` 的 prompt 截斷與 LLM 失敗降級。本 spec 確保多公司並行不會觸發 LLM rate limit、單公司失敗不影響其餘、UI 顯示的比較表行數合理。

## Requirements

### Requirement: `run_multi_company` SHALL 用 `ThreadPoolExecutor` 並行、上限 2 workers

`run_multi_company(companies, topic, quarters, custom_query="")` MUST：
- 以 `ThreadPoolExecutor(max_workers=min(len(companies), 2))` 並行呼叫 `run_agent()`
- 對每家公司收集結果為 `{company: AgentState}` dict
- 單一公司 raise 時 MUST 捕獲，回傳該公司對應的 fallback dict（含 `error` 為例外類別名稱、`contradictions=[]`、`promises=[]`、`confidence=0.0`、`final_report` 為含 friendly message 的字串）；其他公司不受影響

#### Scenario: 2 家公司啟 2 workers
- **GIVEN** `companies = ["台積電", "聯發科"]`
- **WHEN** `run_multi_company(...)` 被呼叫
- **THEN** `ThreadPoolExecutor` 以 `max_workers=2` 建立

#### Scenario: 3 家公司仍只啟 2 workers（rate limit 保護）
- **GIVEN** `companies = ["台積電", "聯發科", "鴻海"]`
- **WHEN** `run_multi_company(...)` 被呼叫
- **THEN** `ThreadPoolExecutor` 以 `max_workers=2` 建立

#### Scenario: 單公司失敗時其他完成
- **GIVEN** `run_agent("台積電", ...)` raise、`run_agent("聯發科", ...)` 成功
- **WHEN** `run_multi_company(["台積電", "聯發科"], ...)` 被呼叫
- **THEN** 回傳 dict 同時含兩家公司的條目
- **AND** `results["台積電"]["error"]` 為原例外類別名稱
- **AND** `results["台積電"]["final_report"]` 含 `⚠ 台積電 分析失敗` 字樣
- **AND** `results["聯發科"]` 為正常 AgentState

### Requirement: 自訂 query 時 SHALL 蓋過預設模板

`run_multi_company` MUST 在 `custom_query` 非空字串時，將該字串作為 `run_agent` 的 `query` 參數；空字串時用預設模板 `"{company} 在「{topic}」方面，各季度發言是否有矛盾或立場轉變？請追蹤承諾兌現情況。"`

#### Scenario: 空 custom_query 用模板
- **GIVEN** `custom_query=""`
- **WHEN** `run_multi_company(["台積電"], "AI需求", [])` 被呼叫
- **THEN** 傳給 `run_agent` 的 query 含 `台積電 在「AI需求」`

#### Scenario: 非空 custom_query 直接套用
- **WHEN** `run_multi_company(["台積電"], "AI需求", [], custom_query="比較各季毛利率")` 被呼叫
- **THEN** 傳給 `run_agent` 的 query 為 `"比較各季毛利率"`

### Requirement: `build_comparison_table` SHALL 過濾「無關」、boilerplate 與全空白列

`build_comparison_table(results)` MUST：
- 對每家公司的 `contradictions`，跳過 `stance_change == "無關"` 的條目
- 跳過 `evidence_early == evidence_later`（boilerplate 重複引文）的條目
- 對剩下的條目以 `quarter_a vs quarter_b` 作為 `pair_key`
- 對所有出現過的 `pair_key` 對齊各公司，缺席格填 `"—"`
- 依季度時間序排序（用 quarter 前 4 字當年份、後段當季）

#### Scenario: 無關條目被過濾
- **GIVEN** 某公司有條目 `{quarter_a: "2024Q1", quarter_b: "2024Q2", analysis: {stance_change: "無關"}}`
- **WHEN** `build_comparison_table({...})` 被呼叫
- **THEN** 回傳列表不含該 `pair_key`

#### Scenario: 引文重複的條目被過濾
- **GIVEN** 條目 `evidence_early == evidence_later == "前瞻性陳述..."` 且非空
- **WHEN** `build_comparison_table({...})` 被呼叫
- **THEN** 該條目不出現在表中

#### Scenario: 缺席季度對顯示破折號
- **GIVEN** 台積電 `2024Q1 vs 2024Q2` 為「更樂觀」、聯發科無此 pair
- **WHEN** `build_comparison_table({...})` 被呼叫
- **THEN** 回傳列含 `{"quarter_pair": "2024Q1 vs 2024Q2", "台積電": "更樂觀", "聯發科": "—"}`

#### Scenario: 列依時間序排列
- **GIVEN** 多個 pair 含 `2023Q4 vs 2024Q1`、`2024Q2 vs 2024Q3`、`2024Q1 vs 2024Q2`
- **WHEN** `build_comparison_table(...)` 被呼叫
- **THEN** 回傳列表前到後為 `2023Q4...`, `2024Q1...`, `2024Q2...`

### Requirement: `synthesize_diff` SHALL 在無分歧時回傳一致性訊息，並對輸入做截斷

`synthesize_diff(comparison_table, topic, companies)` MUST：
1. 比較表為空時回傳 `"兩家公司在此主題上資料不足，無法進行有效比較。"`
2. 對每列計算各公司非 `"—"` 的 stance 集合大小；大小 > 1 的列視為「分歧列」
3. 無分歧列時回傳含「立場走勢高度一致」字樣的訊息
4. 有分歧列時 MUST 取前 8 列、最終 prompt 表格文字截斷至 1500 字元，再呼叫 LLM
5. LLM 失敗 MUST 不 raise，回傳 `"差異摘要生成失敗，請參考上方比較表。"` 並 print 錯誤

#### Scenario: 空表回傳資料不足訊息
- **WHEN** `synthesize_diff([], "AI", ["A", "B"])` 被呼叫
- **THEN** 回傳含 `資料不足` 字樣的字串
- **AND** LLM 不被呼叫

#### Scenario: 全部立場一致時回傳一致性訊息
- **GIVEN** 比較表所有列各公司 stance 相同（如全為「更樂觀」）
- **WHEN** `synthesize_diff(...)` 被呼叫
- **THEN** 回傳含 `立場走勢高度一致` 字樣
- **AND** LLM 不被呼叫

#### Scenario: 有分歧時呼叫 LLM
- **GIVEN** 比較表含至少 1 列各公司 stance 不同
- **WHEN** `synthesize_diff(...)` 被呼叫
- **THEN** LLM 被呼叫一次

#### Scenario: 分歧列超過 8 時只取前 8
- **GIVEN** 10 列皆為分歧列
- **WHEN** `synthesize_diff(...)` 被呼叫
- **THEN** 實際送進 LLM 的 prompt 中 table_text 來自至多 8 列

#### Scenario: LLM 失敗時降級
- **GIVEN** `llm_chat(...)` raise
- **WHEN** `synthesize_diff(...)` 被呼叫
- **THEN** 回傳含 `差異摘要生成失敗` 字樣的字串，不 raise
