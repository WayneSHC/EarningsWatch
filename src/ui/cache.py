"""
src/ui/cache.py

Demo 保底快取 + XSS 防護字串工具。

設計動機：
  - Streamlit Cloud 上若 LLM API 配額用盡或 BigQuery 連線失敗，
    至少要能展示先前成功跑過的結果（demo_cache.json），不然 demo 直接 0 分。
  - `_sanitize_str` 是所有插入 HTML 字串的單一入口，
    集中在這裡方便日後加白名單 / 長度限制等政策。

擴出此檔的好處：
  - 純函數，零 Streamlit 依賴，可被 pytest 直接測試。
  - 與 UI rendering 分離，不會被 session_state 重渲染干擾。
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
from pathlib import Path

# 專案根目錄（依本檔案位置回推），與 app.py 行為一致
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = _PROJECT_ROOT / "cache" / "demo_cache.json"


def sanitize_str(val) -> str:
    """
    [f] 對任意值套用 html.escape，防止 XSS 注入。

    只要最終會插入 HTML（unsafe_allow_html=True）的字串都必須經過此函數。
    None 安全：回傳空字串而非 "None"。
    """
    return html.escape(str(val)) if val is not None else ""


def load_cache() -> dict:
    """讀取 demo cache；檔案不存在或損毀時回傳空 dict（不 raise）。"""
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            # [b] 損壞的快取不該讓 UI 整個掛掉，靜默回退
            pass
    return {}


def cache_key(
    company: str,
    topic: str,
    quarters: list[str] | None = None,
    custom_query: str = "",
) -> str:
    """[f] 含季度與自訂問題的 MD5 key，換季度或換問題不會命中舊快取。"""
    raw = f"{company}|{topic}|{sorted(quarters or [])}|{custom_query.strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


def get_cached_result(
    company: str,
    topic: str,
    quarters: list[str] | None = None,
    custom_query: str = "",
) -> dict | None:
    """命中時回傳完整 cache entry，未命中回傳 None。"""
    return load_cache().get(cache_key(company, topic, quarters, custom_query))


def save_to_cache(
    company: str,
    topic: str,
    result: dict,
    quarters: list[str] | None = None,
    custom_query: str = "",
) -> None:
    """
    將 Agent 結果寫入 demo cache。

    注意：`retrieved` 含完整向量 chunks，體積會非常大（單筆數 MB），
    快取只保留季度 key 列表，下次命中時 UI 不顯示原始 chunks（僅展示分析結果）。
    """
    cache = load_cache()
    cache[cache_key(company, topic, quarters, custom_query)] = {
        "final_report":   result.get("final_report", ""),
        "contradictions": result.get("contradictions", []),
        "promises":       result.get("promises", []),
        "steps_log":      result.get("steps_log", []),
        "confidence":     result.get("confidence", 0.0),
        # RAG Agent 展示欄位
        "iteration":      result.get("iteration", 1),
        "tool_plan":      result.get("tool_plan", ["bigquery"]),
        "news_context":   result.get("news_context", []),
        "stock_data":     result.get("stock_data", {}),
        "sub_queries":    result.get("sub_queries", []),
        "node_timings":   result.get("node_timings", {}),
        # 只存季度 key，不存 chunks（體積考量）
        "retrieved":      {q: [] for q in result.get("retrieved", {})},
    }
    CACHE_PATH.parent.mkdir(exist_ok=True)
    # [b] atomic write：先寫 temp file 再 os.replace，
    # 避免多 session 並行或 process 中斷時留下半截 JSON
    _data = json.dumps(cache, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_data)
        os.replace(tmp_path, CACHE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
