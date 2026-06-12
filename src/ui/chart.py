"""
src/ui/chart.py
立場趨勢圖 — 將 contradiction 資料轉為 Plotly 時間序列視覺化。

設計決策：
  - 使用 quarter_b（較晚季度）作為 X 軸刻度：代表「觀察到立場轉變的時間點」
  - 每個 quarter_b 只出現一次（取最顯著的非零 delta；若全為無關則顯示 0）
  - 累積分數模式：累加 (+1/0/-1)，直觀反映長期樂觀/保守趨勢
  - 逐季變化模式：每季 delta bar，適合短序列（< 5 季）
    - 有方向性的 bar 用對應顏色；無關/boilerplate 的 0-delta bar 用淺灰色
  - 多公司時各用不同顏色折線，共用同一 X 軸
"""

from __future__ import annotations

import plotly.graph_objects as go

STANCE_SCORE: dict[str, int] = {
    "更樂觀": +1,
    "維持不變": 0,
    "更保守": -1,
}
STANCE_COLOR: dict[str, str] = {
    "更樂觀": "#27ae60",
    "維持不變": "#95a5a6",
    "更保守": "#e74c3c",
    "無關":   "#dfe6e9",   # 淺灰：此季度主題未被討論
}
COMPANY_COLORS = ["#1f4e79", "#c0392b", "#27ae60", "#8e44ad", "#e67e22"]


def _quarter_sort_key(q: str) -> tuple[str, str]:
    """'2024Q3' → ('2024', 'Q3')，確保字典序正確排序。"""
    return (q[:4], q[4:]) if len(q) >= 5 else (q, "")


def build_stance_series(contradictions: list[dict]) -> list[dict]:
    """
    將 contradiction 列表轉為時間序列。

    設計：
      - 以 quarter_b 為鍵去重：每個季度只出現一次
      - 若該季度有多個比對，取 abs(delta) 最大的（即最顯著的立場轉變）
      - 「無關」或 boilerplate 比對仍會在 X 軸顯示（delta=0，標記為「無關」）
        → 保持時間軸完整，不讓中間季度消失

    Returns:
        [{"quarter": "2024Q3", "delta": +1, "cumulative": 2,
          "stance": "更樂觀", "detail": "...", "is_relevant": True}, ...]
        依 quarter 排序。
    """
    # quarter_b → best entry dict
    best: dict[str, dict] = {}

    for c in contradictions:
        a = c.get("analysis", {})
        q_b = c.get("quarter_b", "")
        if not q_b:
            continue

        stance = a.get("stance_change", "無關")

        # boilerplate 偵測：evidence 完全相同
        ev_a = (a.get("evidence_early") or "").strip()
        ev_b = (a.get("evidence_later") or "").strip()
        is_boilerplate = bool(ev_a and ev_b and ev_a == ev_b)

        if is_boilerplate or stance not in STANCE_SCORE:
            # 「無關」或 boilerplate → delta=0，但仍在 X 軸佔位
            if q_b not in best:
                best[q_b] = {
                    "quarter":     q_b,
                    "delta":       0,
                    "stance":      "無關",
                    "detail":      "",
                    "is_relevant": False,
                }
            continue

        delta = STANCE_SCORE[stance]
        entry = {
            "quarter":     q_b,
            "delta":       delta,
            "stance":      stance,
            "detail":      a.get("change_detail", ""),
            "is_relevant": True,
        }

        # 優先保留 abs(delta) 最大的（更樂觀/更保守優先於維持不變）
        if q_b not in best or abs(delta) > abs(best[q_b]["delta"]):
            best[q_b] = entry

    # 依季度排序
    series = sorted(best.values(), key=lambda x: _quarter_sort_key(x["quarter"]))

    # 計算累積分數（只累加有方向性的 delta）
    cumsum = 0
    for s in series:
        cumsum += s["delta"]
        s["cumulative"] = cumsum

    return series


def render_trend_chart(
    series_by_company: dict[str, list[dict]],
    topic: str,
    mode: str = "cumulative",
    price_by_company: dict[str, dict[str, float]] | None = None,
) -> go.Figure:
    """
    Args:
        series_by_company: {"台積電": [...], "聯發科": [...]}
        topic: 主題名稱（顯示在圖標題）
        mode: "cumulative"（累積折線）或 "delta"（逐季 bar）
        price_by_company: {"台積電": {"2024Q1": 100.0, ...}} 指數化季末收盤
            （最早季 = 100），有值時於右軸疊上股價虛線 —— 語氣領先或落後
            價格，一眼可辨。只在 cumulative 模式繪製。

    Returns:
        plotly Figure，供 st.plotly_chart() 使用
    """
    fig = go.Figure()

    all_quarters = sorted(
        set(
            s["quarter"]
            for series in series_by_company.values()
            for s in series
        ),
        key=_quarter_sort_key,
    )

    if not all_quarters:
        fig.add_annotation(
            text="此主題無立場變化資料",
            xref="paper", yref="paper",
            x=0.5, y=0.5,
            showarrow=False,
            font=dict(size=14, color="#888"),
        )
        return fig

    for i, (company, series) in enumerate(series_by_company.items()):
        if not series:
            continue
        color = COMPANY_COLORS[i % len(COMPANY_COLORS)]

        if mode == "cumulative":
            # 只畫「有相關性」的點用實線；無關的點用空心圓虛線連接
            relevant = [s for s in series if s.get("is_relevant", True)]
            irrelevant = [s for s in series if not s.get("is_relevant", True)]

            # 主折線（有方向性的點）
            if relevant:
                hover = [
                    f"{s['stance']}<br>{s['detail'][:60]}" for s in relevant
                ]
                fig.add_trace(go.Scatter(
                    x=[s["quarter"] for s in relevant],
                    y=[s["cumulative"] for s in relevant],
                    mode="lines+markers",
                    name=company,
                    line=dict(color=color, width=2.5),
                    marker=dict(size=9, symbol="circle"),
                    hovertemplate=(
                        "<b>%{x}</b><br>%{customdata}<br>"
                        f"累積分數：%{{y}}<extra>{company}</extra>"
                    ),
                    customdata=hover,
                ))

            # 無關的點：空心圓，不連線
            if irrelevant:
                fig.add_trace(go.Scatter(
                    x=[s["quarter"] for s in irrelevant],
                    y=[s["cumulative"] for s in irrelevant],
                    mode="markers",
                    name=f"{company}（主題未提及）",
                    marker=dict(
                        size=7, symbol="circle-open",
                        color="#bdc3c7", line=dict(width=1.5),
                    ),
                    hovertemplate=(
                        "<b>%{x}</b><br>主題未提及（無關）"
                        f"<extra>{company}</extra>"
                    ),
                    showlegend=False,
                ))

        else:  # delta bars
            # 有相關性的 bar 用立場顏色；無關的用淺灰
            bar_colors = [
                STANCE_COLOR.get(s["stance"], STANCE_COLOR["無關"])
                for s in series
            ]
            hover_labels = [
                s["stance"] if s.get("is_relevant") else "主題未提及"
                for s in series
            ]
            fig.add_trace(go.Bar(
                x=[s["quarter"] for s in series],
                y=[s["delta"] for s in series],
                name=company,
                marker_color=bar_colors,
                opacity=0.85,
                hovertemplate=(
                    "<b>%{x}</b><br>立場：%{customdata}<extra>" + company + "</extra>"
                ),
                customdata=hover_labels,
            ))

    # ── 股價疊圖（右軸，僅 cumulative 模式）────────────────────────────────
    has_price = False
    if mode == "cumulative" and price_by_company:
        for i, (company, quarter_prices) in enumerate(price_by_company.items()):
            pts = [(q, p) for q, p in sorted(
                quarter_prices.items(), key=lambda kv: _quarter_sort_key(kv[0])
            ) if q in set(all_quarters)]
            if len(pts) < 2:
                continue
            has_price = True
            color = COMPANY_COLORS[i % len(COMPANY_COLORS)]
            fig.add_trace(go.Scatter(
                x=[q for q, _ in pts],
                y=[p for _, p in pts],
                mode="lines",
                name=f"{company} 股價（指數化）",
                line=dict(color=color, width=1.5, dash="dot"),
                yaxis="y2",
                opacity=0.65,
                hovertemplate=(
                    "<b>%{x}</b><br>股價指數：%{y:.1f}（期初=100）"
                    f"<extra>{company}</extra>"
                ),
            ))

    # 零基線
    fig.add_hline(y=0, line_dash="dash", line_color="#bbb", line_width=1)

    y_title = (
        "累積立場分數（↑ 樂觀｜↓ 保守）"
        if mode == "cumulative"
        else "立場變化（+1 樂觀 / 0 不變或未提及 / -1 保守）"
    )

    # 每季至少 80px，最少 640px，超過螢幕寬度時靠 scrollable container 捲動
    chart_width = max(640, len(all_quarters) * 80)

    fig.update_layout(
        title=dict(text=f"「{topic}」立場趨勢", font=dict(size=16)),
        xaxis_title="季度",
        yaxis_title=y_title,
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="right", x=1,
        ),
        width=chart_width,
        height=380,
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=50, r=30, t=70, b=40),
        barmode="group",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#f0f0f0", tickangle=-30)
    fig.update_yaxes(
        showgrid=True, gridcolor="#f0f0f0",
        tickmode="linear", dtick=1, zeroline=False,
    )
    if has_price:
        fig.update_layout(
            yaxis2=dict(
                title="股價（期初=100）",
                overlaying="y", side="right",
                showgrid=False, zeroline=False,
            ),
        )

    return fig


def chart_to_scrollable_html(fig) -> str:
    """
    將 Plotly Figure 轉為可水平捲動的 HTML 片段。
    供 st.components.v1.html() 使用。
    """
    import plotly.io as pio
    fig_html = pio.to_html(
        fig,
        include_plotlyjs="cdn",
        full_html=False,
        config={"displayModeBar": True, "scrollZoom": False},
    )
    return (
        '<div style="overflow-x:auto;overflow-y:hidden;'
        '-webkit-overflow-scrolling:touch;padding-bottom:4px;">'
        f"{fig_html}"
        "</div>"
    )
