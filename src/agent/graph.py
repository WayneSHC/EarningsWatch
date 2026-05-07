"""
src/agent/graph.py
LangGraph StateGraph 建構與編譯。

選用 LangGraph 而非 LangChain 的原因：
  - LangChain 是線性 Chain，無法處理 Self-Reflection 的迭代循環
  - LangGraph 的 StateGraph 原生支援條件跳轉（add_conditional_edges）
  - Self-Reflection 信心度不足時可以跳回 retrieve 節點重查，最多 3 輪
  - 這是整個 Agentic 架構的核心機制
"""

import os

from langgraph.graph import StateGraph, END


# ── [S4] LangSmith Tracing（選用，可觀測性）──────────────────────────────────
# 在 .env 設定下列任一即啟用：
#   LANGSMITH_TRACING=true   + LANGSMITH_API_KEY=ls__...
#   LANGCHAIN_TRACING_V2=true + LANGCHAIN_API_KEY=ls__...   （舊版相容）
# 啟用後 LangGraph 自動上傳每次 Agent 執行的節點軌跡至 https://smith.langchain.com
# 用途：debug 自我反思迴圈、觀測 prompt token 用量、回放歷史 trace
def is_tracing_enabled() -> bool:
    """[S4] 判斷 LangSmith tracing 是否就緒（環境變數齊全）。"""
    flag = (
        os.getenv("LANGSMITH_TRACING", "").strip().lower() in ("true", "1", "yes")
        or os.getenv("LANGCHAIN_TRACING_V2", "").strip().lower() in ("true", "1", "yes")
    )
    has_key = bool(
        os.getenv("LANGSMITH_API_KEY", "").strip()
        or os.getenv("LANGCHAIN_API_KEY", "").strip()
    )
    return flag and has_key


def tracing_status() -> str:
    """[S4] UI 顯示用：回傳簡短狀態字串。"""
    if is_tracing_enabled():
        project = os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or "default"
        return f"✅ LangSmith：{project}"
    return "⚪ LangSmith：未啟用"

from src.agent.state import AgentState
from src.agent.nodes import (
    intent_classifier,
    query_decomposer,
    dynamic_tool_router,
    parallel_retrieval,
    contradiction_detect,
    self_reflect,
    report_generator,
    should_continue,
)


def build_graph() -> StateGraph:
    """建構並回傳已編譯的 LangGraph Agent。"""
    workflow = StateGraph(AgentState)

    # ── 加入七個節點 ───────────────────────────────────────────────────
    workflow.add_node("classify",  intent_classifier)
    workflow.add_node("decompose", query_decomposer)
    workflow.add_node("route",     dynamic_tool_router)
    workflow.add_node("retrieve",  parallel_retrieval)
    workflow.add_node("detect",    contradiction_detect)   # ★ 核心
    workflow.add_node("reflect",   self_reflect)
    workflow.add_node("report",    report_generator)

    # ── 設定入口節點 ──────────────────────────────────────────────────
    workflow.set_entry_point("classify")

    # ── 線性邊 ───────────────────────────────────────────────────────
    workflow.add_edge("classify",  "decompose")
    workflow.add_edge("decompose", "route")
    workflow.add_edge("route",     "retrieve")
    workflow.add_edge("retrieve",  "detect")
    workflow.add_edge("detect",    "reflect")

    # ── 關鍵：條件邊（Self-Reflection 迭代的核心）──────────────────
    # should_continue 回傳 "retry" 或 "end"
    workflow.add_conditional_edges(
        "reflect",
        should_continue,
        {
            "retry": "retrieve",   # 信心不足 → 重新檢索
            "end":   "report",     # 信心足夠 → 輸出報告
        },
    )

    workflow.add_edge("report", END)

    return workflow.compile()


# singleton（避免每次呼叫都重新編譯）
_agent = None

def get_agent():
    global _agent
    if _agent is None:
        _agent = build_graph()
    return _agent


def run_agent(
    query: str,
    company: str,
    topic: str,
    quarters: list[str] | None = None,
) -> dict:
    """
    執行 Agent，回傳最終 AgentState。
    Streamlit UI 呼叫此函數。
    """
    agent = get_agent()
    initial_state: AgentState = {
        "query": query,
        "company": company,
        "topic": topic,
        "quarters": quarters or [],
        "sub_queries": [],
        "tool_plan": [],
        "retrieved": {},
        "news_context": [],
        "stock_data": {},
        "contradictions": [],
        "promises": [],
        "confidence": 1.0,
        "iteration": 0,
        "reflection_issues": [],
        "reflection_gaps": [],
        "coverage_matrix": {},
        "abstain": False,
        "cost_baseline_usd": 0.0,
        "cost_guard_triggered": False,
        "final_report": "",
        "steps_log": [],
    }
    result = agent.invoke(initial_state)
    return result


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    print("🔨 編譯 LangGraph Agent...")
    agent = build_graph()
    print("✅ Agent 編譯成功")
    print("\nAgent 節點：classify → decompose → route → retrieve → detect → reflect → report")
    print("條件邊：reflect → [retry: retrieve | end: report]")
