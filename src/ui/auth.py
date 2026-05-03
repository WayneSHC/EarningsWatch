"""
src/ui/auth.py

[f] 對外部署用的簡易密碼閘門。

僅在環境變數 `APP_PASSWORD` 有值時生效；本機開發未設定時自動跳過，
不影響開發體驗。

設計取捨：
  - 不做 user/role 系統：本專案是單一 demo 站，加 user 反而過度工程。
  - 不寫死密碼：必須由部署者在 Streamlit Cloud Secrets 設定。
  - 不做 hash 驗證：APP_PASSWORD 僅是「擋好奇心」級別的保護，
    若需嚴格驗證請用 SSO/OAuth 替代（不在本檔範圍）。
"""

from __future__ import annotations

import os
import streamlit as st


def require_password() -> None:
    """
    若設定了 APP_PASSWORD 而 session 尚未通過驗證 → 顯示輸入框並 `st.stop()`。
    通過後設 `st.session_state["_authenticated"]=True` 並 rerun。
    """
    expected = os.getenv("APP_PASSWORD", "").strip()
    if not expected:
        return  # 未設定 → 不啟用密碼保護

    if st.session_state.get("_authenticated"):
        return

    st.title("🔒 EarningsWatch")
    pwd = st.text_input("請輸入存取密碼", type="password", key="_pwd_input")
    if st.button("確認", type="primary"):
        if pwd == expected:
            st.session_state["_authenticated"] = True
            st.rerun()
        else:
            st.error("密碼錯誤，請重試")
    st.stop()
