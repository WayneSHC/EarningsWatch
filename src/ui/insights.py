"""
src/ui/insights.py
決策導向摘要 — 把 agent 原始輸出轉成「八秒內拿到方向」的裁決資料。

設計動機（資深資產管理人 UI/UX 審查結論）：
  - 指標卡要先給「方向」（語氣軌跡），系統遙測（模型信心度）退居分析過程頁
  - 矛盾發現須按「重大性 × 信心度」排序：毛利指引收回 ≠ 庫存措辭微調
  - 承諾兌現率 = 管理層信用記分卡的原子指標
  - 全部為純函數、零 Streamlit 依賴 → pytest 可直接離線測試
"""

from __future__ import annotations

# ── 語氣軌跡 ──────────────────────────────────────────────────────────────────

_DIRECTION = {1: ("樂觀", "↗"), -1: ("保守", "↘")}


def summarize_trajectory(series: list[dict]) -> dict:
    """
    從 build_stance_series 輸出計算「最近的連續轉向」。

    只看 is_relevant 的季度（無關/boilerplate 佔位不參與判向）。
    Returns:
        {"direction": 樂觀|保守|穩定|無資料, "arrow": ↗|↘|→|—,
         "streak": int, "label": 顯示字串}
    """
    relevant = [e for e in (series or []) if e.get("is_relevant")]
    if not relevant:
        return {"direction": "無資料", "arrow": "—", "streak": 0,
                "label": "語氣軌跡：資料不足"}

    last_delta = relevant[-1].get("delta", 0)
    if last_delta == 0:
        return {"direction": "穩定", "arrow": "→", "streak": 0,
                "label": "語氣穩定 →"}

    streak = 0
    for e in reversed(relevant):
        if e.get("delta", 0) == last_delta:
            streak += 1
        else:
            break

    direction, arrow = _DIRECTION[1 if last_delta > 0 else -1]
    label = (
        f"連續 {streak} 季轉{direction} {arrow}"
        if streak >= 2 else f"最近一季轉{direction} {arrow}"
    )
    return {"direction": direction, "arrow": arrow, "streak": streak, "label": label}


# ── 承諾兌現（管理層信用記分卡）──────────────────────────────────────────────

def promise_stats(promises: list[dict]) -> dict:
    """
    統計承諾兌現：status 以 emoji 開頭（✅ 達標 / ❌ 未兌現 / ⚠ 不明）。

    兌現率分母只含「已能判定」的承諾（達標 + 未兌現）；「不明」是資料不足，
    不該拉低也不該灌水信用分數。無可判定承諾時 rate 為 None。
    """
    fulfilled = sum(1 for p in promises if str(p.get("status", "")).startswith("✅"))
    missed = sum(1 for p in promises if str(p.get("status", "")).startswith("❌"))
    unclear = sum(1 for p in promises if str(p.get("status", "")).startswith("⚠"))
    decided = fulfilled + missed
    return {
        "fulfilled": fulfilled,
        "missed": missed,
        "unclear": unclear,
        "total": len(promises),
        "rate": (fulfilled / decided) if decided else None,
    }


# ── 重大性排序 ────────────────────────────────────────────────────────────────

# (標籤, 權重, 關鍵詞)：順序即優先序，第一個命中的類別生效。
# 權重反映對估值的影響力：財測/毛利 > 資本支出 > 需求/庫存/產能 > 一般敘述。
_MATERIALITY_RULES: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("財測指引", 3.0, ("財測", "指引", "guidance", "outlook", "展望")),
    ("毛利率",   3.0, ("毛利", "margin", "獲利率")),
    ("資本支出", 2.5, ("資本支出", "capex", "資本開支")),
    ("需求",     2.0, ("需求", "訂單", "demand")),
    ("庫存",     2.0, ("庫存", "inventory", "去化")),
    ("產能",     2.0, ("產能", "擴產", "capacity")),
)
_DEFAULT_TAG = ("一般敘述", 1.0)


def materiality_of(analysis: dict) -> tuple[float, str]:
    """回傳 (score, tag)。score = 主題權重 × LLM 信心度（缺值視為 0.5）。"""
    text = " ".join(
        str(analysis.get(k, "") or "")
        for k in ("change_detail", "evidence_early", "evidence_later",
                  "follow_up_question")
    ).lower()
    conf = analysis.get("confidence")
    conf = conf if isinstance(conf, (int, float)) else 0.5

    for tag, weight, keywords in _MATERIALITY_RULES:
        if any(kw.lower() in text for kw in keywords):
            return weight * conf, tag
    return _DEFAULT_TAG[1] * conf, _DEFAULT_TAG[0]


def sort_by_materiality(contradictions: list[dict]) -> list[dict]:
    """矛盾比對結果按 重大性 × 信心度 由高到低排序（穩定排序保留原次序）。"""
    return sorted(
        contradictions,
        key=lambda c: materiality_of(c.get("analysis", {}))[0],
        reverse=True,
    )


def top_follow_ups(contradictions: list[dict], n: int = 3) -> list[str]:
    """取最重要的 n 條建議追問：有效立場變化、去重、按重大性排序。"""
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for c in sort_by_materiality(contradictions):
        a = c.get("analysis", {})
        fq = str(a.get("follow_up_question", "") or "").strip()
        if not fq or fq in seen:
            continue
        if a.get("stance_change") in ("無關", "維持不變", None):
            continue
        seen.add(fq)
        scored.append((materiality_of(a)[0], fq))
    return [fq for _, fq in scored[:n]]


# ── 裁決句（executive verdict）───────────────────────────────────────────────

def build_verdict(series: list[dict], contradictions: list[dict],
                  promises: list[dict]) -> dict:
    """
    組合三行裁決：語氣方向、最重大矛盾、承諾兌現率。
    回傳值皆為原始字串（呼叫端負責 html escape）。
    """
    traj = summarize_trajectory(series)

    top_finding = None
    confirmed = [c for c in contradictions
                 if c.get("analysis", {}).get("has_contradiction")]
    if confirmed:
        best = sort_by_materiality(confirmed)[0]
        a = best.get("analysis", {})
        _, tag = materiality_of(a)
        detail = str(a.get("change_detail", "") or "").strip()
        top_finding = (
            f"{best.get('quarter_a', '?')} → {best.get('quarter_b', '?')}"
            f"［{tag}］{detail}"
        )

    ps = promise_stats(promises)
    if ps["total"] == 0:
        promise_line = "無可追蹤的前瞻承諾"
    elif ps["rate"] is None:
        promise_line = f"承諾 {ps['total']} 項，後續資訊不足無法判定"
    else:
        promise_line = (
            f"承諾兌現 {ps['fulfilled']}/{ps['fulfilled'] + ps['missed']}"
            f"（{ps['rate']:.0%}）"
            + (f"，另 {ps['unclear']} 項不明" if ps["unclear"] else "")
        )

    return {"tone": traj, "top_finding": top_finding, "promise_line": promise_line}


def morning_note(company: str, topic: str, as_of: str | None,
                 verdict: dict) -> str:
    """三行純文字晨會摘要（給「複製到 IC memo / 群組」用）。"""
    lines = [
        f"【{company}・{topic}】語氣軌跡：{verdict['tone']['label']}"
        + (f"（資料截至 {as_of}）" if as_of else ""),
    ]
    if verdict.get("top_finding"):
        lines.append(f"最重大發現：{verdict['top_finding']}")
    lines.append(f"管理層信用：{verdict['promise_line']}")
    return "\n".join(lines)


# ── 季度 / 股價疊圖輔助 ──────────────────────────────────────────────────────

_Q_END = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}


def latest_quarter(quarters: list[str]) -> str | None:
    """回傳最新季度（YYYYQn 字典序 = 時間序）。"""
    return max(quarters) if quarters else None


def quarter_end_date(quarter: str) -> str | None:
    """'2024Q3' → '2024-09-30'；格式不符回 None。"""
    if len(quarter) != 6 or quarter[4:] not in _Q_END:
        return None
    return f"{quarter[:4]}-{_Q_END[quarter[4:]]}"


def index_prices(quarter_closes: dict[str, float]) -> dict[str, float]:
    """以最早季度收盤為 100 做指數化（語氣 vs 股價同圖可比）。"""
    if not quarter_closes:
        return {}
    ordered = sorted(quarter_closes)
    base = quarter_closes[ordered[0]]
    if not base:
        return {}
    return {q: quarter_closes[q] / base * 100.0 for q in ordered}
