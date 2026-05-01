"""
src/agent/tools.py
外部工具：Tavily 即時新聞、yfinance 股價資料。
Dynamic Tool Router 依問題類型選擇呼叫哪些工具。
"""

import os
from functools import lru_cache
from typing import Any
import yfinance as yf

# ── 工具路由關鍵字（提升為 module-level 常數，方便維護）────────────────────────
# [d] 與 decide_tools 函式分離，修改關鍵字不需要進入函式內部
_NEWS_KEYWORDS  = ["最新", "近期", "新聞", "市場", "今年", "現在", "目前"]
_STOCK_KEYWORDS = ["股價", "市值", "漲跌", "報酬", "ADR"]


# [c] lru_cache 確保同一程式生命週期內只實例化一次，與 _get_cohere_client 設計一致
@lru_cache(maxsize=1)
def _get_tavily():
    """Lazy 初始化 Tavily client；未設定 API Key 或未安裝 package 時回傳 None。"""
    key = os.getenv("TAVILY_API_KEY", "")
    if not key:
        return None
    try:
        from tavily import TavilyClient
        return TavilyClient(api_key=key)
    except ImportError:
        print("[Tools] tavily-python 未安裝，跳過新聞搜尋")
        return None


# ── 股票代號對照表 ─────────────────────────────────────────────────────────
STOCK_CODE_MAP = {
    "台積電": "TSM",     # yfinance 用美股 ADR 代號
    "聯發科": "2454.TW",
    "鴻海": "2317.TW",
    "台達電": "2308.TW",
}


def search_news(query: str, company: str, max_results: int = 5) -> list[dict]:
    """
    Tavily 即時新聞搜尋。
    若未設定 API Key，回傳空列表（不影響主流程）。

    Returns:
        [{"title", "content", "url", "published_date"}, ...]
    """
    client = _get_tavily()
    if not client:
        print("[Tools] Tavily 未設定，跳過新聞搜尋")
        return []

    search_query = f"{company} {query} 法說會 財報"
    try:
        resp = client.search(
            query=search_query,
            search_depth="basic",
            max_results=max_results,
            include_answer=False,
        )
        results = []
        for r in resp.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "content": r.get("content", "")[:500],
                "url": r.get("url", ""),
                "published_date": r.get("published_date", ""),
            })
        return results
    except Exception as e:
        print(f"[Tools] Tavily 搜尋失敗: {e}")
        return []


def get_stock_price(company: str, period: str = "1y") -> dict:
    """
    yfinance 取得股價資料。

    Args:
        company: 公司中文名稱
        period: 資料期間（1y = 一年）

    Returns:
        {"ticker", "current_price", "52w_high", "52w_low", "recent_change_pct"}
    """
    ticker_symbol = STOCK_CODE_MAP.get(company, "")
    if not ticker_symbol:
        return {"error": f"找不到 {company} 的股票代號"}

    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period=period)
        if hist.empty:
            return {"error": "無法取得股價資料"}

        current = float(hist["Close"].iloc[-1])
        high_52w = float(hist["Close"].max())
        low_52w = float(hist["Close"].min())
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else current
        change_pct = round((current - prev_close) / prev_close * 100, 2)

        # [b] 從 yfinance info 讀取貨幣，比「有無 . 」的格式判斷更可靠
        # fallback：有 .TW/.HK 等後綴 → 非美股；否則假設 USD
        _info_currency = ticker.info.get("currency", "")
        currency = _info_currency if _info_currency else (
            "USD" if "." not in ticker_symbol else "TWD"
        )
        return {
            "ticker": ticker_symbol,
            "company": company,
            "current_price": round(current, 2),
            "52w_high": round(high_52w, 2),
            "52w_low": round(low_52w, 2),
            "recent_change_pct": change_pct,
            "currency": currency,
        }
    except Exception as e:
        print(f"[Tools] yfinance 失敗: {e}")
        return {"error": str(e)}


def decide_tools(query: str, topic: str) -> list[str]:
    """
    Dynamic Tool Router：依問題特徵決定需要哪些工具。
    回傳工具名稱列表。

    工具選擇邏輯：
    - 一律使用知識庫向量搜尋（qdrant）
    - 問到近期新聞 / 市場動態 → 加 tavily
    - 問到股價 / 市值 → 加 yfinance
    """
    tools = ["qdrant"]  # 一定使用知識庫

    # 使用 module-level 常數（_NEWS_KEYWORDS / _STOCK_KEYWORDS），方便日後維護
    query_lower = query + topic
    if any(kw in query_lower for kw in _NEWS_KEYWORDS):
        tools.append("tavily")
    if any(kw in query_lower for kw in _STOCK_KEYWORDS):
        tools.append("yfinance")

    return tools
