# secrets-management Specification

## Purpose

定義 `src/core/secrets.py` 對敏感資料（API keys、tokens）的解析行為：GCP Secret Manager 優先、`.env` / 環境變數 fallback、placeholder 字串視為缺值、Secret Manager 失敗時 graceful degrade、`bridge_to_env` 把 SM secrets 注入 `os.environ` 供第三方 SDK 使用。本 spec 確保 production（Cloud Run + Secret Manager）與本機開發（.env）能用同一段呼叫端程式，且 Secret Manager 服務中斷時應用仍能啟動。

## Requirements

### Requirement: `get_secret(name)` SHALL 依序嘗試 Secret Manager 然後 env var

`get_secret(name, default="")` MUST 依下列順序解析：
1. 若 `GCP_SECRET_PROJECT` 環境變數有值，呼叫 `_fetch_from_gcp(name, project)`；成功且非 placeholder 時回傳
2. 否則（或 GCP 步驟失敗 / 取得 placeholder），讀 `os.getenv(name, "").strip()`；非空且非 placeholder 時回傳
3. 全失敗回傳 `default`（預設 `""`）

#### Scenario: 無 GCP_SECRET_PROJECT 時直接走 env
- **GIVEN** `GCP_SECRET_PROJECT` 未設、`os.environ["FOO"] == "real-value"`
- **WHEN** `get_secret("FOO")` 被呼叫
- **THEN** 回傳 `"real-value"`、`_fetch_from_gcp` 不被呼叫

#### Scenario: GCP 成功時用 SM 值
- **GIVEN** `GCP_SECRET_PROJECT=my-proj`，SM 該 secret 回傳 `"sm-value"`
- **WHEN** `get_secret("FOO")` 被呼叫
- **THEN** 回傳 `"sm-value"`

#### Scenario: GCP 失敗時降級到 env
- **GIVEN** `GCP_SECRET_PROJECT=my-proj`，SM 對該 secret raise PermissionDenied，`os.environ["FOO"] == "env-fallback"`
- **WHEN** `get_secret("FOO")` 被呼叫
- **THEN** 回傳 `"env-fallback"`
- **AND** stdout 含 `failed to fetch` 字樣

#### Scenario: 全失敗回傳 default
- **GIVEN** `GCP_SECRET_PROJECT` 未設、`FOO` 不在 env
- **WHEN** `get_secret("FOO", default="fallback")` 被呼叫
- **THEN** 回傳 `"fallback"`

### Requirement: Placeholder 值 MUST 被視為缺值

`_is_placeholder(value)` MUST 在值經 `strip().lower()` 後含下列任一片段時回傳 `True`：`<your-`、`your-key-here`、`placeholder`、`changeme`、`sk-...`、`tvly-...`、`llx-...`、`ls__...`。`get_secret(...)` 對 placeholder 值 MUST 視同缺值並繼續 fallback 流程。

#### Scenario: `.env.example` 殘留值不被當成有效 key
- **GIVEN** `os.environ["OPENAI_API_KEY"] == "sk-...your-key-here..."`
- **WHEN** `get_secret("OPENAI_API_KEY")` 被呼叫
- **THEN** 回傳 `""`（fall through 到 default）

#### Scenario: 大寫變體也被偵測
- **GIVEN** `os.environ["X"] == "<YOUR-API-KEY>"`
- **WHEN** `get_secret("X")` 被呼叫
- **THEN** 回傳 `""`

#### Scenario: 真實值不被誤判
- **GIVEN** `os.environ["X"] == "sk-abc123def456ghi789"`
- **WHEN** `get_secret("X")` 被呼叫
- **THEN** 回傳 `"sk-abc123def456ghi789"`

### Requirement: Secret Manager SDK 缺席 MUST graceful degrade

`_sm_client()` MUST 在 `google.cloud.secretmanager` 無法 import 時 print 警告並回傳 `None`，使 `_fetch_from_gcp` 跳過 SM 直接降級到 env。MUST 不 raise。

#### Scenario: SDK 未安裝
- **GIVEN** `google.cloud.secretmanager` import raise `ImportError`
- **WHEN** `_sm_client()` 第一次被呼叫
- **THEN** 回傳 `None`
- **AND** stdout 含 `google-cloud-secret-manager not installed` 字樣

#### Scenario: 認證失敗
- **GIVEN** SDK 可 import 但 `SecretManagerServiceClient()` raise `Exception`
- **WHEN** `_sm_client()` 被呼叫
- **THEN** 回傳 `None`，不 raise

### Requirement: SM 結果 SHALL 以 LRU 快取避免重複呼叫

`_fetch_from_gcp(secret_name, project)` MUST 使用 `lru_cache(maxsize=64)`，相同 `(secret_name, project)` MUST 只發起一次 API 呼叫。`clear_cache()` MUST 清空 `_fetch_from_gcp` 與 `_sm_client` 兩個 cache，供測試與金鑰輪換情境使用。

#### Scenario: 重複呼叫命中快取
- **GIVEN** `_fetch_from_gcp("FOO", "p1")` 已被呼叫一次
- **WHEN** 同參數再次被呼叫
- **THEN** SM API 不再被呼叫

#### Scenario: clear_cache 後重新查詢
- **GIVEN** 同上快取已建立
- **WHEN** `clear_cache()` 被呼叫
- **AND** 之後再呼叫 `_fetch_from_gcp("FOO", "p1")`
- **THEN** SM API 被重新呼叫

### Requirement: `is_gcp_enabled()` SHALL 同時要求 project 設定與 SDK 可用

`is_gcp_enabled()` MUST 回傳 `bool(_gcp_project()) and _sm_client() is not None`。任一條件不滿足 MUST 回傳 `False`。

#### Scenario: project 未設定
- **GIVEN** `GCP_SECRET_PROJECT` 未設
- **WHEN** `is_gcp_enabled()` 被呼叫
- **THEN** 回傳 `False`

#### Scenario: project 設定且 SDK 可用
- **GIVEN** `GCP_SECRET_PROJECT=my-proj`、SDK 可正常 import
- **WHEN** `is_gcp_enabled()` 被呼叫
- **THEN** 回傳 `True`

### Requirement: `bridge_to_env(*names)` SHALL 保留已設定的 env var

`bridge_to_env(*names)` MUST 對每個 name：
1. 若 `os.environ.get(name, "").strip()` 已非空 → 略過（保留使用者明確設定的值）
2. 否則呼叫 `get_secret(name)` 取值；非空時寫入 `os.environ[name]`

此函數用於把 SM-only 的 secret 注入 `os.environ`，供 LangChain 等讀 `os.environ` 的 SDK 取得。

#### Scenario: 已設 env 不被覆寫
- **GIVEN** `os.environ["LANGSMITH_API_KEY"] == "user-value"`
- **WHEN** `bridge_to_env("LANGSMITH_API_KEY")` 被呼叫
- **THEN** `os.environ["LANGSMITH_API_KEY"] == "user-value"`（未變）

#### Scenario: 空 env 從 SM 注入
- **GIVEN** `os.environ` 不含 `LANGSMITH_API_KEY`、`get_secret("LANGSMITH_API_KEY")` 回傳 `"ls__abc"`
- **WHEN** `bridge_to_env("LANGSMITH_API_KEY")` 被呼叫
- **THEN** `os.environ["LANGSMITH_API_KEY"] == "ls__abc"`

#### Scenario: SM 無值時不寫 env
- **GIVEN** `get_secret("X")` 回傳 `""`、`os.environ` 不含 `X`
- **WHEN** `bridge_to_env("X")` 被呼叫
- **THEN** `os.environ` 仍不含 `X`
