"""
src/agent/nodes.py
LangGraph 七節點的具體實作。

節點執行順序：
  classify → decompose → route → retrieve → detect → reflect → report

每個節點接收 AgentState，回傳更新後的 state 片段（dict）。
"""

import os
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import mean
from typing import Any

from src.agent.state import AgentState
from src.agent.tools import decide_tools, search_news, get_stock_price
from src.core.retriever import retrieve, get_company_quarters, retrieve_coverage
from src.core.contradiction import batch_detect, detect_promises
from src.core.llm_client import chat as llm_chat


def _llm(prompt: str, max_tokens: int = 500) -> str:
    return llm_chat(prompt, max_tokens=max_tokens)


# ══════════════════════════════════════════════════════════════════════════════
# 節點 1：Intent Classifier
# ══════════════════════════════════════════════════════════════════════════════
def intent_classifier(state: AgentState) -> dict:
    """
    分析使用者問題，萃取：公司、主題、季度範圍。
    若使用者已在 UI 選好公司和主題，直接沿用，不重複解析。
    """
    log = [f"🔍 **分析問題意圖**：{state['query']}"]

    # 若 UI 已提供 company 和 topic，直接使用
    company = state.get("company", "").strip()
    topic = state.get("topic", "").strip()
    quarters = state.get("quarters", [])

    if not company or not topic:
        # 用 LLM 萃取
        resp = _llm(f"""從以下問題萃取資訊，只回傳 JSON：
問題：{state['query']}
{{
  "company": "公司名（台積電/聯發科/鴻海/台達電）",
  "topic": "主題（AI需求/毛利率/產能/庫存/展望）",
  "quarters": ["2024Q1", "2024Q3"] 或 [] 代表全部
}}""", max_tokens=200)
        try:
            # 防呆：re.search 若回傳 None（LLM 未輸出 JSON），先檢查再呼叫 .group()
            m = re.search(r'\{.*\}', resp, re.DOTALL)
            parsed = json.loads(m.group()) if m else {}
            company = company or parsed.get("company", "台積電")
            topic = topic or parsed.get("topic", "")
            quarters = quarters or parsed.get("quarters", [])
        except Exception:
            company = company or "台積電"

    log.append(f"  → 公司：{company}｜主題：{topic}｜季度：{quarters or '全部'}")
    return {"company": company, "topic": topic, "quarters": quarters, "steps_log": log}


# ══════════════════════════════════════════════════════════════════════════════
# 節點 2：Query Decomposer
# ══════════════════════════════════════════════════════════════════════════════
def query_decomposer(state: AgentState) -> dict:
    """
    將問題拆解為三條子任務：
    1. 各季發言查詢（知識庫）
    2. 財務數字查詢（知識庫 guidance 段落）
    3. 即時新聞背景（Tavily，若有相關）
    """
    # [b] 用 .get() 防呆：若 intent_classifier 因 LLM 失敗未回傳欄位，不引發 KeyError
    company = state.get("company", "")
    topic   = state.get("topic", "")
    query   = state.get("query", "")
    log = [f"📋 **分解子問題**"]

    sub_queries = [
        {
            "id": "cross_quarter",
            "query": f"{company} {topic} 發言 各季比對",
            "purpose": "跨季發言比對",
            "tool": "qdrant",
        },
        {
            "id": "guidance",
            "query": f"{company} {topic} 預估 指引 guidance",
            "purpose": "財務指引與承諾追蹤",
            "tool": "qdrant",
            "section_filter": "guidance",
        },
        {
            "id": "news",
            "query": f"{company} {topic} 最新動態",
            "purpose": "即時新聞背景",
            "tool": "tavily",
        },
    ]

    for sq in sub_queries:
        log.append(f"  → [{sq['purpose']}] {sq['query']}")

    return {"sub_queries": sub_queries, "steps_log": log}


# ══════════════════════════════════════════════════════════════════════════════
# 節點 3：Dynamic Tool Router
# ══════════════════════════════════════════════════════════════════════════════
def dynamic_tool_router(state: AgentState) -> dict:
    """
    決定本次查詢需要哪些工具（知識庫 / Tavily / yfinance）。
    """
    tools = decide_tools(state["query"], state["topic"])
    log = [f"🔧 **工具選擇**：{', '.join(tools)}"]
    return {"tool_plan": tools, "steps_log": log}


# ══════════════════════════════════════════════════════════════════════════════
# 節點 4：Parallel Retrieval
# ══════════════════════════════════════════════════════════════════════════════
def parallel_retrieval(state: AgentState) -> dict:
    """
    依 sub_queries 真正並行執行檢索（ThreadPoolExecutor）。
    - Qdrant 向量搜尋、Tavily 新聞、yfinance 股價三路同時發出，互不阻塞
    - 知識庫結果按季度分組，便於 Contradiction Detector 跨季比對
    - 各子任務獨立捕捉例外，單一失敗不影響其他查詢
    """
    log = [f"📚 **檢索知識庫**（第 {state.get('iteration', 0) + 1} 輪）"]
    company = state["company"]
    quarters_filter = state.get("quarters", []) or None
    tool_plan = state.get("tool_plan", ["qdrant"])
    sub_queries = state.get("sub_queries", [])

    retrieved: dict[str, list[dict]] = {}
    news_context: list[dict] = []
    stock_data: dict = {}

    # ── 定義各工具的任務函式 ──────────────────────────────────────────
    def _do_qdrant(sq: dict) -> list[dict]:
        """單一 qdrant sub_query 的檢索，供 ThreadPoolExecutor 呼叫。"""
        # cross_quarter 子查詢不套用季度過濾，才能跨季比對
        # guidance / 其他子查詢則遵守使用者選擇的季度範圍
        sq_quarters = None if sq["id"] == "cross_quarter" else quarters_filter
        return retrieve(
            query=sq["query"],
            company=company,
            quarters=sq_quarters,
            section=sq.get("section_filter"),
            top_k=5,
        )

    def _do_tavily() -> list[dict]:
        return search_news(state["query"], company)

    def _do_yfinance() -> dict:
        return get_stock_price(company)

    # ── 建立任務清單（future → 用途標籤）────────────────────────────
    futures_map = {}
    # [c] 實際任務數：最多 2 qdrant + 1 tavily + 1 yfinance = 4
    # 過多 worker 浪費資源；動態計算避免未來工具變更時需要手動更新
    _n_workers = min(len([sq for sq in sub_queries if sq.get("tool") == "qdrant"]) + 2, 6)
    with ThreadPoolExecutor(max_workers=_n_workers) as executor:
        if "qdrant" in tool_plan:
            for sq in sub_queries:
                if sq.get("tool") == "qdrant":
                    fut = executor.submit(_do_qdrant, sq)
                    futures_map[fut] = ("qdrant", sq)

        if "tavily" in tool_plan:
            fut = executor.submit(_do_tavily)
            futures_map[fut] = ("tavily", None)

        if "yfinance" in tool_plan:
            fut = executor.submit(_do_yfinance)
            futures_map[fut] = ("yfinance", None)

        # ── 收集結果（as_completed 讓先完成的先處理）──────────────────
        for fut in as_completed(futures_map):
            kind, sq = futures_map[fut]
            try:
                result = fut.result()
            except Exception as e:
                # [f] steps_log 會顯示在 UI，只記錄 type.__name__ 防止 API key 片段洩漏
                _etype = type(e).__name__
                print(f"[Nodes] [{kind}] 查詢失敗: {_etype}: {e}")
                log.append(f"  ⚠ [{kind}] 查詢失敗（{_etype}），不影響其他工具結果")
                continue

            if kind == "qdrant":
                for chunk in result:
                    quarter = chunk.get("payload", {}).get("quarter", "unknown")
                    retrieved.setdefault(quarter, []).append(chunk)
                    log.append(f"  → [{quarter}] {chunk['payload'].get('content', '')[:60]}...")
            elif kind == "tavily":
                news_context = result
                if news_context:
                    log.append(f"  → Tavily 取得 {len(news_context)} 篇即時新聞")
            elif kind == "yfinance":
                stock_data = result
                if "error" not in stock_data:
                    log.append(f"  → 股價：{stock_data.get('current_price')} {stock_data.get('currency')}")

    # ── 季度覆蓋補充（確保所有季度都有代表性資料）────────────────────
    # 只在使用者未指定特定季度時才補充（選「全部」才需要全覆蓋）
    if not quarters_filter and retrieved:
        all_q = get_company_quarters(company)
        missing = [q for q in all_q if q not in retrieved]
        if missing:
            log.append(f"  🔍 補充 {len(missing)} 個未覆蓋季度：{missing}")
            main_query = state.get("query", "")
            coverage = retrieve_coverage(main_query, company, missing, top_k_per_quarter=2)
            for q, chunks in coverage.items():
                retrieved.setdefault(q, []).extend(chunks)
                log.append(f"  → [{q}] 補充 {len(chunks)} 筆（覆蓋掃描）")

    if not retrieved:
        log.append("  ⚠ 知識庫無結果，請確認 PDF 已匯入並 embedding 完成")

    return {
        "retrieved": retrieved,
        "news_context": news_context,
        "stock_data": stock_data,
        "steps_log": log,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 節點 5：Contradiction Detector ★ 核心
# ══════════════════════════════════════════════════════════════════════════════
def contradiction_detect(state: AgentState) -> dict:
    """
    跨季語意比對：
    - 找矛盾發言
    - 追承諾兌現率
    這是整個系統最關鍵的差異化功能。
    """
    log = ["🔎 **矛盾偵測分析**"]
    retrieved = state.get("retrieved", {})

    if len(retrieved) < 2:
        log.append("  ⚠ 只有 1 季資料，需要至少 2 季才能做跨季比對")
        return {"contradictions": [], "promises": [], "steps_log": log}

    topic = state.get("topic", "")

    # 矛盾偵測
    contradictions = batch_detect(retrieved, topic)
    for c in contradictions:
        analysis = c["analysis"]
        if analysis.get("has_contradiction"):
            log.append(
                f"  🚨 [{c['quarter_a']} vs {c['quarter_b']}] "
                f"立場{analysis['stance_change']}：{analysis['change_detail']}"
            )
        else:
            log.append(f"  ✅ [{c['quarter_a']} vs {c['quarter_b']}] 發言一致")

    # 承諾追蹤
    promises = detect_promises(retrieved, topic)
    for p in promises:
        log.append(f"  {p['status']} [{p['promise_quarter']}承諾] {p['content']}")

    return {"contradictions": contradictions, "promises": promises, "steps_log": log}


# ══════════════════════════════════════════════════════════════════════════════
# 節點 6：Self-Reflection
# ══════════════════════════════════════════════════════════════════════════════
def self_reflect(state: AgentState) -> dict:
    """
    評估回答品質，決定是否需要重查。
    信心度 < 0.75 且迭代次數 < 3 → 觸發重新檢索。

    這是證明 Agentic 架構的關鍵機制：系統真的在自我評估。
    """
    log = ["🤔 **自我評估**"]
    score = 1.0
    issues = []
    retrieved = state.get("retrieved", {})
    contradictions = state.get("contradictions", [])

    # 檢查 1：跨季資料是否足夠
    if len(retrieved) < 2:
        issues.append("只找到 1 季資料，需要至少 2 季才能跨季比對")
        score -= 0.4

    # 檢查 2：矛盾偵測信心度
    if contradictions:
        confidences = [
            c["analysis"].get("confidence", 0.5)
            for c in contradictions
            if c.get("analysis")
        ]
        if confidences:
            avg_conf = mean(confidences)
            if avg_conf < 0.6:
                issues.append(f"矛盾偵測信心度偏低（{avg_conf:.2f}），結果可能不準確")
                score -= 0.3

    # 檢查 3：是否有足夠的 chunk
    total_chunks = sum(len(v) for v in retrieved.values())
    if total_chunks < 3:
        issues.append(f"檢索到的相關段落過少（{total_chunks} 個），資訊可能不足")
        score -= 0.2

    score = max(0.0, round(score, 2))

    if issues:
        log.append(f"  ⚠ 信心度：{score:.2f}，發現 {len(issues)} 個問題：")
        for issue in issues:
            log.append(f"    - {issue}")
    else:
        log.append(f"  ✅ 信心度：{score:.2f}，品質達標")

    # 若需重查，擴展搜尋關鍵字
    new_sub_queries = state.get("sub_queries", [])
    if score < 0.75:
        iteration = state.get("iteration", 0)
        log.append(f"  🔄 觸發重查（第 {iteration + 1} 次），擴展關鍵字...")
        new_sub_queries = [
            {**sq, "query": sq["query"] + f" {state.get('topic', '')} 詳細說明"}
            for sq in new_sub_queries
        ]

    return {
        "confidence": score,
        "reflection_issues": issues,
        "iteration": state.get("iteration", 0) + 1,
        "sub_queries": new_sub_queries,
        "steps_log": log,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 節點 7：Report Generator
# ══════════════════════════════════════════════════════════════════════════════
def report_generator(state: AgentState) -> dict:
    """
    整合所有分析結果，生成結構化偵查報告（Markdown 格式）。
    每個論點標明來源文件與頁碼。
    """
    log = ["📝 **生成偵查報告**"]
    company = state.get("company", "")
    topic = state.get("topic", "")
    contradictions = state.get("contradictions", [])
    promises = state.get("promises", [])
    retrieved = state.get("retrieved", {})
    news = state.get("news_context", [])
    stock = state.get("stock_data", {})
    confidence = state.get("confidence", 1.0)

    # ── 組裝報告內容 ───────────────────────────────────────────────────
    sections = [
        f"# 🕵️ EarningsWatch 偵查報告",
        f"**公司**：{company}　**主題**：{topic}　**分析信心度**：{confidence:.0%}",
        "",
    ]

    # 矛盾摘要
    has_contradiction = any(
        c["analysis"].get("has_contradiction") for c in contradictions
    )
    sections.append("## 一、跨季發言比對")
    if not contradictions:
        sections.append("> 資料不足，無法完成跨季比對（請確認已匯入多季法說會）")
    else:
        for c in contradictions:
            a = c["analysis"]
            icon = "🚨" if a.get("has_contradiction") else "✅"
            sections.append(
                f"### {icon} {c['quarter_a']} vs {c['quarter_b']}"
            )
            sections.append(f"- **立場變化**：{a.get('stance_change', '-')}")
            sections.append(f"- **具體改變**：{a.get('change_detail', '-')}")
            if a.get("evidence_early"):
                sections.append(f"- **{c['quarter_a']} 原文**：「{a['evidence_early']}」")
            if a.get("evidence_later"):
                sections.append(f"- **{c['quarter_b']} 原文**：「{a['evidence_later']}」")
            if a.get("follow_up_question"):
                sections.append(f"- **💡 建議追問**：{a['follow_up_question']}")
            sections.append("")

    # 承諾追蹤
    sections.append("## 二、承諾兌現追蹤")
    if not promises:
        sections.append("> 未偵測到可追蹤的前瞻承諾")
    else:
        for p in promises:
            sections.append(
                f"- {p['status']} [{p['promise_quarter']} 承諾] **{p['content']}**  \n"
                f"  後續（{p['followup_quarter']}）：{p['detail']}"
            )
    sections.append("")

    # 即時新聞補充
    if news:
        sections.append("## 三、即時新聞背景")
        for n in news[:3]:
            # [f] URL 白名單驗證：只允許 http/https 協定，防止 javascript:/data: URI 注入
            raw_url = str(n.get("url", ""))
            safe_url = raw_url if raw_url.startswith(("https://", "http://")) else ""
            title = re.sub(r"[\[\]()]", "", str(n.get("title", "")))  # 移除 Markdown link 特殊字元
            if safe_url:
                sections.append(f"- [{title}]({safe_url}) — {n.get('published_date', '')}")
            else:
                sections.append(f"- {title} — {n.get('published_date', '')}")
        sections.append("")

    # 股價資訊
    if stock and "error" not in stock:
        sections.append("## 四、股價參考")
        sections.append(
            f"- 現價：{stock.get('current_price')} {stock.get('currency')}　"
            f"52週高：{stock.get('52w_high')}　52週低：{stock.get('52w_low')}　"
            f"近期漲跌：{stock.get('recent_change_pct')}%"
        )
        sections.append("")

    # 來源列表
    sections.append("## 來源索引")
    seen_sources = set()
    for quarter, chunks in retrieved.items():
        for chunk in chunks:
            payload = chunk.get("payload", {})
            src = f"{payload.get('source_file', '未知檔案')} 第 {payload.get('source_page', '?')} 頁"
            if src not in seen_sources:
                sections.append(f"- [{quarter}] {src}")
                seen_sources.add(src)

    final_report = "\n".join(sections)
    log.append(f"  ✅ 報告生成完成（{len(final_report)} 字）")

    return {"final_report": final_report, "steps_log": log}


# ══════════════════════════════════════════════════════════════════════════════
# 條件邊：決定 Self-Reflection 後要重查還是輸出
# ══════════════════════════════════════════════════════════════════════════════
def should_continue(state: AgentState) -> str:
    """
    LangGraph 條件邊函數。
    信心度 < 0.75 且迭代次數 < 3 → retry（跳回 retrieve）
    否則 → end（進入 report）
    """
    confidence = state.get("confidence", 1.0)
    iteration = state.get("iteration", 0)

    if confidence < 0.75 and iteration < 3:
        return "retry"
    return "end"
