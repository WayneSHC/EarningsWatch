"""
src/ui/auth.py

[f] 對外部署用的簡易密碼閘門。

僅在環境變數 `APP_PASSWORD` 有值時生效；本機開發未設定時自動跳過，
不影響開發體驗。

設計取捨：
  - 不做 user/role 系統：本專案是單一 demo 站，加 user 反而過度工程。
  - 不寫死密碼：必須由部署者在 Streamlit Cloud Secrets 設定。
  - 使用 hmac.compare_digest 做 timing-safe 比對，防暴力破解。
  - 失敗超過 MAX_ATTEMPTS 次後鎖定 LOCKOUT_SECONDS 秒。
  - [f] 失敗計數以 client IP 為 key（rate_limiter._AuthStore），
    開新分頁 / 清 cookie 無法重置計數，防暴力繞過。
    若 IP 無法取得（本機開發、測試），自動退回 st.session_state 行為。
  - 若需嚴格驗證請用 SSO/OAuth 替代（不在本檔範圍）。
"""

from __future__ import annotations

import hmac
import time
import streamlit as st

from src.core.secrets import get_secret
from src.core.rate_limiter import (
    get_client_ip,
    auth_fail_count,
    auth_lockout_until,
    auth_record_fail,
    auth_reset,
)

_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 分鐘


def require_password() -> None:
    """
    若設定了 APP_PASSWORD 而 session 尚未通過驗證 → 顯示輸入框並 `st.stop()`。
    通過後設 `st.session_state["_authenticated"]=True` 並 rerun。
    失敗 _MAX_ATTEMPTS 次後鎖定 _LOCKOUT_SECONDS 秒。

    失敗計數以 IP 為 key（跨 session 持久），IP 不可得時退回 session state。
    """
    expected = get_secret("APP_PASSWORD")
    if not expected:
        return  # 未設定 → 不啟用密碼保護

    if st.session_state.get("_authenticated"):
        return

    ip = get_client_ip()
    use_ip = bool(ip)

    # ── 讀取失敗計數 / 鎖定時間戳 ────────────────────────────────────────
    if use_ip:
        fail_count = auth_fail_count(ip)
        lockout_until = auth_lockout_until(ip)
    else:
        # [b] 本機 / 測試環境無 IP → 退回 session state（不影響開發體驗）
        fail_count = st.session_state.get("_pwd_fail_count", 0)
        lockout_until = st.session_state.get("_pwd_lockout_until", 0.0)

    # ── 鎖定檢查 ─────────────────────────────────────────────────────────
    if fail_count >= _MAX_ATTEMPTS and time.time() < lockout_until:
        remaining = int(lockout_until - time.time())
        st.title("🔒 EarningsWatch")
        st.error(f"嘗試次數過多，請 {remaining} 秒後再試")
        st.stop()

    # ── 密碼輸入表單 ──────────────────────────────────────────────────────
    st.title("🔒 EarningsWatch")
    pwd = st.text_input("請輸入存取密碼", type="password", key="_pwd_input")
    if st.button("確認", type="primary"):
        # [f] timing-safe 比對，防止 timing side-channel 攻擊
        if hmac.compare_digest(pwd.encode(), expected.encode()):
            st.session_state["_authenticated"] = True
            if use_ip:
                auth_reset(ip)
            else:
                st.session_state.pop("_pwd_fail_count", None)
                st.session_state.pop("_pwd_lockout_until", None)
            st.rerun()
        else:
            if use_ip:
                fail_count, lockout_until = auth_record_fail(
                    ip, _MAX_ATTEMPTS, _LOCKOUT_SECONDS
                )
            else:
                fail_count += 1
                st.session_state["_pwd_fail_count"] = fail_count
                if fail_count >= _MAX_ATTEMPTS:
                    lockout_until = time.time() + _LOCKOUT_SECONDS
                    st.session_state["_pwd_lockout_until"] = lockout_until

            if fail_count >= _MAX_ATTEMPTS:
                st.error(f"密碼錯誤次數過多，鎖定 {_LOCKOUT_SECONDS // 60} 分鐘")
            else:
                remaining_attempts = _MAX_ATTEMPTS - fail_count
                st.error(f"密碼錯誤，還剩 {remaining_attempts} 次機會")
    st.stop()
