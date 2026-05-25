"""
src/core/rate_limiter.py
跨 session 的 IP-based rate limiter（in-memory，thread-safe）。

設計取捨：
  - 純 in-memory：Streamlit Cloud 多實例部署時各 pod 獨立，無共享 state。
    若需跨節點共享必須改 Redis / Memcached，此處選擇單機簡單實作，
    在中小流量場景已能阻擋暴力連點。
  - TTL 自動清理：避免長時間運行下 IP dict 無限成長造成記憶體洩漏。
  - 同 session 沿用 st.session_state 機制 + 此模組的 IP 限流，雙保險。
    清空 cookie 換 tab 仍受 IP 限制；NAT 後多人共用 IP 則受 session 保護。
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
