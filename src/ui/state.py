"""
src/ui/state.py
集中保管 Streamlit session state 的所有欄位。

設計動機：
  app.py 早期把 12 個 session_state 鍵散在 1000+ 行內，加新欄位要四處 grep、
  字串 typo 不會 raise 只會默默回 None。改用單一 dataclass 後：
    - 全部欄位一處宣告，易讀也易加
    - IDE 可做型別檢查與自動補全
    - 字串 key typo 變成 AttributeError，立刻暴露錯誤

設計選擇（為何不用巢狀 dataclass）：
  曾考慮 SingleResult / MultiResult / MultiState 等巢狀結構，但 meta 是 dict
  原因是下游 export / synthesize_diff / 比較表 都直接用 dict key 讀，改成
  dataclass 會把 blast radius 從 app.py 擴散到所有 consumer。本次 refactor
  只做「key 集中化」，不重新設計資料形狀。

Streamlit session 語意：
  Streamlit 會在每次 user 互動時重跑整個 app.py。`get()` 透過 setdefault
  確保整個 browser session 共用同一個 UIState 實例，欄位 mutation 會持續存在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class UIState:
    """單一 source of truth：app.py 所有 session_state 欄位都在這裡宣告。

    欄位語意：
      - last_run_time：上次「開始偵查」按鈕按下時間，給 cooldown 用
      - mode：當下顯示的結果類型（單公司 / 多公司比較）
      - result / meta：單公司模式的 agent 輸出 + UI 參數
      - multi_*：多公司比較模式的並行結果 + UI 參數
      - pdf_cache_key / *_pdf_bytes：PDF 匯出的快取 bytes（避免每次重渲染都重算）
    """

    # ── cooldown ──
    last_run_time: float = 0.0

    # ── 結果分派 ──
    mode: Literal["single", "multi"] | None = None

    # ── 單公司結果 ──
    result: dict | None = None
    meta: dict | None = None  # {"company", "topic", "quarters", "custom_query"}

    # ── 多公司比較結果 ──
    multi_results: dict | None = None
    multi_companies: list[str] = field(default_factory=list)
    multi_topic: str = ""
    multi_quarters: list[str] = field(default_factory=list)
    multi_custom_query: str = ""

    # ── PDF 匯出快取 ──
    pdf_cache_key: str | None = None
    single_pdf_bytes: bytes | None = None
    multi_pdf_bytes: bytes | None = None

    @classmethod
    def get(cls) -> "UIState":
        """取得本次 browser session 的 UIState（首次呼叫時建立、之後重用）。

        實作細節：streamlit 每次互動都會重跑 app.py，但 `st.session_state.setdefault`
        會回傳已存在的同一物件，所以欄位 mutation 跨 rerun 持續存在。
        """
        # 延遲 import streamlit：讓本模組能被無 streamlit 環境（如 unit test）import
        import streamlit as st
        return st.session_state.setdefault("_ui_state", cls())
