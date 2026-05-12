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


# ── [A2] LLM 驅動工具選擇 ────────────────────────────────────────────────────
# 工具規格表（function-calling style schema）：
#   給 LLM 看的工具清單，描述用途、輸入、適用情境。
#   LLM 依據問題語意決定要呼叫哪些工具，取代純關鍵字匹配。
TOOL_SPECS = [
    {
        "name": "bigquery",
        "description": "公司法說會逐字稿 BigQuery 向量檢索（核心工具，幾乎所有財報問題都需要）",
        "use_when": "需要查公司過去發言、財務指引、跨季比對時",
        "always_required": True,
    },
    {
        "name": "tavily",
        "description": "搜尋即時新聞、近期財報解讀、競爭對手動態。",
        "use_when": "問題涉及『最新』『近期』『今年』『市場動態』『產業趨勢』『競爭對手』時",
        "always_required": False,
    },
    {
        "name": "yfinance",
        "description": "獲取最新股價表現與本益比，作為市場反應的佐證。",
        "use_when": "問題明確涉及『股價』『市值』『漲跌』『報酬率』『投資價值』時",
        "always_required": False,
    },
]


def decide_tools_by_keyword(query: str, topic: str) -> list[str]:
    """關鍵字匹配版本（保留作為 LLM 失敗時的降級路徑）。"""
    tools = ["bigquery"]
    query_lower = query + topic
    if any(kw in query_lower for kw in _NEWS_KEYWORDS):
        tools.append("tavily")
    if any(kw in query_lower for kw in _STOCK_KEYWORDS):
        tools.append("yfinance")
    return tools


def decide_tools(query: str, topic: str) -> list[str]:
    """
    [A2] LLM 驅動的 Dynamic Tool Router。
    LLM 依工具規格表（TOOL_SPECS）判斷哪些工具與問題相關，
    取代舊版純關鍵字匹配（如「最新」→ tavily）。

    LLM 失敗或回應格式錯誤時自動降級為 decide_tools_by_keyword。
    bigquery 為 always_required，無論 LLM 怎麼選都會包含。
    """
    # 內部 import 避免 module load 時的循環依賴（tools 不依賴 llm_client，但保險起見）
    from src.core.llm_client import chat as llm_chat
    from src.core.contradiction import _extract_json

    tool_doc = "\n".join(
        f"- {t['name']}：{t['description']}\n  使用時機：{t['use_when']}"
        for t in TOOL_SPECS
    )
    prompt = f"""你是 Agent 的工具規劃器。可用工具如下：
{tool_doc}

使用者問題：{query}
主題：{topic}

只回傳 JSON（不要其他文字）：
{{
  "tools": ["bigquery", "tavily", ...],
  "reasoning": "10字以內的選擇理由"
}}

規則：
- bigquery 一律必選（公司問題都需要查發言）
- 只在問題真的需要時加 tavily 或 yfinance
- 若不確定，傾向不加（避免拖慢回應）
"""
    try:
        raw = llm_chat(prompt, max_tokens=150)
        parsed = _extract_json(raw)
        if isinstance(parsed, dict):
            tools_raw = parsed.get("tools", [])
            valid = {t["name"] for t in TOOL_SPECS}
            tools = [t for t in tools_raw if isinstance(t, str) and t in valid]
            # 強制納入 always_required 的工具
            for spec in TOOL_SPECS:
                if spec.get("always_required") and spec["name"] not in tools:
                    tools.append(spec["name"])
            if tools:
                return tools
    except Exception as e:
        # [b] 與 contradiction.py 一致：不吞錯誤訊息，方便辨識 429/quota
        from src.core.contradiction import _unwrap
        root = _unwrap(e)
        print(f"[Tools] decide_tools LLM 失敗（{type(root).__name__}: {str(root)[:120]}），降級為關鍵字匹配")

    return decide_tools_by_keyword(query, topic)
