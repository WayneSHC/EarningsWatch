"""
src/core/safe.py
共用的「安全」小工具：環境變數解析與例外記錄。

放在 core 而非 utils 的原因：
  - retriever / contradiction / nodes / tools 都需要這些 helper
  - 屬於 core 的「基礎建設」層級，沒有業務邏輯
  - 避免每個模組各自實作一份（過去 _safe_int_env、_load_min_score_from_env、
    _log_bq_error 散落各處，行為微妙不一致 — 見 spec 003 audit F-2）

設計原則：
  - 永不 raise：環境變數 / log 都是次要功能，不該炸主流程
  - 訊息一致：固定 prefix 格式 "[{prefix}] ⚠ ..."，方便 grep
  - 截斷至 120 字：與 contradiction._unwrap + retriever._log_bq_error 的成例對齊，
    防止 LLM / BQ / Tavily SDK 例外訊息含 API key / endpoint 洩漏到 log / UI
"""

from __future__ import annotations

import os


_DEFAULT_EXC_TRUNC = 120


def safe_int_env(name: str, default: int, *, prefix: str = "Safe") -> int:
    """讀取整數環境變數；缺漏 / 非整數時退回 default 並警告（不 raise）。

    Args:
        name: 環境變數名稱
        default: fallback 值
        prefix: log 訊息的模組標籤（如 "Contradiction" / "Retriever"）

    Returns:
        int — 解析成功則為 env 值，否則為 default
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[{prefix}] ⚠ {name}={raw!r} 非整數，回退預設 {default}")
        return default


def safe_float_env(name: str, default: float, *, prefix: str = "Safe") -> float:
    """讀取浮點數環境變數；缺漏 / 非浮點時退回 default 並警告（不 raise）。

    Args:
        name: 環境變數名稱
        default: fallback 值
        prefix: log 訊息的模組標籤

    Returns:
        float — 解析成功則為 env 值，否則為 default
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(f"[{prefix}] ⚠ {name}={raw!r} 非浮點數，回退預設 {default}")
        return default


def log_exc(
    prefix: str,
    where: str,
    exc: BaseException,
    *,
    max_len: int = _DEFAULT_EXC_TRUNC,
) -> None:
    """[f] 統一例外記錄：截斷訊息 + 僅記錄類型名稱。

    外部 SDK 例外（LLM / BigQuery / Tavily / yfinance）常在 message 中夾帶
    API key 片段 / endpoint / project ID / trace-id 等。原樣 print 到 log 違反
    Constitution Principle VI。本 helper 提供一致的截斷格式。

    Args:
        prefix: 模組標籤（如 "Nodes" / "Tools" / "Retriever"）
        where:  發生位置 / 操作描述（如 "vector_search" / "Tavily 搜尋"）
        exc:    例外物件
        max_len: 訊息截斷長度（預設 120 字，與 contradiction._unwrap 對齊）
    """
    msg = str(exc)[:max_len]
    print(f"[{prefix}] ⚠ {where} 失敗（{type(exc).__name__}: {msg}）")
