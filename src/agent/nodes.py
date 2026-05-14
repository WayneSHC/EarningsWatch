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
from html import escape as _he
from statistics import mean
from typing import Any

from src.agent.state import AgentState
from src.agent.tools import decide_tools, search_news, get_stock_price
from src.core.retriever import retrieve, get_company_quarters, retrieve_coverage
from src.core.contradiction import batch_detect, detect_promises, _extract_json
from src.core.llm_client import chat as llm_chat, friendly_error_message
from src.core import telemetry


# [c] 單次 query 的 LLM 預算上限（USD）；超過則強制結束 retry 迴圈。
# 預設 0.50 美元 ≈ 一般查詢上限的 5~10 倍，保留 self-reflection 重查空間，
# 避免 LLM 反覆呼叫導致帳單失控。
_LLM_BUDGET_USD = float(os.getenv("LLM_BUDGET_USD", "0.50"))


# [A7] 判斷 LLM 生成的「直接回答」是否表達「法說會逐字稿未涵蓋此主題」。
# 命中時 report_generator 跳過跨季比對 / 承諾追蹤等對 off-topic 主題無意義的區塊，
# 並改以網路新聞做補充（必要時現場觸發 Tavily）。
_OFF_TOPIC_PHRASES = (
    "並未提及", "並未包含", "未提及", "未包含", "並未提供", "未提供",
    "沒有提及", "沒有包含", "沒有任何資訊", "沒有相關資訊",
    "並無相關", "並未涵蓋", "未涵蓋", "資料中並無", "資料中並未",
    "無法回答", "無法從", "無相關內容", "沒有相關內容",
)


def _is_off_topic_answer(text: str) -> bool:
    if not text:
        return False
    return any(p in text for p in _OFF_TOPIC_PHRASES)


# Tavily content 是從網頁直接刮下的 markdown，常見三類雜質：
#  (1) `## ### #` 等 heading 符號 → 套進 blockquote 會被 Streamlit 渲染成巨大標題
#  (2) `*` `>` `` ` `` 等其他 markdown 結構字元
#  (3) 整段都是「請啟用 JavaScript / 更新瀏覽器」之類的 SPA fallback 文字（毫無資訊量）
# _clean_news_snippet 過濾上述雜質，讓 off-topic 模式的新聞片段呈現一致格式。
_NEWS_BOILERPLATE = (
    "請更新您的瀏覽器", "請啟用 JavaScript", "請在瀏覽器上啟用", "請啟用JavaScript",
    "更新瀏覽器", "不再受支援", "Javascript is disabled",
)


def _clean_news_snippet(text: str, max_len: int = 180) -> str:
    if not text:
        return ""
    if any(p in text for p in _NEWS_BOILERPLATE):
        return ""
    # [b] 擴大過濾範圍：除了 # * ` >，再加上 = ~ _ 與 setext heading 用的 ---/===，
    #     並把 [] () 也洗掉避免被當 markdown link 殘骸。Readmo.ai 之類的爬蟲頁面
    #     會把標題、麵包屑、按鈕文字全部塞進 content，混雜大量結構字元。
    cleaned = re.sub(r"[#*`>=~_\[\]]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


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
        # [P2] topic 不再限定 6 項清單；改為依問題語意自由提煉，僅給範例參考。
        #      下游 batch_detect / decompose / synthesize_diff 都把 topic 當自由字串使用，
        #      free-form 不會破壞 pipeline；改善處：UI 不必強迫使用者從固定下拉選擇。
        resp = _llm(f"""從以下問題萃取資訊，只回傳 JSON：
問題：{state['query']}

主題萃取規則（重要）：
- 問題中若出現英文縮寫或技術名詞（例如 CoWoS / CoPoS / HBM / FinFET / N2），
  主題必須原樣沿用該縮寫，可調整大小寫成業界標準寫法
- 嚴禁把使用者輸入的縮寫替換成「不同字母組成的相似詞」
  （例：不可把 CoPoS 改成 CoWoS；不可把 HBM4 改成 HBM3）
- 主題長度：2~8 字短語，可純中文 / 純英文縮寫 / 中英混合

{{
  "company": "公司名（台積電/聯發科/鴻海/台達電）",
  "topic": "依問題語意提煉的主題短語，例：AI需求 / 毛利率 / 資本支出 / 庫存調整",
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
    # [P1-6] 快照進入時的累計 cost_usd 作為基線；should_continue 用此計算
    # 本次 query 的實際支出（避免 sibling query 干擾）
    cost_baseline = telemetry.summary().get("estimated_cost_usd", 0.0)
    return {
        "company": company,
        "topic": topic,
        "quarters": quarters,
        "cost_baseline_usd": cost_baseline,
        "steps_log": log,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 節點 2：Query Decomposer
# ══════════════════════════════════════════════════════════════════════════════
_DEFAULT_SUB_QUERIES_TEMPLATE = [
    {"id": "cross_quarter", "purpose": "跨季發言比對", "tool": "bigquery"},
    {"id": "guidance", "purpose": "財務指引與承諾追蹤", "tool": "bigquery", "section_filter": "guidance"},
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
      - tool: "bigquery" 或 "tavily"
      - section_filter: 選填，限制 BigQuery section（如 "guidance"）
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
      "tool": "bigquery 或 tavily",
      "section_filter": "guidance 或 留空字串"
    }}
  ]
}}

要求：
- 至少包含 1 條 bigquery 子查詢做跨季比對
- 若主題涉及財務數字（毛利率/營收/產能），加入 1 條 section_filter=\"guidance\" 的子查詢
- 若問題涉及「最新/近期/市場」，加入 1 條 tavily 子查詢
- query 字串要包含公司名與主題，不要太籠統
- 「query 字串」中若需提到時間，必須使用上方「季度範圍」中的實際季度或年份；嚴禁自行寫入「2024」「2023」等未列出的年份

縮寫保留規則（重要）：
- 「主題」與「原始問題」中若出現英文縮寫或技術名詞（例如 CoPoS / CoWoS / HBM / FinFET / N2 / N3），
  每一條 query 字串都必須**原樣沿用該縮寫**，可調整大小寫成業界標準寫法
- 嚴禁把使用者輸入的縮寫替換成「不同字母組成的相似詞」
  （例：上方主題若是 CoPoS，所有子查詢都必須寫 CoPoS，不可改成 CoWoS；不可把 HBM4 改成 HBM3）
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
            tool = str(sq.get("tool", "bigquery")).strip().lower()
            if not q_str or tool not in ("bigquery", "tavily"):
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
    - BigQuery 向量搜尋、Tavily 新聞、yfinance 股價三路同時發出，互不阻塞
    - 知識庫結果按季度分組，便於 Contradiction Detector 跨季比對
    - 各子任務獨立捕捉例外，單一失敗不影響其他查詢
    """
    log = [f"📚 **檢索知識庫**（第 {state.get('iteration', 0) + 1} 輪）"]
    company = state["company"]
    quarters_filter = state.get("quarters", []) or None
    tool_plan = state.get("tool_plan", ["bigquery"])
    sub_queries = state.get("sub_queries", [])
    iteration = state.get("iteration", 0)

    # [b] retry 時以前輪結果為基底，避免覆蓋已取到的資料；
    # 新一輪若工具成功則覆寫，失敗時保留舊值
    retrieved: dict[str, list[dict]] = {
        q: list(chunks) for q, chunks in state.get("retrieved", {}).items()
    }
    news_context: list[dict] = list(state.get("news_context") or [])
    stock_data: dict = dict(state.get("stock_data") or {})

    # [R4] 自適應 top_k：依問題範圍調整每條 sub_query 取回的 chunk 數
    #   - 跨季比對需要更多 chunks 餵給 Contradiction Detector
    #   - 重試輪（iteration > 0）擴大搜尋範圍，提高補強成功率
    #   - coverage_fill 類查詢專攻單一弱季，給更多 candidate
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
        # [A5+] coverage_fill：單一弱季專屬查詢，多取 candidate 提高補強成功率
        if sq.get("tool_hint") == "coverage_fill":
            base = 8
        # 重試時擴大搜尋（給 reflect-driven gap 查詢更多候選）
        if iteration > 0:
            base = min(12, base + 2)
        return base

    # ── 定義各工具的任務函式 ──────────────────────────────────────────
    def _do_bigquery(sq: dict) -> list[dict]:
        """單一 bigquery sub_query 的檢索，供 ThreadPoolExecutor 呼叫。"""
        # [f] 使用者選定季度時，所有子查詢（含 cross_quarter）都必須遵守；
        # 只有使用者未指定季度（quarters_filter=None）時，cross_quarter 才查全部季度。
        # [A5+] target_quarter 是 self_reflect 標記的弱季專屬查詢；
        #     蓋過 quarters_filter，把火力集中到該季提高補強成功率。
        target_q = sq.get("target_quarter")
        sq_quarters = [target_q] if target_q else quarters_filter
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
    # [c] 實際任務數：最多 2 bigquery + 1 tavily + 1 yfinance = 4
    # 過多 worker 浪費資源；動態計算避免未來工具變更時需要手動更新
    _n_workers = min(len([sq for sq in sub_queries if sq.get("tool") == "bigquery"]) + 2, 6)
    with ThreadPoolExecutor(max_workers=_n_workers) as executor:
        if "bigquery" in tool_plan:
            for sq in sub_queries:
                if sq.get("tool") == "bigquery":
                    fut = executor.submit(_do_bigquery, sq)
                    futures_map[fut] = ("bigquery", sq)

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

            if kind == "bigquery":
                for chunk in result:
                    quarter = chunk.get("payload", {}).get("quarter", "unknown")
                    # [b] 依 chunk id 去重，避免 retry 輪重複追加相同 chunk
                    # c.get("id") 而非 c["id"]，容忍缺 id 的邊緣資料不 KeyError
                    _seen_ids = {c.get("id") for c in retrieved.get(quarter, [])}
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

    # ── [A5] 建立覆蓋矩陣：每季資料品質指標 ──────────────────────────────
    coverage_matrix: dict = {}
    for q, chunks in retrieved.items():
        scores = [
            c.get("score", 0.0) for c in chunks
            if isinstance(c.get("score"), (int, float))
        ]
        pages = sorted({
            c.get("payload", {}).get("source_page")
            for c in chunks
            if c.get("payload", {}).get("source_page") is not None
        })[:5]
        # 若本季在矛盾比對中有引文驗證失敗，標記 quote_verified=False
        q_failed = any(
            c.get("analysis", {}).get("verification_failed")
            for c in contradictions
            if c.get("quarter_a") == q or c.get("quarter_b") == q
        )
        top_excerpt = ""
        if chunks:
            top_excerpt = (chunks[0].get("payload", {}).get("content", "") or "")[:80]
        coverage_matrix[q] = {
            "chunk_count": len(chunks),
            "max_score":   round(max(scores), 3) if scores else 0.0,
            "avg_score":   round(sum(scores) / len(scores), 3) if scores else 0.0,
            "source_pages": pages,
            "quote_verified": not q_failed,
            "top_excerpt": top_excerpt,
        }

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

    # 格式化覆蓋矩陣給 LLM judge 參考
    cov_lines = []
    for q_key, info in sorted(coverage_matrix.items()):
        v_str = "✓已驗證" if info["quote_verified"] else "⚠引文未驗證"
        cov_lines.append(
            f"  {q_key}: {info['chunk_count']}段 "
            f"max={info['max_score']} avg={info['avg_score']} "
            f"pages={info['source_pages']} quote={v_str} "
            f"excerpt=「{info['top_excerpt']}」"
        )
    coverage_str = "\n".join(cov_lines) if cov_lines else "（無覆蓋資料）"

    prompt = f"""你是嚴謹的研究品管員。評估下列分析是否足以回答使用者問題。

使用者問題：{query}
公司：{company}　主題：{topic}
已檢索季度：{quarters}（共 {chunk_count} 段）

各季覆蓋品質：
{coverage_str}

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
- 1.0：跨季資料完整、引文已驗證、矛盾結論信心度高、能直接回答問題
- 0.7~0.9：可回答但有小缺漏或部分引文為模糊匹配
- 0.5~0.7：覆蓋不足、引文驗證失敗或某些立場判斷可疑
- < 0.5：嚴重資訊不足、多季引文驗證失敗
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

    # [A3] 用 gaps + coverage_matrix 構造新 sub_queries
    new_sub_queries = state.get("sub_queries", [])
    do_retry = (score < 0.75 or should_retry_llm) and iteration < 3
    if do_retry:
        log.append(f"  🔄 觸發重查（第 {iteration + 1} 次）")

        # [A3+] (1) gap-driven：依 LLM 指出的缺漏主題產生通用查詢
        # tool_hint：含「新聞/市場/最新/外部/競爭/產業」或前瞻性詞彙 → tavily；
        # 因法說會逐字稿只涵蓋過去發言，未來展望類 gap 需要外部新聞補充。
        _NEWS_KWS = (
            "新聞", "市場", "最新", "外部", "競爭", "產業",
            "未來", "展望", "預期", "預測", "前景",
        )
        rebuilt: list[dict] = []
        for i, gap in enumerate(gaps):
            tool = "tavily" if any(kw in gap for kw in _NEWS_KWS) else "bigquery"
            rebuilt.append({
                "id": f"gap_{i}",
                "query": f"{company} {gap}",
                "purpose": f"補強：{gap[:20]}",
                "tool": tool,
                "tool_hint": "gap_fill",
            })

        # [A5+] (2) coverage-driven：用 coverage_matrix 找出資料品質弱的季度，
        #     對每個弱季產生季度限定的重查（target_quarter）。
        #     弱季定義：chunk_count<2 或 max_score<0.4 或 引文驗證失敗。
        #     原因：LLM 的 gaps 偏向「主題」缺漏，但實際常見問題是「特定季度」
        #     檢索分數過低；季度導向重查能直接補強這類 case，與 gap fill 互補。
        weak_quarters: list[str] = []
        for q_key, info in sorted(coverage_matrix.items()):
            if (
                info["chunk_count"] < 2
                or info["max_score"] < 0.4
                or not info["quote_verified"]
            ):
                weak_quarters.append(q_key)

        # 上限 3 個避免重查 sub_queries 過多
        for i, wq in enumerate(weak_quarters[:3]):
            rebuilt.append({
                "id": f"weak_{wq}_{i}",
                # 用更廣的查詢字串覆蓋該季（提高 recall）
                "query": f"{company} {wq} {topic} 發言重點",
                "purpose": f"補強弱季 {wq}",
                "tool": "bigquery",
                "tool_hint": "coverage_fill",
                "target_quarter": wq,  # parallel_retrieval 會用此覆蓋 quarters_filter
            })
        if weak_quarters[:3]:
            log.append(f"  → coverage 發現弱季：{weak_quarters[:3]}")

        if rebuilt:
            new_sub_queries = rebuilt
            log.append(
                f"  → 重建 {len(rebuilt)} 條 sub_queries"
                f"（gap={len(gaps)}, weak={min(len(weak_quarters), 3)}）"
            )
        else:
            # 無 gaps 且無弱季 → 沿用原 sub_queries（避免引入雜訊）
            log.append("  → 無 gaps 且無弱季，沿用原 sub_queries 重試")

    # [A6] 棄權路徑：重試次數用盡且信心度仍嚴重不足（< 0.4）。
    # 避免 report_generator 用極少資料生成不可靠報告，對使用者造成誤導。
    # 門檻 0.4（非 0.75）：預留空間——0.4~0.75 的報告雖有缺漏但仍有參考價值。
    abstain = False
    if not do_retry and score < 0.4:
        abstain = True
        log.append(
            f"  ❌ 三輪後資料嚴重不足（信心度 {score:.2f} < 0.40），"
            "進入棄權模式，不生成可能誤導的報告"
        )

    # [P1-6] Cost Guard：本次 query 累計支出 ≥ 預算就強制結束 retry。
    # 計算用 (current - baseline) 隔離出本次 query 的實際支出，
    # 避免 multi-company 並行時誤算到 sibling query 的成本。
    cost_guard = False
    baseline = state.get("cost_baseline_usd", 0.0)
    spent = telemetry.summary().get("estimated_cost_usd", 0.0) - baseline
    if do_retry and spent >= _LLM_BUDGET_USD:
        cost_guard = True
        log.append(
            f"  💸 cost guard：本次已花費 ${spent:.4f} ≥ 預算 ${_LLM_BUDGET_USD:.2f}，"
            "停止 retry，以現有資料生成報告"
        )

    return {
        "confidence": score,
        "reflection_issues": issues,
        "reflection_gaps": gaps,
        "iteration": iteration + 1,
        "sub_queries": new_sub_queries,
        "coverage_matrix": coverage_matrix,
        "abstain": abstain,
        "cost_guard_triggered": cost_guard,
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

    # [A6] 棄權路徑：資料嚴重不足，輸出說明訊息取代可能誤導的分析報告
    if state.get("abstain"):
        issues = state.get("reflection_issues", [])
        gaps = state.get("reflection_gaps", [])
        abstain_sections = [
            "# ⚠️ EarningsWatch — 資料不足，無法完成分析",
            f"**公司**：{company}　**主題**：{topic}　"
            f"**分析信心度**：{confidence:.0%}（低於可靠門檻）",
            "",
            "## 系統說明",
            "",
            "三輪查詢後，仍未取得足夠的法說會資料來可靠回答您的問題。"
            "為避免生成可能誤導的分析，系統選擇不輸出報告。",
            "",
        ]
        if issues:
            abstain_sections.append("**發現的問題：**")
            for issue in issues:
                abstain_sections.append(f"- {issue}")
            abstain_sections.append("")
        if gaps:
            abstain_sections.append("**仍缺乏的資訊（供參考）：**")
            for gap in gaps:
                abstain_sections.append(f"- {gap}")
            abstain_sections.append("")
        abstain_sections += [
            "**建議：**",
            "- 確認相關季度的 PDF 已匯入並完成 embedding（`python scripts/run_ingestion.py`）",
            "- 嘗試縮小查詢範圍（在左側選單指定特定季度）",
            "- 更換查詢主題關鍵字（如「AI需求」→「CoWoS 先進封裝」）",
        ]
        final_report = "\n".join(abstain_sections)
        log.append(f"  ⚠ 棄權報告生成（信心度 {confidence:.2f}）")
        return {"final_report": final_report, "steps_log": log}

    # ── 組裝報告內容 ───────────────────────────────────────────────────
    sections = [
        f"# 🕵️ EarningsWatch 偵查報告",
        f"**公司**：{company}　**主題**：{topic}　**分析信心度**：{confidence:.0%}",
        "",
    ]

    # [P1-6] cost guard 觸發時加上提示，讓使用者知道報告非完整收斂結果
    if state.get("cost_guard_triggered"):
        sections.append(
            "> 💸 **預算保護觸發**：本次查詢的 LLM 成本已達上限，"
            "Self-Reflection 提早結束 retry，報告基於目前可用資料生成。"
        )
        sections.append("")

    # ── 直接回答使用者問題 ──────────────────────────────────────────────
    # 彙整所有有主題相關內容的季度 chunks，送 LLM 合成一段直接回答。
    # 只使用分數最高的 chunk（retrieved 已按 score 降序），每季取前 2 筆，
    # 限制總長度避免 token 超限。
    user_query = state.get("query", "")
    direct_answer_text = ""  # [A7] 保留供 off-topic 判斷使用
    if user_query and retrieved:
        all_chunks: list[str] = []
        for q_label in sorted(retrieved.keys()):
            for chunk in retrieved[q_label][:2]:
                c_text = chunk.get("payload", {}).get("content", "").strip()
                if c_text:
                    all_chunks.append(f"[{q_label}] {c_text}")
        combined = "\n\n".join(all_chunks)[:4000]  # [c] 防 token 超限

        if combined:
            direct_answer_prompt = (
                f"你是台積電法說會研究員。以下是從多季法說會逐字稿中擷取的相關段落：\n\n"
                f"{combined}\n\n"
                f"請根據上述資料，用繁體中文直接、具體地回答以下問題（150-300字）。"
                f"只引用上方資料所涵蓋的事實，不要推測或補充資料以外的內容。\n\n"
                f"問題：{user_query}"
            )
            try:
                direct_answer = llm_chat(direct_answer_prompt, max_tokens=600, mode="demo")
                direct_answer_text = direct_answer.strip()
                sections.append("## 直接回答")
                sections.append(_he(direct_answer_text))
                sections.append("")
                log.append("  ✅ 直接回答段落生成完成")
            except Exception as e:
                # [f] 只顯示乾淨摘要，原始 SDK 例外（含 HTTP body / trace-id）只進 stdout
                _msg = friendly_error_message(e)
                print(f"[Nodes] direct_answer LLM 失敗: {type(e).__name__}: {str(e)[:200]}")
                log.append(f"  ⚠ 直接回答生成失敗：{_msg}")
                sections.append("## 直接回答")
                sections.append(
                    f"> ⚠️ 暫時無法生成直接回答（{_msg}）。"
                    "下方仍提供跨季比對與承諾追蹤分析，可作為參考。"
                )
                sections.append("")

    # [A7] 主題未涵蓋判斷：direct_answer 表達「資料中未提及」時，
    # 或知識庫完全無命中（retrieved 全空）時，
    # 跨季比對 / 承諾追蹤 / 趨勢分析在此主題下都是雜訊，改以網路新聞補充。
    # [b] 補上 `total_chunks == 0` 分支：BigQuery 對主題（如 CoPoS 未來獲利）
    #     完全無命中時，direct_answer 不會被生成，原本判斷因此漏掉。
    total_chunks = sum(len(v) for v in retrieved.values()) if retrieved else 0
    off_topic = _is_off_topic_answer(direct_answer_text) or total_chunks == 0
    if off_topic:
        log.append("  ⚠ direct_answer 判定主題未涵蓋於法說會逐字稿，跳過跨季比對與承諾追蹤")
        sections.append(
            "> ⚠️ **本主題未在法說會逐字稿中找到相關內容**　"
            "跨季比對 / 承諾追蹤 / 趨勢分析在此主題下無意義，"
            "以下改以網路新聞作為補充。"
        )
        sections.append("")
        # 若先前的 tool router 未觸發 Tavily（news_context 為空），現場補一次。
        if not news:
            try:
                news = search_news(user_query, company)
                if news:
                    log.append(f"  → 主題未涵蓋 → 現場觸發 Tavily 補強：{len(news)} 篇")
                else:
                    log.append("  → 主題未涵蓋 → Tavily 也未取得結果")
            except Exception as e:
                print(f"[Nodes] off-topic Tavily 失敗: {type(e).__name__}: {e}")
        # 把新聞提前到主要位置呈現，並附上摘要片段。
        if news:
            sections.append("## 一、網路新聞補充")
            sections.append("")  # [b] 與標題隔一個空行，確保下方 list 起始乾淨
            for n in news[:5]:
                raw_url = str(n.get("url", ""))
                safe_url = raw_url if raw_url.startswith(("https://", "http://")) else ""
                title = re.sub(r"[\[\]()]", "", str(n.get("title", "")))
                pub = n.get("published_date", "")
                # [b] 標題與摘要用「軟換行」（行尾兩空白）接成同一個 list item paragraph，
                #     避免 4-space 縮排在某些 markdown renderer 被當 indented code block，
                #     CJK 字體就會被放大成像 heading 的尺寸（這次 Readmo.ai 那則的成因）。
                head_line = (
                    f"- [{title}]({safe_url})" if safe_url else f"- {title}"
                ) + (f" — {pub}" if pub else "")
                snippet = _clean_news_snippet(str(n.get("content", "")))
                if snippet:
                    sections.append(head_line + "  ")  # trailing 2 spaces = hard break
                    sections.append(f"  {_he(snippet)}")  # 2-space indent = paragraph continuation
                else:
                    sections.append(head_line)
            sections.append("")
        else:
            sections.append("> 網路新聞搜尋也未取得結果，建議調整查詢主題或關鍵字。")
            sections.append("")
        # 股價資訊（若有）仍保留作為市場反應參考
        if stock and "error" not in stock:
            sections.append("## 二、股價參考")
            sections.append(
                f"- 現價：{stock.get('current_price')} {stock.get('currency')}　"
                f"52週高：{stock.get('52w_high')}　52週低：{stock.get('52w_low')}　"
                f"近期漲跌：{stock.get('recent_change_pct')}%"
            )
            sections.append("")
        final_report = "\n".join(sections)
        log.append(f"  ✅ 報告生成完成（off-topic 模式，{len(final_report)} 字）")
        return {"final_report": final_report, "news_context": news, "steps_log": log}

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
            # [f] defense-in-depth：LLM 回傳字串用 html.escape 處理，
            #     雖 final_report 目前以 st.markdown()（無 unsafe_allow_html）渲染，
            #     但提前轉義可防止未來渲染方式改變時的 XSS 風險。
            sections.append(f"- **立場變化**：{_he(a.get('stance_change', '-'))}")
            sections.append(f"- **具體改變**：{_he(a.get('change_detail', '-'))}")
            if a.get("evidence_early"):
                cite_a = ""
                if c.get("sources_a"):
                    s = c["sources_a"][0]
                    cite_a = f"  ＜{_he(str(s.get('file','')))} p.{s.get('page','?')}＞"
                # [f] 模糊匹配通過的引文加 `～` 提示原文措辭可能略有差異
                fuzzy_a = "～" if a.get("evidence_early_fuzzy") else ""
                sections.append(
                    f"- **{c['quarter_a']} 原文**：「{_he(a['evidence_early'])}」{fuzzy_a}{cite_a}"
                )
            if a.get("evidence_later"):
                cite_b = ""
                if c.get("sources_b"):
                    s = c["sources_b"][0]
                    cite_b = f"  ＜{_he(str(s.get('file','')))} p.{s.get('page','?')}＞"
                fuzzy_b = "～" if a.get("evidence_later_fuzzy") else ""
                sections.append(
                    f"- **{c['quarter_b']} 原文**：「{_he(a['evidence_later'])}」{fuzzy_b}{cite_b}"
                )
            if a.get("follow_up_question"):
                sections.append(f"- **💡 建議追問**：{_he(a['follow_up_question'])}")
            sections.append("")

    # 承諾追蹤
    sections.append("## 二、承諾兌現追蹤")
    if not promises:
        sections.append("> 未偵測到可追蹤的前瞻承諾")
    else:
        for p in promises:
            # [f] defense-in-depth：承諾內容與判斷說明來自 LLM，提前轉義
            sections.append(
                f"- {p['status']} [{p['promise_quarter']} 承諾] "
                f"**{_he(p['content'])}**  \n"
                f"  後續（{p['followup_quarter']}）：{_he(p['detail'])}"
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
    LangGraph 條件邊函數（純函數，不 mutate state）。

    Retry 條件（必須同時滿足）：
      1. 信心度 < 0.75
      2. 迭代次數 < 3
      3. cost_guard_triggered = False（self_reflect 已負責設定此旗標）
    """
    confidence = state.get("confidence", 1.0)
    iteration = state.get("iteration", 0)
    if state.get("cost_guard_triggered"):
        return "end"
    if confidence < 0.75 and iteration < 3:
        return "retry"
    return "end"
