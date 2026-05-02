"""
src/agent/state.py
LangGraph AgentState 定義。
TypedDict 讓 StateGraph 能做型別推斷，也方便 Streamlit 讀取各步驟資訊。
"""

from typing import Annotated, Any
import operator
from typing_extensions import TypedDict


class AgentState(TypedDict):
    # ── 輸入 ─────────────────────────────────────────────────────────
    query: str              # 使用者原始問題
    company: str            # 目標公司（台積電）
    topic: str              # 分析主題（AI需求 / 毛利率 / ...）
    quarters: list[str]     # 指定季度範圍，空列表代表全部

    # ── 規劃 ─────────────────────────────────────────────────────────
    sub_queries: list[dict]         # Query Decomposer 拆解的子問題
    tool_plan: list[str]            # Dynamic Tool Router 的工具選擇

    # ── 檢索 ─────────────────────────────────────────────────────────
    retrieved: dict[str, list[dict]]    # {quarter: [chunks]}
    news_context: list[dict]            # Tavily 即時新聞
    stock_data: dict                    # yfinance 股價資料

    # ── 分析 ─────────────────────────────────────────────────────────
    contradictions: list[dict]  # Contradiction Detector 結果
    promises: list[dict]        # 承諾兌現追蹤結果

    # ── 自我評估 ──────────────────────────────────────────────────────
    confidence: float       # Self-Reflection 信心度（0.0 ~ 1.0）
    iteration: int          # 重查次數（最多 3 次）
    reflection_issues: list[str]    # 本輪發現的問題
    # LLM judge 指出的缺漏資訊主題，retry 時據此產生新的 sub_queries
    # e.g. ["缺乏 2024Q3 毛利率展望", "未涵蓋 AI 產能擴張時程"]
    reflection_gaps: list[str]

    # ── 輸出 ─────────────────────────────────────────────────────────
    final_report: str       # 最終偵查報告（Markdown 格式）

    # ── UI 顯示 ───────────────────────────────────────────────────────
    # Annotated + operator.add 讓每個節點可以 append，不會被覆蓋
    steps_log: Annotated[list[str], operator.add]   # Agent 思考步驟，即時顯示用
