"""
src/core/comparison.py
多公司並行分析與比較摘要。

設計：
  - 不修改 Agent 架構，直接用 ThreadPoolExecutor 並行呼叫 run_agent()
  - max_workers=2 避免同時觸發過多 LLM API 呼叫（免費 tier rate limit）
  - build_comparison_table() 對齊各公司的季度對，缺席季度顯示 "—"
  - synthesize_diff() 呼叫 LLM 生成差異摘要（輸入只取有差異的列，控制 token 數）
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from src.agent.graph import run_agent
from src.core.llm_client import chat as llm_chat


def run_multi_company(
    companies: list[str],
    topic: str,
    quarters: list[str],
    custom_query: str = "",
) -> dict[str, dict]:
    """
    並行為每家公司執行 Agent，回傳 {company: result_dict}。

    Args:
        companies: 公司名稱列表（最多 3 家，避免 rate limit）
        topic:     分析主題
        quarters:  季度過濾（空列表 = 全部）
        custom_query: 自訂問題（空字串則使用模板）

    Returns:
        {company: AgentState dict（含 contradictions, promises, final_report, confidence）}
    """
    def _run_one(company: str) -> tuple[str, dict]:
        query = custom_query or (
            f"{company} 在「{topic}」方面，各季度發言是否有矛盾或立場轉變？請追蹤承諾兌現情況。"
        )
        result = run_agent(query=query, company=company, topic=topic, quarters=quarters)
        return company, result

    results: dict[str, dict] = {}
    # cap at 2 workers：避免同時打 4+ 個 LLM API 呼叫觸發 rate limit
    max_workers = min(len(companies), 2)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_one, c): c for c in companies}
        for fut in as_completed(futures):
            company = futures[fut]
            try:
                _, result = fut.result()
                results[company] = result
            except Exception as e:
                # [f] 只記錄 type.__name__，避免完整 str(e) 含 API key 片段洩漏到 UI
                _etype = type(e).__name__
                print(f"[Comparison] {company} 分析失敗: {_etype}: {str(e)[:120]}")
                results[company] = {
                    "error": _etype,
                    "contradictions": [],
                    "promises": [],
                    "confidence": 0.0,
                    "final_report": f"⚠ {company} 分析失敗：{_etype}",
                }
    return results


def build_comparison_table(
    results: dict[str, dict],
) -> list[dict]:
    """
    將各公司的 contradictions 對齊到共同的季度對軸。

    Returns:
        [
          {"quarter_pair": "2024Q2 vs 2024Q3",
           "台積電": "更樂觀", "聯發科": "—"},
          ...
        ]
        已過濾掉所有公司都是「無關」或 "—" 的列。
    """
    companies = list(results.keys())

    all_pairs: set[str] = set()
    company_stances: dict[str, dict[str, str]] = {}

    for company, result in results.items():
        stances: dict[str, str] = {}
        for c in result.get("contradictions", []):
            stance = c.get("analysis", {}).get("stance_change", "無關")
            if stance == "無關":
                continue
            # 過濾 boilerplate
            a = c.get("analysis", {})
            ev_a = (a.get("evidence_early") or "").strip()
            ev_b = (a.get("evidence_later") or "").strip()
            if ev_a and ev_b and ev_a == ev_b:
                continue
            pair_key = f"{c.get('quarter_a', '')} vs {c.get('quarter_b', '')}"
            stances[pair_key] = stance
            all_pairs.add(pair_key)
        company_stances[company] = stances

    def _pair_sort_key(pair: str) -> tuple[str, str]:
        parts = pair.split(" vs ")
        q = parts[0] if parts else "0000Q0"
        return (q[:4], q[4:]) if len(q) >= 5 else (q, "")

    rows = []
    for pair in sorted(all_pairs, key=_pair_sort_key):
        row: dict[str, str] = {"quarter_pair": pair}
        for company in companies:
            row[company] = company_stances[company].get(pair, "—")
        rows.append(row)

    return rows


def synthesize_diff(
    comparison_table: list[dict],
    topic: str,
    companies: list[str],
) -> str:
    """
    呼叫 LLM 生成跨公司差異摘要（3–5 句）。
    只傳入有分歧的列，控制 prompt token 數。
    """
    if not comparison_table:
        return "兩家公司在此主題上資料不足，無法進行有效比較。"

    # 只取各公司立場不同的列
    differ_rows = [
        r for r in comparison_table
        if len({v for k, v in r.items() if k != "quarter_pair" and v != "—"}) > 1
    ]

    if not differ_rows:
        companies_str = " 與 ".join(companies)
        return f"在「{topic}」主題上，{companies_str} 的立場走勢高度一致，各季度方向相同，未見明顯分歧。"

    table_text = "\n".join(
        "  " + r["quarter_pair"] + ": "
        + "，".join(f"{c}={r.get(c, '—')}" for c in companies)
        for r in differ_rows[:8]   # cap 8 列，避免 token 過多
    )
    # [c] 二次截斷：不同 LLM backend context window 不同，防止較小 context 後端超限
    table_text = table_text[:1500]

    prompt = f"""以下是兩家公司針對「{topic}」的跨季立場比較表：
{table_text}

請用 3 至 5 句話總結：
1. 兩者立場的主要異同
2. 關鍵分歧在哪個季度、分歧方向
3. 對散戶投資人的一個值得注意的觀察

只輸出純文字段落，不要標題或列表。"""

    try:
        return llm_chat(prompt, max_tokens=250)
    except Exception as e:
        print(f"[Comparison] synthesize_diff 失敗: {e}")
        return "差異摘要生成失敗，請參考上方比較表。"
