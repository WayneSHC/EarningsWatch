# telemetry Specification

## Purpose

定義 `src/core/telemetry.py` 對 LLM 呼叫的成本 / token / 延遲觀測：thread-safe 全域 registry、靜態定價表的成本估算、未知模型 graceful degrade、按 backend 聚合彙整。本 spec 確保 cost guard、UI sidebar、benchmark per-query cost 三個下游消費端都能讀到一致的觀測數據。

## Requirements

### Requirement: `record(call)` SHALL 接受成功與失敗的 LLM 呼叫

`record(call: LLMCall)` MUST 將傳入的 `LLMCall` 追加進全域 `_registry._calls` 列表，不論其 `error` 欄位是否為 `None`。MUST 在持有 `Lock()` 的情況下寫入。

#### Scenario: 成功呼叫被記錄
- **WHEN** `record(LLMCall(backend="openai", model="gpt-5-mini", prompt_tokens=10, completion_tokens=20, cost_usd=0.001))` 被呼叫
- **THEN** `calls()` 包含該筆記錄

#### Scenario: 失敗呼叫亦被記錄
- **WHEN** `record(LLMCall(backend="openai", model="gpt-5", error="TimeoutError"))` 被呼叫
- **THEN** `calls()` 末筆 `error == "TimeoutError"`

### Requirement: `estimate_cost(backend, model, prompt_tokens, completion_tokens)` SHALL 查 `_PRICING` 表

`estimate_cost(...)` MUST 用 `(backend, model)` tuple 查 `_PRICING` 表，回傳 `(prompt_tokens / 1_000_000) * input_rate + (completion_tokens / 1_000_000) * output_rate`。表中未列出的 `(backend, model)` 組合 MUST 回傳 `0.0`，不 raise。

#### Scenario: 已知模型回傳合理成本
- **WHEN** `estimate_cost("openai", "gpt-5-mini", 1_000_000, 1_000_000)` 被呼叫
- **THEN** 回傳值 `> 0.0`（單位 USD）

#### Scenario: 未知模型回傳 0
- **WHEN** `estimate_cost("openai", "totally-fake-model", 1000, 1000)` 被呼叫
- **THEN** 回傳 `0.0`

#### Scenario: 未知 backend 回傳 0
- **WHEN** `estimate_cost("madeup", "x", 1000, 1000)` 被呼叫
- **THEN** 回傳 `0.0`

#### Scenario: 部分 token 也能算
- **WHEN** `estimate_cost("gemini", "gemini-2.5-flash", 500_000, 0)` 被呼叫
- **THEN** 回傳值 `> 0.0` 且等於 `0.5 * 0.075`（依當期 `_PRICING`）

### Requirement: `summary()` SHALL 聚合彙整，按 backend 分桶

`summary()` MUST 回傳一個 dict，包含至少下列鍵：
- `total_calls` / `successful_calls` / `failed_calls`
- `prompt_tokens` / `completion_tokens` / `total_tokens`
- `estimated_cost_usd`（`round(_, 6)`）
- `total_duration_ms`（`round(_, 1)`）
- `by_backend`: dict，每個 backend 的 `{calls, tokens, cost_usd}`

#### Scenario: 多筆呼叫的累加
- **GIVEN** 已 record 3 筆 openai 呼叫、2 筆 gemini 呼叫
- **WHEN** `summary()` 被呼叫
- **THEN** `total_calls == 5`、`by_backend["openai"]["calls"] == 3`、`by_backend["gemini"]["calls"] == 2`

#### Scenario: 失敗呼叫計入 failed_calls
- **GIVEN** 已 record 2 筆成功 + 1 筆失敗
- **WHEN** `summary()` 被呼叫
- **THEN** `successful_calls == 2`、`failed_calls == 1`、`total_calls == 3`

### Requirement: `reset()` SHALL 清空累積資料

`reset()` MUST 在 lock 保護下清空 `_registry._calls`。`benchmark.py` 在每題開始前呼叫，使 `summary()` 反映該題的獨立成本。

#### Scenario: reset 後 summary 為空
- **GIVEN** 已 record 多筆呼叫
- **WHEN** `reset()` 被呼叫
- **AND** 之後呼叫 `summary()`
- **THEN** `total_calls == 0`、`estimated_cost_usd == 0`

### Requirement: Registry MUST 為 thread-safe

並行 thread 同時呼叫 `record(...)` MUST 不會遺失資料；最終 `len(calls())` MUST 等於並行呼叫總次數。

#### Scenario: 並行 100 次寫入不丟失
- **GIVEN** 100 個 thread 各自呼叫 `record(...)` 一次
- **WHEN** 全部結束後 `calls()` 被呼叫
- **THEN** `len(calls()) == 100`

### Requirement: 定價表 SHALL 為單一來源（hardcoded），缺項 graceful degrade

`_PRICING` MUST 為模組級 dict，更新時 MUST 直接編輯該常數；MUST 不從外部設定檔或 API 動態載入。缺項 MUST 不阻擋 `record()`：`estimate_cost()` 回傳 `0.0` 但 `record()` 仍照常寫入完整 `LLMCall`。

#### Scenario: 缺定價時仍能 record
- **GIVEN** `_PRICING` 不含 `("openai", "weird-future-model")`
- **WHEN** `llm_client.chat()` 用該 model 呼叫 `record(...)`
- **THEN** `record()` 不 raise；`summary()` 中該筆的 `cost_usd == 0.0`，但 `tokens` 仍累加
