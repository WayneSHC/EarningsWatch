# llm-backend-cascade Specification

## Purpose

定義 `src/core/llm_client.py` 的統一 LLM 呼叫介面：四種後端（OpenAI / Gemini / Anthropic / Cohere）的自動偵測順序、`mode` 對應的模型選擇、quota / 429 / 401 / 404 / 503 等錯誤的偵測與分類、跨後端 fallback、同後端重試、逐呼叫 timeout、`LLMUnavailableError` 包裝、Prompt Injection guardrail，以及 telemetry 紀錄。本 spec 確保任何 LLM 失敗情境都能（a）給使用者乾淨的中文訊息、（b）讓 fallback 鏈完整走完才放棄、（c）不洩漏 raw HTTP response。

## Requirements

### Requirement: Auto-detect 後端 SHALL 依固定順序探測 API key

`_detect_backend()` MUST 在未設定 `LLM_BACKEND` 環境變數時，按 `["gemini", "openai", "anthropic", "cohere"]` 順序檢查對應的 API key 環境變數（`GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `COHERE_API_KEY`），回傳第一個有效金鑰對應的後端名稱。

#### Scenario: 第一個有 key 的後端勝出
- **GIVEN** 環境只有 `OPENAI_API_KEY` 與 `COHERE_API_KEY` 有效，`GEMINI_API_KEY` 為空
- **WHEN** `_detect_backend()` 被呼叫
- **THEN** 回傳 `"openai"`（因 gemini 缺 key 跳過）

#### Scenario: 全部後端皆無 key 時 raise EnvironmentError
- **GIVEN** 所有四個 API key 環境變數皆未設定
- **WHEN** `_detect_backend()` 被呼叫
- **THEN** raise `EnvironmentError` 並含「找不到任何 LLM API Key」字樣

### Requirement: 顯式 `LLM_BACKEND` SHALL 蓋過 auto-detect

`_detect_backend()` MUST 在 `LLM_BACKEND` 為支援的後端名（小寫後在 `BACKEND_MODELS` 中）時優先使用該後端；MUST 在該後端缺 key 時 raise `EnvironmentError`；MUST 在值不在支援清單時印警告並 fall through 到 auto-detect。

#### Scenario: 顯式合法值有 key
- **GIVEN** `LLM_BACKEND=anthropic`、`ANTHROPIC_API_KEY` 有效
- **WHEN** `_detect_backend()` 被呼叫
- **THEN** 回傳 `"anthropic"`，跳過其他偵測

#### Scenario: 顯式合法值無 key 立刻 raise
- **GIVEN** `LLM_BACKEND=anthropic`、`ANTHROPIC_API_KEY` 未設
- **WHEN** `_detect_backend()` 被呼叫
- **THEN** raise `EnvironmentError`，不 fall through 到其他後端

#### Scenario: 顯式不合法值降級為 auto-detect
- **GIVEN** `LLM_BACKEND=groq`（已移除）、有 `GEMINI_API_KEY`
- **WHEN** `_detect_backend()` 被呼叫
- **THEN** 印警告，然後回傳 `"gemini"`

### Requirement: `mode` 參數 SHALL 對映 dev/demo 模型

`get_model_name(mode)` 與 `chat(..., mode=...)` MUST 依 `mode` 從 `BACKEND_MODELS[backend][mode]` 取出對應模型 ID。支援 `mode in {"dev", "demo"}`；`"demo"` 對映付費 / 高品質模型（例如 `gpt-5`、`claude-sonnet-4-6`），`"dev"` 對映便宜 / 高頻模型（例如 `gpt-5-mini`、`claude-haiku-4-5-20251001`）。`chat()` 預設 `mode="demo"`。

#### Scenario: demo 模式取得 GPT-5
- **GIVEN** 後端為 `openai`
- **WHEN** `get_model_name("demo")` 被呼叫
- **THEN** 回傳 `"gpt-5"`

#### Scenario: dev 模式取得 Haiku
- **GIVEN** 後端為 `anthropic`
- **WHEN** `get_model_name("dev")` 被呼叫
- **THEN** 回傳 `"claude-haiku-4-5-20251001"`

### Requirement: GPT-5 系列呼叫 MUST 帶 `reasoning_effort` 避免空回應

`_call_openai_compat` MUST 對 model 名稱以 `gpt-5` 開頭的呼叫加上 `reasoning_effort` 參數。預設值為 `"minimal"`，可由環境變數 `LLM_REASONING_EFFORT` 覆寫。非 `gpt-5` 開頭的 OpenAI 模型（如 `gpt-4o`）MUST 不帶此參數（非 reasoning 模型，傳入會被 API 以 400 拒絕）。

原因：GPT-5 系列為 reasoning 模型，`max_completion_tokens` 同時涵蓋「推理 token」與「輸出 token」。在預設 `reasoning_effort`（medium）下，稍微複雜的 prompt 會把整個 token 預算耗在推理上、輸出 0 token，`message.content` 變為空字串或 `None`，使下游（如 `contradiction.batch_detect`）只能走 `_extract_json` 降級路徑、形同失效。`"minimal"` 確保預算留給實際輸出。

`_call_openai_compat` MUST 在 `message.content` 為 `None` 時回傳空字串（`(content or "").strip()`），不 raise `AttributeError`。

#### Scenario: gpt-5 帶 reasoning_effort
- **WHEN** `_call_openai_compat("...", "gpt-5", 600)` 被呼叫
- **THEN** 送進 OpenAI SDK 的 kwargs 含 `reasoning_effort == "minimal"`（或 `LLM_REASONING_EFFORT` 指定值）

#### Scenario: gpt-5-mini 也帶 reasoning_effort
- **WHEN** `_call_openai_compat("...", "gpt-5-mini", 600)` 被呼叫
- **THEN** kwargs 含 `reasoning_effort`

#### Scenario: 非 gpt-5 模型不帶 reasoning_effort
- **WHEN** `_call_openai_compat("...", "gpt-4o", 600)` 被呼叫
- **THEN** kwargs 不含 `reasoning_effort` 鍵

#### Scenario: content 為 None 時回傳空字串
- **GIVEN** OpenAI 回傳的 `message.content` 為 `None`
- **WHEN** `_call_openai_compat(...)` 被呼叫
- **THEN** 回傳的 text 為 `""`，不 raise

#### Scenario: 環境變數覆寫 reasoning effort
- **GIVEN** `LLM_REASONING_EFFORT=low`
- **WHEN** 模組重新載入並呼叫 `_call_openai_compat("...", "gpt-5", 600)`
- **THEN** kwargs 含 `reasoning_effort == "low"`

### Requirement: `chat()` MUST 在 quota / 認證 / 模型錯誤時跨後端 fallback

`chat()` MUST 將下列 marker 視為「不可重試、立刻換後端」（檢查全部以 case-insensitive 比對 `str(exc)`）：`RESOURCE_EXHAUSTED`、`quota`、`exceeded`、`insufficient_quota`、`billing`、`credit balance`、`credit_balance`、`low balance`、`rate limit`、`rate_limit`、`too many requests`、`429`、`401`、`403`、`404`、`NOT_FOUND`、`not_found`、`is not found`、`does not exist`、`model_not_found`、`InvalidModel`、`503`、`service unavailable`、`overloaded`。觸發時 MUST 印出由 `_format_quota_message()` 產生的中文友善提示，然後嘗試下一個有 key 的後端。

#### Scenario: 主後端 429 → 自動切換次選
- **GIVEN** 主後端為 `openai`，呼叫時 raise `Exception("429 Too Many Requests")`
- **AND** `GEMINI_API_KEY` 也有效
- **WHEN** `chat("hi")` 被呼叫
- **THEN** stdout 含 `OpenAI (GPT-5)` + `速率限制` 字樣
- **AND** 函數呼叫 `gemini` 後端並回傳其結果（若成功）

#### Scenario: Quota markers 優先於裸 429 分類
- **GIVEN** SDK raise 訊息含 `"Error code: 429 ... exceeded your current quota"`
- **WHEN** `_format_quota_message("openai", msg)` 被呼叫
- **THEN** 回傳訊息含 `今日 token / 配額已用完`（而非「速率限制」）

#### Scenario: 模型 404 → 切換並印「模型不存在」
- **GIVEN** SDK raise `"404 NOT_FOUND model gemini-3.0-flash does not exist"`
- **WHEN** `_format_quota_message("gemini", msg)` 被呼叫
- **THEN** 回傳訊息含 `模型名稱不存在或已下線`

#### Scenario: 401 → 切換並印「API Key 失效或權限不足」
- **GIVEN** SDK raise `"401 Unauthorized"`
- **WHEN** `_format_quota_message("openai", msg)` 被呼叫
- **THEN** 回傳訊息含 `API Key 失效或權限不足`

### Requirement: 暫時性網路錯誤 MUST 走同後端重試

`chat()` MUST 對 `TimeoutError`、或訊息中含 `connection / timeout / read timeout / connect timeout / network / temporarily / timed out / reset by peer`（case-insensitive）的例外，在同一後端最多重試 `_MAX_SAME_BACKEND_RETRIES`（預設 1）次。每次重試前 sleep `_RETRY_BASE_DELAY * 2^(attempt-1)` 秒，並在 stdout 印出重試訊息。重試耗盡後 MUST 切換到下一後端。

#### Scenario: 第一次網路錯誤後重試成功
- **GIVEN** 主後端第一次呼叫 raise `TimeoutError("read timeout")`
- **AND** 第二次呼叫成功
- **WHEN** `chat("hi")` 被呼叫
- **THEN** 回傳第二次的結果，不切換後端

#### Scenario: 重試耗盡後切換下一後端
- **GIVEN** 主後端每次呼叫都 raise `TimeoutError`
- **AND** 次選後端有 key 且成功
- **WHEN** `chat("hi")` 被呼叫
- **THEN** 函數最終回傳次選後端的結果
- **AND** stdout 含「重試耗盡，切換下一後端」字樣

### Requirement: 非暫時性、非 quota 例外 MUST 立刻 raise

`chat()` MUST 對不屬於 quota markers 也不屬於 transient markers 的例外（例如語法錯誤、SDK 內部 bug）立刻 re-raise，不重試也不切後端。

#### Scenario: 任意未分類 RuntimeError 直接 raise
- **GIVEN** 主後端 raise `RuntimeError("internal bug xyz")`
- **WHEN** `chat("hi")` 被呼叫
- **THEN** raise 原始 `RuntimeError`，不嘗試其他後端

### Requirement: 所有後端皆失敗時 MUST raise `LLMUnavailableError`

當 fallback 鏈中所有候選後端皆無法成功回應，`chat()` MUST raise `LLMUnavailableError`，其 `friendly_message` 為人類可讀中文訊息（含「所有 LLM 後端皆暫時不可用」字樣），`root_cause` 為最後一次例外。MUST 不洩漏原始 SDK response body / trace-id 到 `friendly_message`。

#### Scenario: 全部 quota 用盡
- **GIVEN** OpenAI / Gemini / Anthropic / Cohere 全部 raise 含 `quota` 的例外
- **WHEN** `chat("hi")` 被呼叫
- **THEN** raise `LLMUnavailableError`
- **AND** `str(error)` 含「所有 LLM 後端皆暫時不可用」字樣

### Requirement: 空 prompt MUST 立刻 raise `ValueError`

`chat(prompt, ...)` MUST 在 `prompt` 為空字串或經 `strip()` 後為空時，raise `ValueError`；MUST 不送任何後端請求、不消耗 quota。

#### Scenario: 空字串
- **WHEN** `chat("")` 被呼叫
- **THEN** raise `ValueError`

#### Scenario: 純空白
- **WHEN** `chat("   \n\t")` 被呼叫
- **THEN** raise `ValueError`

### Requirement: `chat()` 成功與失敗皆 MUST 記錄 telemetry

`chat()` MUST 在每次後端呼叫後（無論成功或失敗）寫一筆 `telemetry.LLMCall` 到 `telemetry` 全域 registry：
- **成功**：含 `prompt_tokens` / `completion_tokens` / `duration_ms` / `cost_usd`
- **失敗**：含 `duration_ms` / `error`（例外類別名稱），其餘為 `0`

#### Scenario: 成功路徑記錄完整 token
- **GIVEN** OpenAI 回傳 `usage.prompt_tokens=12`、`completion_tokens=34`
- **WHEN** `chat("hi")` 成功完成
- **THEN** `telemetry.calls()` 末筆 `prompt_tokens == 12`、`completion_tokens == 34`、`cost_usd > 0`

#### Scenario: 失敗路徑記錄錯誤類型
- **GIVEN** 主後端 raise，次選後端成功
- **WHEN** `chat("hi")` 完成
- **THEN** `telemetry.calls()` 至少含兩筆記錄
- **AND** 第一筆 `error` 為失敗的例外類別名稱

### Requirement: 後端切換 SHALL 透過 `set_backend()` 並清快取

`set_backend(backend)` MUST 在 backend 不在 `BACKEND_MODELS` 時 raise `ValueError`；MUST 在缺對應 API key 時 raise `ValueError`；成功時 MUST 設 `os.environ["LLM_BACKEND"]` 並清除 `_detect_backend.lru_cache`，讓下一次 `chat()` 重新偵測。

#### Scenario: 切到 Anthropic 後 chat 走 Anthropic
- **GIVEN** `ANTHROPIC_API_KEY` 有效
- **WHEN** `set_backend("anthropic")` 被呼叫
- **THEN** `_detect_backend()` 下一次回傳 `"anthropic"`

#### Scenario: 切到無 key 後端 raise
- **GIVEN** 無 `ANTHROPIC_API_KEY`
- **WHEN** `set_backend("anthropic")` 被呼叫
- **THEN** raise `ValueError` 含「缺少有效的 ANTHROPIC_API_KEY」字樣

#### Scenario: 切到未知後端 raise
- **WHEN** `set_backend("groq")` 被呼叫
- **THEN** raise `ValueError`

### Requirement: `friendly_error_message` MUST 將任意 LLM 例外翻譯為中文摘要

`friendly_error_message(exc)` MUST 從 `type(exc).__name__` 與 `str(exc)` 萃取分類，回傳形如 `"LLM XXX（{ExceptionClassName}）"` 的字串，不 MUST 包含 raw HTTP body / trace-id。對 `LLMUnavailableError` MUST 直接回傳 `exc.friendly_message`。

#### Scenario: 429 → 速率限制
- **WHEN** `friendly_error_message(Exception("429 Too Many Requests"))` 被呼叫
- **THEN** 回傳含 `速率限制` 字樣

#### Scenario: quota → 配額已用完
- **WHEN** `friendly_error_message(Exception("insufficient_quota"))` 被呼叫
- **THEN** 回傳含 `配額已用完` 字樣

#### Scenario: 通用例外
- **WHEN** `friendly_error_message(Exception("Strange undocumented error xyz123"))` 被呼叫
- **THEN** 回傳含 `呼叫失敗` 字樣

### Requirement: Prompt Injection Guardrail MUST 預設啟用

`chat()` MUST 在 `LLM_INJECTION_GUARD` 環境變數不為 `"false"`（case-insensitive）時，於 prompt 前綴一段「系統安全政策」說明，要求 LLM 將後續資料視為純粹文字而非指令。`LLM_INJECTION_GUARD=false` 時 MUST 直接送原始 prompt。

#### Scenario: 預設啟用
- **GIVEN** `LLM_INJECTION_GUARD` 未設定
- **WHEN** `chat("hello")` 被呼叫
- **THEN** 實際送至後端的 prompt 含「系統安全政策」字樣

#### Scenario: 明確關閉
- **GIVEN** `LLM_INJECTION_GUARD=false`
- **WHEN** `chat("hello")` 被呼叫
- **THEN** 實際送至後端的 prompt 等於 `"hello"`

### Requirement: 逐呼叫 Timeout SHALL 強制套用

`_dispatch_with_timeout(...)` MUST 將 SDK 呼叫提交至全域 ThreadPoolExecutor，並以 `LLM_TIMEOUT_SECONDS`（預設 45 秒）為硬性 timeout；逾時 MUST raise `TimeoutError`（會被 `chat()` 識別為 transient 並走重試 / fallback）。

#### Scenario: SDK 沒回應到 timeout 觸發
- **GIVEN** 某後端 SDK 呼叫超過 timeout
- **WHEN** `_dispatch_with_timeout(...)` 被呼叫
- **THEN** raise `TimeoutError` 含 `超過 {n}s 未回應` 字樣
