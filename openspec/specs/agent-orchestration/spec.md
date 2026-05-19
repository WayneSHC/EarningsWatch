# agent-orchestration Specification

## Purpose

定義 EarningsWatch 的 LangGraph Agent 拓樸：七節點線性流程加上 `reflect` 條件邊（retry 迴圈），以及共享狀態 `AgentState`、`steps_log` 累加語意、Agent 入口介面 `run_agent()` 的初始狀態形狀。本 spec 確保未來在新增節點 / 改邊 / 重構初始 state 時，現有 UI、benchmark、demo 快取等呼叫端不受影響。

## Requirements

### Requirement: Agent 採用固定的七節點 LangGraph 拓樸

`build_graph()`（`src/agent/graph.py`）SHALL 編譯一個由七個節點組成的 `StateGraph`：`classify` → `decompose` → `route` → `retrieve` → `detect` → `reflect` → `report`。`classify` MUST 為入口節點；節點之間 MUST 為線性 edge，除 `reflect` 之外。

#### Scenario: 編譯成功且暴露七節點

- **WHEN** 呼叫 `build_graph()`
- **THEN** 回傳的編譯後 graph 包含七個節點 `classify` / `decompose` / `route` / `retrieve` / `detect` / `reflect` / `report`
- **AND** 入口節點為 `classify`
- **AND** 不 raise

#### Scenario: 線性邊連接前五個節點

- **WHEN** Graph 結構被檢視
- **THEN** 存在線性 edge：`classify→decompose`、`decompose→route`、`route→retrieve`、`retrieve→detect`、`detect→reflect`
- **AND** `report` 節點連接到 `END`

### Requirement: `reflect` 節點 SHALL 透過條件邊決定 retry 或結束

`build_graph()` MUST 在 `reflect` 節點安裝一條條件 edge，依 `should_continue(state)` 的回傳值跳轉：回傳 `"retry"` → 回到 `retrieve`；回傳 `"end"` → 進入 `report`。

#### Scenario: 信心度高時走 end → report
- **GIVEN** `state` 中 `confidence ≥ 0.75` 且未觸發 cost guard
- **WHEN** `should_continue(state)` 被呼叫
- **THEN** 回傳 `"end"`
- **AND** 條件邊將狀態傳至 `report`

#### Scenario: 信心度低且未達 iteration 上限時走 retry → retrieve
- **GIVEN** `state` 中 `confidence < 0.75`、`iteration < 3`、未觸發 cost guard
- **WHEN** `should_continue(state)` 被呼叫
- **THEN** 回傳 `"retry"`
- **AND** 條件邊將狀態傳回 `retrieve`

#### Scenario: iteration 達上限時強制結束
- **GIVEN** `state` 中 `iteration ≥ 3`，無論 confidence 為何
- **WHEN** `should_continue(state)` 被呼叫
- **THEN** 回傳 `"end"`

#### Scenario: cost_guard_triggered 蓋過 retry 條件
- **GIVEN** `state["cost_guard_triggered"] is True`，即便 `confidence < 0.75` 且 `iteration < 3`
- **WHEN** `should_continue(state)` 被呼叫
- **THEN** 回傳 `"end"`

### Requirement: `run_agent()` 入口 MUST 提供完整初始 AgentState

`run_agent(query, company, topic, quarters)`（`src/agent/graph.py`）MUST 構造一個包含 `AgentState`（`src/agent/state.py`）所有鍵的初始 dict，並以該 dict 呼叫已編譯 graph 的 `.invoke()`。`quarters` 參數為 `None` 時 MUST 預設為空列表。

#### Scenario: 初始 state 涵蓋全部欄位
- **WHEN** `run_agent("q", "台積電", "AI", None)` 被呼叫
- **THEN** 傳給 `agent.invoke(...)` 的 state 字典至少包含以下鍵：
  - `query`, `company`, `topic`, `quarters`
  - `sub_queries`, `tool_plan`
  - `retrieved`, `news_context`, `stock_data`
  - `contradictions`, `promises`
  - `confidence`, `iteration`, `reflection_issues`, `reflection_gaps`
  - `coverage_matrix`, `abstain`
  - `cost_baseline_usd`, `cost_guard_triggered`
  - `final_report`, `steps_log`
- **AND** `confidence` 預設為 `1.0`、`iteration` 預設為 `0`、`abstain` 預設為 `False`、`cost_guard_triggered` 預設為 `False`、`cost_baseline_usd` 預設為 `0.0`

#### Scenario: 未指定 quarters 時降級為空列表
- **WHEN** `run_agent("q", "台積電", "AI", None)` 被呼叫
- **THEN** 傳給 graph 的初始 state 中 `quarters == []`

### Requirement: `steps_log` MUST 使用累加（reducer）語意

`AgentState` 中的 `steps_log` 欄位 MUST 以 `Annotated[list[str], operator.add]` 宣告，使後續節點回傳的 `steps_log` 與前節點累積；不被覆蓋。

#### Scenario: 多節點 log 串接保留
- **GIVEN** 節點 A 回傳 `{"steps_log": ["a1"]}`、後續節點 B 回傳 `{"steps_log": ["b1", "b2"]}`
- **WHEN** Graph 執行至 B 完成
- **THEN** state 中 `steps_log == ["a1", "b1", "b2"]`

### Requirement: 編譯後的 Agent 透過 singleton 重用

`get_agent()`（`src/agent/graph.py`）MUST 使用模組級單例 `_agent` 快取已編譯 graph；首次呼叫編譯，後續呼叫直接回傳同一物件，避免每次 query 重編譯。

#### Scenario: 連續呼叫不重編譯
- **WHEN** `get_agent()` 連續被呼叫兩次
- **THEN** 兩次回傳同一個物件（`is` 相同）

### Requirement: LangSmith Tracing SHALL 為可選且預設關閉

`is_tracing_enabled()`（`src/agent/graph.py`）MUST 同時要求兩個條件才視為啟用：（a）`LANGSMITH_TRACING` 或 `LANGCHAIN_TRACING_V2` 環境變數為真值（`true`/`1`/`yes`），且（b）`LANGSMITH_API_KEY` 或 `LANGCHAIN_API_KEY` 至少一個非空。任一條件不滿足 MUST 回傳 `False`。

#### Scenario: 預設關閉
- **GIVEN** `os.environ` 不含上述任何變數
- **WHEN** `is_tracing_enabled()` 被呼叫
- **THEN** 回傳 `False`

#### Scenario: 只設 flag 不設 key 時仍視為關閉
- **GIVEN** `LANGSMITH_TRACING=true` 但無任何 API key 環境變數
- **WHEN** `is_tracing_enabled()` 被呼叫
- **THEN** 回傳 `False`

#### Scenario: flag + key 齊備時啟用
- **GIVEN** `LANGSMITH_TRACING=true` 且 `LANGSMITH_API_KEY=ls__abc`
- **WHEN** `is_tracing_enabled()` 被呼叫
- **THEN** 回傳 `True`
