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
  - 若需嚴格驗證請用 SSO/OAuth 替代（不在本檔範圍）。
"""

from __future__ import annotations

import hmac
import time
import streamlit as st

from src.core.secrets import get_secret

_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 分鐘


def require_password() -> None:
    """
    若設定了 APP_PASSWORD 而 session 尚未通過驗證 → 顯示輸入框並 `st.stop()`。
    通過後設 `st.session_state["_authenticated"]=True` 並 rerun。
    失敗 _MAX_ATTEMPTS 次後鎖定 _LOCKOUT_SECONDS 秒。
    """
    expected = get_secret("APP_PASSWORD")
    if not expected:
        return  # 未設定 → 不啟用密碼保護

    if st.session_state.get("_authenticated"):
        return

    # 鎖定檢查
    fail_count = st.session_state.get("_pwd_fail_count", 0)
    lockout_until = st.session_state.get("_pwd_lockout_until", 0.0)
    if fail_count >= _MAX_ATTEMPTS and time.time() < lockout_until:
        remaining = int(lockout_until - time.time())
        st.title("🔒 EarningsWatch")
        st.error(f"嘗試次數過多，請 {remaining} 秒後再試")
        st.stop()

    st.title("🔒 EarningsWatch")
    pwd = st.text_input("請輸入存取密碼", type="password", key="_pwd_input")
    if st.button("確認", type="primary"):
        # [f] timing-safe 比對，防止 timing side-channel 攻擊
        if hmac.compare_digest(pwd.encode(), expected.encode()):
            st.session_state["_authenticated"] = True
            st.session_state.pop("_pwd_fail_count", None)
            st.session_state.pop("_pwd_lockout_until", None)
            st.rerun()
        else:
            fail_count += 1
            st.session_state["_pwd_fail_count"] = fail_count
            if fail_count >= _MAX_ATTEMPTS:
                st.session_state["_pwd_lockout_until"] = time.time() + _LOCKOUT_SECONDS
                st.error(f"密碼錯誤次數過多，鎖定 {_LOCKOUT_SECONDS // 60} 分鐘")
            else:
                remaining_attempts = _MAX_ATTEMPTS - fail_count
                st.error(f"密碼錯誤，還剩 {remaining_attempts} 次機會")
    st.stop()
