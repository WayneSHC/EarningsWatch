# app-auth Specification

## Purpose

定義 `src/ui/auth.py` 的密碼閘門：`APP_PASSWORD` 觸發、未設定時自動跳過、`hmac.compare_digest` timing-safe 比對、失敗次數鎖定、`st.session_state` 保存驗證狀態。本 spec 確保本機開發無感、production 部署可用、密碼比對不受 timing side-channel 攻擊。

## Requirements

### Requirement: 未設定 `APP_PASSWORD` SHALL 跳過驗證

`require_password()` MUST 在 `get_secret("APP_PASSWORD")` 回傳空字串時立刻 return，不顯示輸入框、不呼叫 `st.stop()`，使本機開發者無需設定密碼。

#### Scenario: 缺密碼時函數無作用
- **GIVEN** `APP_PASSWORD` 未設定（`get_secret` 回 `""`）
- **WHEN** `require_password()` 被呼叫
- **THEN** 函數直接 return
- **AND** `st.stop` / `st.text_input` 不被呼叫

### Requirement: 已驗證的 session SHALL 不再要求密碼

`require_password()` MUST 在 `st.session_state["_authenticated"] == True` 時直接 return；MUST 不重複顯示輸入框。

#### Scenario: 已驗證 session 跳過
- **GIVEN** `APP_PASSWORD="x"`、`session_state["_authenticated"] = True`
- **WHEN** `require_password()` 被呼叫
- **THEN** 函數直接 return
- **AND** `st.text_input` 不被呼叫

### Requirement: 密碼比對 MUST 使用 `hmac.compare_digest`

驗證輸入密碼時 MUST 用 `hmac.compare_digest(pwd.encode(), expected.encode())`，MUST 不使用 `==` 或 `is`。原因：防 timing side-channel 攻擊（透過比較字串時的微秒差異推測密碼前綴）。

#### Scenario: 比對正確
- **GIVEN** `APP_PASSWORD="abc123"`、使用者輸入 `"abc123"`
- **WHEN** 「確認」按鈕被點擊
- **THEN** `session_state["_authenticated"] = True`
- **AND** `st.rerun()` 被呼叫

### Requirement: 失敗 `_MAX_ATTEMPTS=5` 次後 SHALL 鎖定 `_LOCKOUT_SECONDS=300` 秒

`require_password()` MUST 累計 `session_state["_pwd_fail_count"]`；達到 `_MAX_ATTEMPTS`（5）時 MUST 設 `session_state["_pwd_lockout_until"] = time.time() + _LOCKOUT_SECONDS`（300 秒）。鎖定期內任何新輸入嘗試 MUST 顯示「請 N 秒後再試」訊息並 `st.stop()`，MUST 不再比對。

#### Scenario: 第 5 次失敗觸發鎖定
- **GIVEN** `session_state["_pwd_fail_count"] = 4`、輸入錯誤密碼
- **WHEN** 確認被點擊
- **THEN** `_pwd_fail_count` 變為 5
- **AND** `_pwd_lockout_until` 為 `time.time() + 300` 附近
- **AND** 顯示「鎖定 5 分鐘」訊息

#### Scenario: 鎖定期內顯示倒數
- **GIVEN** `_pwd_fail_count >= 5`、`_pwd_lockout_until = time.time() + 200`
- **WHEN** `require_password()` 再次被呼叫
- **THEN** 顯示「嘗試次數過多，請 200 秒後再試」（秒數動態計算）
- **AND** `st.stop()` 被呼叫

#### Scenario: 成功登入清除失敗計數
- **GIVEN** `_pwd_fail_count = 2`
- **WHEN** 輸入正確密碼並確認
- **THEN** `_pwd_fail_count` 與 `_pwd_lockout_until` 從 session_state 被移除

### Requirement: 鎖定期過後 SHALL 允許再次嘗試

`require_password()` MUST 在 `_pwd_fail_count >= _MAX_ATTEMPTS` 但 `time.time() >= _pwd_lockout_until` 時，允許再次顯示輸入框（不再走鎖定分支）。

#### Scenario: 鎖定 5 分鐘後可重試
- **GIVEN** `_pwd_fail_count = 5`、`_pwd_lockout_until = time.time() - 1`（已過期）
- **WHEN** `require_password()` 被呼叫
- **THEN** 不顯示「請 N 秒後再試」
- **AND** 仍顯示密碼輸入框
