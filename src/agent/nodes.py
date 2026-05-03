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
from src.core.contradiction import batch_detect, detect_promises, _extract_json
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
            # [b] 統一用 _extract_json，支援 markdown fence / 巢狀結構，比 re.search 穩
            parsed = _extract_json(resp) or {}
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
_DEFAULT_SUB_QUERIES_TEMPLATE = [
    {"id": "cross_quarter", "purpose": "跨季發言比對", "tool": "qdrant"},
    {"id": "guidance", "purpose": "財務指引與承諾追蹤", "tool": "qdrant", "section_filter": "guidance"},
    {"id": "news", "purpose": "即時新聞背景", "tool": "tavily"},
]


def _fallback_sub_queries(company: str, topic: str) -> list[dict]:
    """LLM 失敗時的保底拆解（與舊版相同的 3 條樣板）。"""
    return [
        {**_DEFAULT_SUB_QUERIES_TEMPLATE[0], "query": f"{company} {topic} 發言 各季比對"},
        {**_DEFAULT_SUB_QUERIES_TEMPLATE[1], "query": f"{company} {topic} 預估 指引 guidance"},
        {**_DEFAULT_SUB_QUERIES_TEMPLATE[2], "query": f"{company} {topic} 最新動態"},
    ]


def query_decomposer(state: AgentState) -> dict:
    """
    [A1] LLM 驅動的子問題拆解：根據實際問題語意產生 2~5 條子查詢，
    而非套用固定樣板。LLM 失敗時降級為樣板。

    每條子查詢需指定：
      - query: 實際送進 retriever / Tavily 的查詢字串
      - purpose: 該子查詢要解決的問題（log 顯示）
      - tool: "qdrant" 或 "tavily"
      - section_filter: 選填，限制 Qdrant section（如 "guidance"）
    """
    company = state.get("company", "")
    topic   = state.get("topic", "")
    query   = state.get("query", "")
    quarters = state.get("quarters", []) or []
    log = [f"📋 **分解子問題**（LLM 驅動）"]

    # [b] 把使用者選的季度 / 推導出的年份範圍餵進 prompt，
    #     避免 LLM 在 query 字串裡自行套用「2024」這類預設年份。
    if quarters:
        years = sorted({q[:4] for q in quarters if isinstance(q, str) and len(q) >= 4})
        scope_str = f"季度範圍：{', '.join(quarters)}（年份：{', '.join(years) or '未知'}）"
    else:
        scope_str = "季度範圍：使用者未指定 → 應做全部季度的跨季比對，子查詢 query 字串「不要」寫入任何特定年份"

    prompt = f"""你是財務分析師。將下列問題拆解為 2~5 條子查詢，覆蓋不同分析角度（跨季比對、財務指引、競爭/產業背景、最新動態等）。

公司：{company}
主題：{topic}
{scope_str}
原始問題：{query}

只回傳 JSON（不要其他文字）：
{{
  "sub_queries": [
    {{
      "id": "唯一識別字串（英數）",
      "query": "送進向量檢索或新聞 API 的實際查詢字串（中文，10~30字）",
      "purpose": "這條子查詢的目的（10字內）",
      "tool": "qdrant 或 tavily",
      "section_filter": "guidance 或 留空字串"
    }}
  ]
}}

要求：
- 至少包含 1 條 qdrant 子查詢做跨季比對
- 若主題涉及財務數字（毛利率/營收/產能），加入 1 條 section_filter=\"guidance\" 的子查詢
- 若問題涉及「最新/近期/市場」，加入 1 條 tavily 子查詢
- query 字串要包含公司名與主題，不要太籠統
- 「query 字串」中若需提到時間，必須使用上方「季度範圍」中的實際季度或年份；嚴禁自行寫入「2024」「2023」等未列出的年份
"""

    sub_queries: list[dict] = []
    try:
        raw = _llm(prompt, max_tokens=400)
        parsed = _extract_json(raw)
        candidates = parsed.get("sub_queries", []) if isinstance(parsed, dict) else []
        for sq in candidates:
            if not isinstance(sq, dict):
                continue
            q_str = str(sq.get("query", "")).strip()
            tool = str(sq.get("tool", "qdrant")).strip().lower()
            if not q_str or tool not in ("qdrant", "tavily"):
                continue
            entry = {
                "id": str(sq.get("id", f"sq_{len(sub_queries)}")),
                "query": q_str[:120],  # [f] 防 prompt 爆量
                "purpose": str(sq.get("purpose", ""))[:40] or "子查詢",
                "tool": tool,
            }
            sf = str(sq.get("section_filter", "")).strip()
            if sf:
                entry["section_filter"] = sf
            sub_queries.append(entry)
    except Exception as e:
        log.append(f"  ⚠ LLM 拆解失敗（{type(e).__name__}），降級為樣板")

    if not sub_queries:
        sub_queries = _fallback_sub_queries(company, topic)
        log.append("  → 使用樣板拆解")

    for sq in sub_queries:
        log.append(f"  → [{sq['purpose']}] {sq['query']}")

    return {"sub_queries": sub_queries, "steps_log": log}


# ══════════════════════════════════════════════════════════════════════════════
# 節點 3：Dynamic Tool Router
# ══════════════════════════════════════════════════════════════════════════════
def dynamic_tool_router(state: AgentState) -> dict:
    """
    [A2] LLM 驅動的工具選擇（知識庫 / Tavily / yfinance）。
    decide_tools 內部以工具規格表（TOOL_SPECS）讓 LLM 判斷工具相關性，
    LLM 失敗時自動降級為關鍵字匹配。
    """
    tools = decide_tools(state["query"], state["topic"])
    log = [f"🔧 **工具選擇**（LLM 規劃）：{', '.join(tools)}"]
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
    iteration = state.get("iteration", 0)

    # [b] retry 時以前輪結果為基底，避免覆蓋已取到的季度與 chunks
    retrieved: dict[str, list[dict]] = {
        q: list(chunks) for q, chunks in state.get("retrieved", {}).items()
    }
    news_context: list[dict] = []
    stock_data: dict = {}

    # [R4] 自適應 top_k：依問題範圍調整每條 sub_query 取回的 chunk 數
    #   - 跨季比對需要更多 chunks 餵給 Contradiction Detector
    #   - 重試輪（iteration > 0）擴大搜尋範圍，提高補強成功率
    #   - 上限 12 防止單次檢索 payload 過大
    def _adaptive_top_k(sq: dict) -> int:
        base = 5
        # 跨季比對：依使用者選的季度數放大（無指定時假設最多比 6 季）
        if sq.get("id") == "cross_quarter" or not sq.get("section_filter"):
            n_quarters = len(quarters_filter) if quarters_filter else 6
            base = max(5, min(10, n_quarters + 2))
        # guidance 類查詢通常 chunk 集中，3~4 已足夠
        if sq.get("section_filter") == "guidance":
            base = 4
        # 重試時擴大搜尋（給 reflect-driven gap 查詢更多候選）
        if iteration > 0:
            base = min(12, base + 2)
        return base

    # ── 定義各工具的任務函式 ──────────────────────────────────────────
    def _do_qdrant(sq: dict) -> list[dict]:
        """單一 qdrant sub_query 的檢索，供 ThreadPoolExecutor 呼叫。"""
        # [f] 使用者選定季度時，所有子查詢（含 cross_quarter）都必須遵守；
        # 只有使用者未指定季度（quarters_filter=None）時，cross_quarter 才查全部季度。
        sq_quarters = quarters_filter
        return retrieve(
            query=sq["query"],
            company=company,
            quarters=sq_quarters,
            section=sq.get("section_filter"),
            top_k=_adaptive_top_k(sq),
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
                    # [b] 依 chunk id 去重，避免 retry 輪重複追加相同 chunk
                    _seen_ids = {c["id"] for c in retrieved.get(quarter, [])}
                    if chunk.get("id") not in _seen_ids:
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
def _hard_floor_checks(retrieved: dict, contradictions: list) -> tuple[float, list[str]]:
    """
    [A4] LLM judge 之外的硬性檢查，作為 floor。
    LLM 若給高分但這些底線不滿足，仍視為低品質。
    回傳 (penalty, issues)：penalty 為扣分，issues 為人類可讀的問題清單。
    """
    penalty = 0.0
    issues: list[str] = []
    if len(retrieved) < 2:
        issues.append("只找到 1 季資料，需要至少 2 季才能跨季比對")
        penalty += 0.4
    total_chunks = sum(len(v) for v in retrieved.values())
    if total_chunks < 3:
        issues.append(f"檢索到的相關段落過少（{total_chunks} 個）")
        penalty += 0.2
    if contradictions:
        confs = [
            c["analysis"].get("confidence", 0.5)
            for c in contradictions if c.get("analysis")
        ]
        if confs and mean(confs) < 0.5:
            issues.append(f"矛盾偵測平均信心度過低（{mean(confs):.2f}）")
            penalty += 0.2
    return penalty, issues


def self_reflect(state: AgentState) -> dict:
    """
    [A4] LLM-as-judge 自我評估：取代規則式扣分。
    LLM 同時回傳：
      - score（0~1）：整體可回答度
      - issues：發現的具體問題
      - gaps：缺漏的資訊主題（給下一輪 retrieve 當作新 sub_queries 種子）

    [A3] retry 不再用 "+ 詳細說明"，改用 gaps 為每個缺漏主題產生一條新 sub_query。

    保留硬性 floor 檢查（資料量極端不足時即使 LLM 給高分也壓低分數）。
    """
    log = ["🤔 **自我評估（LLM judge）**"]
    retrieved = state.get("retrieved", {})
    contradictions = state.get("contradictions", [])
    promises = state.get("promises", [])
    query = state.get("query", "")
    company = state.get("company", "")
    topic = state.get("topic", "")
    iteration = state.get("iteration", 0)

    # 摘要當前證據（控制 prompt 長度）
    quarters = sorted(retrieved.keys())
    chunk_count = sum(len(v) for v in retrieved.values())
    contradiction_summary = []
    for c in contradictions[:8]:
        a = c.get("analysis", {})
        contradiction_summary.append(
            f"{c.get('quarter_a')}→{c.get('quarter_b')}: "
            f"{a.get('stance_change','-')} (conf={a.get('confidence',0):.2f})"
        )

    prompt = f"""你是嚴謹的研究品管員。評估下列分析是否足以回答使用者問題。

使用者問題：{query}
公司：{company}　主題：{topic}
已檢索季度：{quarters}（共 {chunk_count} 段）
矛盾比對結果：
{chr(10).join(contradiction_summary) if contradiction_summary else "（無）"}
承諾追蹤：{len(promises)} 條

只回傳 JSON：
{{
  "score": 0~1 之間的浮點數（整體回答品質與覆蓋度）,
  "issues": ["具體問題1", "問題2"],
  "gaps": ["缺漏的資訊主題，每條 15 字內，retrieve 會用來產生新查詢", ...],
  "should_retry": true 或 false
}}

評分準則：
- 1.0：跨季資料完整、矛盾結論信心度高、能直接回答問題
- 0.7~0.9：可回答但有小缺漏
- 0.5~0.7：覆蓋不足或某些立場判斷可疑
- < 0.5：嚴重資訊不足
gaps 應指出「retrieve 還沒抓到但對回答有幫助」的主題（如：缺乏 2024Q4 毛利率展望、未涵蓋 AI 產能 capex 規劃）。最多 4 條。
"""

    score = 0.6
    issues: list[str] = []
    gaps: list[str] = []
    should_retry_llm = False
    try:
        raw = _llm(prompt, max_tokens=400)
        parsed = _extract_json(raw)
        if isinstance(parsed, dict):
            try:
                score = float(parsed.get("score", 0.6))
            except (TypeError, ValueError):
                score = 0.6
            score = max(0.0, min(1.0, score))
            issues = [str(x)[:120] for x in (parsed.get("issues") or []) if x][:5]
            gaps = [str(x)[:60] for x in (parsed.get("gaps") or []) if x][:4]
            should_retry_llm = bool(parsed.get("should_retry", False))
    except Exception as e:
        log.append(f"  ⚠ LLM judge 失敗（{type(e).__name__}），降級為硬性檢查")

    # Floor：硬性條件不滿足時壓低分數
    penalty, hard_issues = _hard_floor_checks(retrieved, contradictions)
    if penalty > 0:
        score = max(0.0, score - penalty)
        for hi in hard_issues:
            if hi not in issues:
                issues.append(hi)

    score = round(score, 2)

    if issues:
        log.append(f"  ⚠ 信心度：{score:.2f}，發現 {len(issues)} 個問題：")
        for issue in issues:
            log.append(f"    - {issue}")
    else:
        log.append(f"  ✅ 信心度：{score:.2f}，品質達標")
    if gaps:
        log.append(f"  📌 缺漏主題：{gaps}")

    # [A3] 用 gaps 構造新 sub_queries（取代盲目加「詳細說明」）
    new_sub_queries = state.get("sub_queries", [])
    do_retry = (score < 0.75 or should_retry_llm) and iteration < 3
    if do_retry:
        log.append(f"  🔄 觸發重查（第 {iteration + 1} 次）")
        if gaps:
            new_sub_queries = [
                {
                    "id": f"gap_{i}",
                    "query": f"{company} {gap}",
                    "purpose": f"補強：{gap[:20]}",
                    "tool": "qdrant",
                }
                for i, gap in enumerate(gaps)
            ]
            log.append(f"  → 依 gaps 重建 {len(new_sub_queries)} 條 sub_queries")
        else:
            # 無 gaps（LLM 失敗）時，保留原 sub_queries（不再加「詳細說明」雜訊）
            log.append("  → 無 gaps，沿用原 sub_queries 重試")

    return {
        "confidence": score,
        "reflection_issues": issues,
        "reflection_gaps": gaps,
        "iteration": iteration + 1,
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
                cite_a = ""
                if c.get("sources_a"):
                    s = c["sources_a"][0]
                    cite_a = f"  ＜{s.get('file','')} p.{s.get('page','?')}＞"
                sections.append(f"- **{c['quarter_a']} 原文**：「{a['evidence_early']}」{cite_a}")
            if a.get("evidence_later"):
                cite_b = ""
                if c.get("sources_b"):
                    s = c["sources_b"][0]
                    cite_b = f"  ＜{s.get('file','')} p.{s.get('page','?')}＞"
                sections.append(f"- **{c['quarter_b']} 原文**：「{a['evidence_later']}」{cite_b}")
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
