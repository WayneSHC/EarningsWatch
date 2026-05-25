"""
src/core/rate_limiter.py
跨 session 的 IP-based rate limiter 與 auth 失敗計數器（in-memory，thread-safe）。

設計取捨：
  - 純 in-memory：Streamlit Cloud 多實例部署時各 pod 獨立，無共享 state。
    若需跨節點共享必須改 Redis / Memcached，此處選擇單機簡單實作，
    在中小流量場景已能阻擋暴力連點。
  - TTL 自動清理：避免長時間運行下 IP dict 無限成長造成記憶體洩漏。
  - 同 session 沿用 st.session_state 機制 + 此模組的 IP 限流，雙保險。
    清空 cookie 換 tab 仍受 IP 限制；NAT 後多人共用 IP 則受 session 保護。

_AuthStore（新增）：
  - auth.py 的密碼失敗計數 / 鎖定時間戳改存此處，以 IP 為 key。
  - 開新分頁 / 清 cookie 不再重置失敗計數，防暴力破解繞過 session 鎖定。
  - 若 IP 無法取得（本機開發、測試）自動退回 st.session_state 行為，
    不影響開發體驗與現有測試。
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Optional


_DEFAULT_COOLDOWN_SEC = 10.0
# 超過此 TTL 未活動的 IP 從 dict 移除，避免無限成長
_TTL_SEC = 600.0


class _IPRateLimiter:
    """[c] thread-safe：Streamlit 用 ScriptRunner 為每個 session 起 thread。"""

    def __init__(self, cooldown_sec: float = _DEFAULT_COOLDOWN_SEC) -> None:
        self._cooldown = cooldown_sec
        self._lock = Lock()
        self._last_seen: dict[str, float] = {}

    def _evict_stale(self, now: float) -> None:
        """移除超過 _TTL_SEC 未活動的 IP（呼叫時必須持有 _lock）。"""
        stale = [ip for ip, t in self._last_seen.items() if now - t > _TTL_SEC]
        for ip in stale:
            self._last_seen.pop(ip, None)

    def check(self, ip: str) -> float:
        """
        回傳此 IP 還需等待幾秒（0 = 可立即執行）。
        不更新狀態，純讀取。
        """
        if not ip:
            return 0.0
        now = time.time()
        with self._lock:
            last = self._last_seen.get(ip, 0.0)
        elapsed = now - last
        return max(0.0, self._cooldown - elapsed)

    def record(self, ip: str) -> None:
        """記錄此 IP 剛剛執行過一次查詢，重置冷卻。"""
        if not ip:
            return
        now = time.time()
        with self._lock:
            self._evict_stale(now)
            self._last_seen[ip] = now

    def reset(self, ip: Optional[str] = None) -> None:
        """測試 / debug 用：清空特定 IP 或全部。"""
        with self._lock:
            if ip is None:
                self._last_seen.clear()
            else:
                self._last_seen.pop(ip, None)


# 模組級單例（所有 Streamlit session 共用）
_limiter = _IPRateLimiter()


# ── Auth fail counter（IP-keyed，thread-safe）────────────────────────────────

class _AuthStore:
    """
    [f] 跨 session 的密碼失敗計數 + 鎖定時間戳，以 client IP 為 key。

    將鎖定狀態從 st.session_state 移至此處，使攻擊者無法透過
    開新 tab / 清 cookie 繞過失敗次數上限。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        # ip → (fail_count, lockout_until_timestamp, last_activity_timestamp)
        self._state: dict[str, tuple[int, float, float]] = {}

    def _evict_stale(self, now: float) -> None:
        """移除最後活動超過 _TTL_SEC 的 IP（呼叫時必須持有 _lock）。"""
        stale = [
            ip for ip, (_, _, last) in self._state.items()
            if now - last > _TTL_SEC
        ]
        for ip in stale:
            self._state.pop(ip, None)

    def fail_count(self, ip: str) -> int:
        """回傳 IP 目前的失敗次數。"""
        with self._lock:
            return self._state.get(ip, (0, 0.0, 0.0))[0]

    def lockout_until(self, ip: str) -> float:
        """回傳 IP 的鎖定解除時間戳（0.0 = 未鎖定）。"""
        with self._lock:
            return self._state.get(ip, (0, 0.0, 0.0))[1]

    def record_fail(self, ip: str, max_attempts: int, lockout_seconds: float) -> tuple[int, float]:
        """
        遞增 IP 失敗次數。達到 max_attempts 時設定鎖定時間戳。
        回傳 (新的失敗次數, 鎖定解除時間戳)。
        """
        if not ip:
            return (0, 0.0)
        now = time.time()
        with self._lock:
            self._evict_stale(now)
            count, until, _ = self._state.get(ip, (0, 0.0, now))
            count += 1
            if count >= max_attempts:
                until = now + lockout_seconds
            self._state[ip] = (count, until, now)
            return count, until

    def reset(self, ip: Optional[str] = None) -> None:
        """成功登入或測試用：清除特定 IP 或全部。"""
        with self._lock:
            if ip is None:
                self._state.clear()
            else:
                self._state.pop(ip, None)


_auth_store = _AuthStore()


def auth_fail_count(ip: str) -> int:
    """回傳 IP 目前的密碼失敗次數。"""
    return _auth_store.fail_count(ip)


def auth_lockout_until(ip: str) -> float:
    """回傳 IP 的鎖定解除時間戳（0.0 = 未鎖定）。"""
    return _auth_store.lockout_until(ip)


def auth_record_fail(ip: str, max_attempts: int, lockout_seconds: float) -> tuple[int, float]:
    """遞增 IP 失敗計數，達限時鎖定。回傳 (新計數, 鎖定解除時間戳)。"""
    return _auth_store.record_fail(ip, max_attempts, lockout_seconds)


def auth_reset(ip: Optional[str] = None) -> None:
    """清除 IP 的失敗記錄（登入成功或測試用）。"""
    _auth_store.reset(ip)


def check(ip: str) -> float:
    """回傳 IP 還需等待幾秒（0 = 可執行）。"""
    return _limiter.check(ip)


def record(ip: str) -> None:
    """記錄 IP 剛執行過。"""
    _limiter.record(ip)


def reset(ip: Optional[str] = None) -> None:
    """測試用：清狀態。"""
    _limiter.reset(ip)


def get_client_ip() -> str:
    """
    從 Streamlit request 取出 client IP。

    [b] 多層 fallback：
      1. X-Forwarded-For（反向代理 / Streamlit Cloud 設定）— 取第一個（client 真實 IP）
      2. X-Real-IP（部分代理用此 header）
      3. 直連 remote address（少見，本機開發）
      4. 全失敗 → 回 ""，呼叫端應視為「無法限流」並退回 session 限流
    """
    try:
        # Streamlit 1.27+ 提供 st.context.headers
        import streamlit as st
        headers = st.context.headers if hasattr(st, "context") else {}
    except Exception:
        return ""

    if not headers:
        return ""

    # X-Forwarded-For 格式：「client, proxy1, proxy2」→ 取第一個
    # [f] 只接受合法 IPv4/IPv6 字元，防止 header 偽造注入任意字串作為 key
    import re as _re
    _IP_RE = _re.compile(r"^[0-9a-fA-F.:]+$")

    xff = headers.get("X-Forwarded-For", "") or headers.get("x-forwarded-for", "")
    if xff:
        ip = xff.split(",")[0].strip()
        if ip and _IP_RE.match(ip):
            return ip

    real = headers.get("X-Real-IP", "") or headers.get("x-real-ip", "")
    if real:
        real = real.strip()
        if _IP_RE.match(real):
            return real

    return ""
