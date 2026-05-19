# ragas-evaluation Specification

## Purpose

定義 `src/core/ragas_eval.py` 對 RAGAS 評估的封裝：可選依賴的偵測、缺套件 / 缺 key / 缺 context 時的 graceful degradation、選擇性 metric、`ground_truth` 觸發 `context_recall`、per-query 結果的彙整、AgentState → contexts 的萃取。本 spec 確保 RAGAS 不影響主流程（任何失敗回傳 `{}` 而非 raise），且 benchmark 在缺依賴的環境下仍能執行。

## Requirements

### Requirement: `is_available()` SHALL 同時要求 `ragas` 與 `langchain_openai`

`is_available()` MUST 用 try/except ImportError 確認下列兩個套件皆可 import：
1. `ragas`
2. `langchain_openai`

任一缺失 MUST 回傳 `False`，且 MUST 不 raise。

#### Scenario: 兩者都安裝時為 True
- **GIVEN** 環境含 ragas 與 langchain_openai
- **WHEN** `is_available()` 被呼叫
- **THEN** 回傳 `True`

#### Scenario: 缺套件回 False
- **GIVEN** `langchain_openai` 無法 import
- **WHEN** `is_available()` 被呼叫
- **THEN** 回傳 `False`

### Requirement: `evaluate_query` SHALL 在四種短路情境回傳空 dict

`evaluate_query(question, answer, contexts, ground_truth, metrics)` MUST 在下列任一情境立刻回傳 `{}` 並 MUST 不 raise：
1. `is_available()` 為 `False`
2. `contexts` 為空 list
3. `OPENAI_API_KEY` 環境變數為空 / 全空白
4. RAGAS 內部 raise（API 失敗、解析失敗等）— catch 並 print 警告

#### Scenario: 套件缺席短路
- **GIVEN** `is_available()` 回傳 `False`
- **WHEN** `evaluate_query("q", "a", ["ctx"])` 被呼叫
- **THEN** 回傳 `{}`

#### Scenario: 空 contexts 短路
- **GIVEN** `is_available()` 為 `True`
- **WHEN** `evaluate_query("q", "a", [])` 被呼叫
- **THEN** 回傳 `{}`

#### Scenario: 缺 OPENAI_API_KEY 短路
- **GIVEN** `is_available()` 為 `True`、`contexts=["x"]`、`OPENAI_API_KEY` 為空
- **WHEN** `evaluate_query("q", "a", ["x"])` 被呼叫
- **THEN** 回傳 `{}`
- **AND** stdout 含 `找不到 OPENAI_API_KEY` 字樣

#### Scenario: RAGAS 內部失敗 graceful
- **GIVEN** 所有前置條件滿足，但 `ragas.evaluate(...)` raise
- **WHEN** `evaluate_query(...)` 被呼叫
- **THEN** 回傳 `{}`，不 raise
- **AND** stdout 含 `評估失敗` 字樣

### Requirement: `ground_truth` 提供時 SHALL 嘗試加入 `context_recall` metric

`evaluate_query` MUST 在 `ground_truth` 非 `None` 時，嘗試 import `context_recall` 並加入 metrics 清單；該 import 失敗時 MUST 不 raise，僅跳過 `context_recall`，其餘 metric 照常執行。

#### Scenario: ground_truth 觸發 context_recall
- **GIVEN** `ground_truth="標準答案"`、`context_recall` 可 import
- **WHEN** `evaluate_query(...)` 被呼叫
- **THEN** RAGAS dataset 含 `ground_truth` 欄位
- **AND** `context_recall` 在 selected metrics 中

#### Scenario: context_recall import 失敗時其他 metric 照常
- **GIVEN** `ground_truth="..."`、但 `context_recall` import raise
- **WHEN** `evaluate_query(...)` 被呼叫
- **THEN** 不 raise，仍嘗試其他 metric

### Requirement: `aggregate(per_query_scores)` SHALL 取平均並 round 至 4 位小數

`aggregate(per_query_scores)` MUST：
- 空輸入回傳 `{}`
- 對每個 metric 計算所有 per-query dict 中該 metric 的平均（缺項不影響其他 metric 的 count）
- 回傳值 `round(_, 4)`

#### Scenario: 空輸入
- **WHEN** `aggregate([])` 被呼叫
- **THEN** 回傳 `{}`

#### Scenario: 兩筆求平均
- **GIVEN** `[{"faithfulness": 0.8}, {"faithfulness": 0.6}]`
- **WHEN** `aggregate(...)` 被呼叫
- **THEN** 回傳 `{"faithfulness": 0.7}`

#### Scenario: 部分缺項只計平均存在的
- **GIVEN** `[{"f": 0.8, "r": 0.9}, {"f": 0.4}]`
- **WHEN** `aggregate(...)` 被呼叫
- **THEN** 回傳 `{"f": 0.6, "r": 0.9}`

#### Scenario: 結果 round 至 4 位
- **GIVEN** `[{"x": 0.123456789}]`
- **WHEN** `aggregate(...)` 被呼叫
- **THEN** 回傳值 `0.1235`

### Requirement: `state_to_contexts(state)` SHALL 攤平 retrieved chunks

`state_to_contexts(state)` MUST 從 `state["retrieved"]`（`{quarter: [chunks]}` dict）攤平所有 chunk 的 `content` 字串為單一 list。chunk 非 dict 或 `content` 為空 MUST 跳過。`retrieved` 為 `None` 或不存在 MUST 回傳 `[]`。

#### Scenario: 空 state
- **WHEN** `state_to_contexts({})` 被呼叫
- **THEN** 回傳 `[]`

#### Scenario: 攤平多季
- **GIVEN** `state["retrieved"] = {"Q1": [{"content": "a"}, {"content": "b"}], "Q2": [{"content": "c"}]}`
- **WHEN** `state_to_contexts(state)` 被呼叫
- **THEN** 回傳 `["a", "b", "c"]`（順序依 dict 迭代）

#### Scenario: 跳過空 content
- **GIVEN** chunks 含 `{"content": ""}` 與 `{"content": "x"}`
- **WHEN** `state_to_contexts(state)` 被呼叫
- **THEN** 回傳僅含 `"x"`

#### Scenario: 跳過非 dict
- **GIVEN** chunks 含字串、None 等非 dict 元素
- **WHEN** `state_to_contexts(state)` 被呼叫
- **THEN** 不 raise，跳過該元素
