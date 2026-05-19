# rate-limiting Specification

## Purpose

定義 `src/core/rate_limiter.py` 對「同一 IP 短時間內過度查詢」的限流行為：thread-safe in-memory dict、TTL 自動清理、X-Forwarded-For / X-Real-IP header 解析、與 Streamlit session-based 冷卻互補的雙層保護。本 spec 確保（a）反向代理後仍能取到真實客戶端 IP、（b）長時間運行不會記憶體洩漏、（c）測試可重置狀態。

## Requirements

### Requirement: `check(ip)` SHALL 回傳剩餘冷卻秒數，不更新狀態

`check(ip)` MUST 回傳 `max(0.0, _cooldown - (now - last_seen))`，即此 IP 還需等待幾秒；MUST 純讀取，不修改內部 `_last_seen` dict。`ip == ""` MUST 直接回傳 `0.0`（無法限流時退回 session-based 冷卻）。

#### Scenario: 全新 IP 無冷卻
- **GIVEN** `_last_seen` 中無 `"1.2.3.4"`
- **WHEN** `check("1.2.3.4")` 被呼叫
- **THEN** 回傳 `0.0`

#### Scenario: 剛紀錄過的 IP 仍在冷卻中
- **GIVEN** 已 `record("1.2.3.4")`、預設 cooldown 10 秒
- **WHEN** 立即呼叫 `check("1.2.3.4")`
- **THEN** 回傳值 `> 0` 且 `≤ 10.0`

#### Scenario: 空字串 IP 無冷卻
- **WHEN** `check("")` 被呼叫
- **THEN** 回傳 `0.0`

### Requirement: `record(ip)` SHALL 更新時間戳，並在 lock 內驅逐過期項目

`record(ip)` MUST 在持有 `_lock` 的情況下：
1. 呼叫 `_evict_stale(now)` 移除所有 `now - last_seen > _TTL_SEC`（預設 600 秒）的 IP
2. 將 `_last_seen[ip] = now`

`ip == ""` MUST 直接 return，不寫入。

#### Scenario: 寫入後 check 立刻看到冷卻
- **WHEN** `record("1.2.3.4")` 被呼叫
- **AND** 立即 `check("1.2.3.4")`
- **THEN** 回傳值 > 0

#### Scenario: TTL 過期項目在下次 record 時被驅逐
- **GIVEN** `_last_seen` 含 `"old.ip"`，時間戳為 `now - 700`（超過 TTL）
- **WHEN** `record("new.ip")` 被呼叫
- **THEN** `_last_seen` 不再含 `"old.ip"`

### Requirement: `_IPRateLimiter` MUST 為 thread-safe

模組級單例 `_limiter` 的所有讀寫 MUST 在 `Lock()` 保護下進行；並行多個 thread 同時呼叫 `record(...)` 寫入相同 IP MUST 不產生 race condition；最終 `_last_seen[ip]` MUST 為其中一次 `now` 值（不為 `None` 或 partial state）。

#### Scenario: 並行 record 不遺失
- **GIVEN** 100 個 thread 各自對相同 IP 呼叫 `record(...)`
- **WHEN** 全部結束
- **THEN** `_last_seen[ip]` 存在且為合法 float

### Requirement: `reset(ip=None)` SHALL 清除狀態（給測試使用）

`reset(None)` MUST 清空整個 `_last_seen` dict；`reset(ip)` MUST 只移除該 IP 的 entry。MUST 在 lock 保護下執行。

#### Scenario: reset 全部
- **GIVEN** 已 record 多個 IP
- **WHEN** `reset()` 被呼叫
- **THEN** 所有 IP 的 `check()` 回傳 `0.0`

#### Scenario: reset 單一 IP
- **GIVEN** 已 record `"a"` 與 `"b"`
- **WHEN** `reset("a")` 被呼叫
- **THEN** `check("a") == 0.0`、`check("b") > 0`

### Requirement: `get_client_ip()` SHALL 依序嘗試 X-Forwarded-For 然後 X-Real-IP

`get_client_ip()` MUST 從 `st.context.headers` 讀取 headers，並依下列順序回傳第一個非空值：
1. `X-Forwarded-For`（或 lowercase `x-forwarded-for`）→ 取 `split(",")[0].strip()`
2. `X-Real-IP`（或 lowercase `x-real-ip`）→ `strip()`
3. 全失敗或無 Streamlit context → 回傳 `""`

任何 import / context 取得錯誤 MUST 被吞掉，回傳 `""`。

#### Scenario: XFF 多 IP 取第一個
- **GIVEN** headers 含 `X-Forwarded-For: "203.0.113.5, 10.0.0.1, 10.0.0.2"`
- **WHEN** `get_client_ip()` 被呼叫
- **THEN** 回傳 `"203.0.113.5"`

#### Scenario: 只有 X-Real-IP 時 fall back
- **GIVEN** 無 `X-Forwarded-For`，但有 `X-Real-IP: "203.0.113.5"`
- **WHEN** `get_client_ip()` 被呼叫
- **THEN** 回傳 `"203.0.113.5"`

#### Scenario: XFF 蓋過 X-Real-IP
- **GIVEN** headers 同時含 `X-Forwarded-For: "1.1.1.1"` 與 `X-Real-IP: "2.2.2.2"`
- **WHEN** `get_client_ip()` 被呼叫
- **THEN** 回傳 `"1.1.1.1"`（XFF 優先）

#### Scenario: lowercase header key 相容
- **GIVEN** headers 只含 `x-forwarded-for: "203.0.113.5"`（小寫）
- **WHEN** `get_client_ip()` 被呼叫
- **THEN** 回傳 `"203.0.113.5"`

#### Scenario: 無 Streamlit context 回傳空字串
- **GIVEN** `import streamlit` 失敗或 `st.context` 不存在
- **WHEN** `get_client_ip()` 被呼叫
- **THEN** 回傳 `""`
- **AND** 不 raise

#### Scenario: 空 XFF 不阻斷 fall-through
- **GIVEN** headers 含 `X-Forwarded-For: ""` 與 `X-Real-IP: "5.5.5.5"`
- **WHEN** `get_client_ip()` 被呼叫
- **THEN** 回傳 `"5.5.5.5"`
