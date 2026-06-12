"""
src/ui/views/single.py

單公司模式的渲染：信心度指標、RAG Agent 工具卡片、矛盾 / 承諾 / 趨勢 / 分析過程
四個 tab、匯出按鈕。

由 app.py 在 ui.mode == "single" 時呼叫一次 render_single_company_result(ui)。
fragment 內仍呼叫 UIState.get() 重新取得實例（fragment 重跑時不繼承 closure 引用）。
"""

from __future__ import annotations

import html
import re
from datetime import date

import streamlit as st

from src.ui.cache import sanitize_str as _sanitize_str
from src.ui.cache import cache_key
from src.ui.state import UIState
from src.ui import insights


@st.cache_data(ttl=3600, show_spinner=False)
def _quarterly_closes(ticker: str, quarters: tuple[str, ...]) -> dict[str, float]:
    """
    [c] 季末收盤價（快取 1 小時）：每季取 quarter_end_date 當日或之前最後一個
    交易日的收盤。任何失敗回空 dict —— 股價疊圖是加值資訊，絕不擋趨勢圖渲染。
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="6y")["Close"]
        if hist.empty:
            return {}
        hist.index = hist.index.tz_localize(None)
        out: dict[str, float] = {}
        for q in quarters:
            end = insights.quarter_end_date(q)
            if not end:
                continue
            sub = hist.loc[:end]
            if not sub.empty:
                out[q] = float(sub.iloc[-1])
        return out
    except Exception:
        return {}


def render_single_company_result(ui: UIState) -> None:
    """渲染單公司分析結果。所有資料從 ui (UIState) 讀取。"""
    result        = ui.result
    _meta         = ui.meta or {}
    _company      = _meta["company"]
    _topic        = _meta["topic"]
    _s_quarters   = _meta.get("quarters", [])
    _s_custom_query = _meta.get("custom_query", "")

    # [P2] 自動推導主題模式：顯示推導結果，讓使用者驗證並可重選
    if _meta.get("auto_detected_topic") and _topic:
        st.info(
            f"🎯 已自動推導主題：**{_topic}** "
            f"（如不正確，請在側欄改選主題後重新分析）"
        )

    # ── [UX-3] 資料新鮮度：金融使用者看任何分析的第一眼是「as of」──────────
    _as_of = None
    try:
        from src.ui.quarters import get_available_quarters
        _as_of = insights.latest_quarter(get_available_quarters(_company))
    except Exception:
        pass  # [b] BQ 不可用時不擋結果渲染，僅略過 banner
    if _as_of:
        st.caption(f"🗓️ **資料截至：{_sanitize_str(_company)} {_sanitize_str(_as_of)} 法說會**　（語料庫最新一季）")

    st.divider()

    confidence = result.get("confidence", 0.0)
    contradictions = result.get("contradictions", [])
    promises = result.get("promises", [])

    # 矛盾（前後說法衝突）vs 立場轉變（正常商業演進）分開計算
    contradiction_count = sum(
        1 for c in contradictions if c.get("analysis", {}).get("has_contradiction")
    )
    stance_shift_count = sum(
        1 for c in contradictions
        if c.get("analysis", {}).get("stance_change") not in ("維持不變", "無關", None)
        and not c.get("analysis", {}).get("has_contradiction")
    )

    # ── [UX-2] 裁決句：結論先於遙測 —— 語氣方向、最重大發現、管理層信用 ─────
    from src.ui.chart import build_stance_series
    _series = build_stance_series(contradictions)
    _verdict = insights.build_verdict(_series, contradictions, promises)
    _ps = insights.promise_stats(promises)

    _v_rows = [
        f"<strong>語氣軌跡</strong>：{_sanitize_str(_verdict['tone']['label'])}"
    ]
    if _verdict.get("top_finding"):
        _v_rows.append(f"<strong>最重大發現</strong>：{_sanitize_str(_verdict['top_finding'])}")
    _v_rows.append(f"<strong>管理層信用</strong>：{_sanitize_str(_verdict['promise_line'])}")
    st.markdown(
        '<div style="background:#fffbeb;border-left:4px solid #d97706;'
        'padding:12px 16px;border-radius:4px;margin-bottom:8px;line-height:1.9">'
        + "<br>".join(_v_rows) + "</div>",
        unsafe_allow_html=True,
    )

    # ── [UX-1] 指標卡：方向優先，模型信心度移往「分析過程」tab ──────────────
    col1, col2, col3, col4 = st.columns(4)
    _traj = _verdict["tone"]
    with col1:
        st.metric(
            "語氣軌跡",
            f"{_traj['arrow']} {_traj['direction']}",
            delta=(f"連續 {_traj['streak']} 季" if _traj["streak"] >= 2 else None),
            delta_color="off",
        )
    with col2:
        st.metric(
            "明確矛盾",
            f"{contradiction_count} 處",
            delta="⚠ 需關注" if contradiction_count > 0 else None,
        )
    with col3:
        st.metric(
            "立場轉變",
            f"{stance_shift_count} 處",
            delta="需留意" if stance_shift_count > 0 else None,
        )
    with col4:
        st.metric(
            "承諾兌現率",
            f"{_ps['rate']:.0%}" if _ps["rate"] is not None else "—",
            delta=(f"{_ps['fulfilled']}達標 / {_ps['missed']}未兌現"
                   if (_ps["fulfilled"] + _ps["missed"]) > 0 else None),
            delta_color="off",
            help="達標 ÷（達標＋未兌現）；「不明」不入分母",
        )

    st.divider()

    # ── RAG Agent 行為展示 ──────────────────────────────────────────────────
    _iteration    = result.get("iteration", 1)
    _tool_plan    = result.get("tool_plan", ["bigquery"])
    _news_ctx     = result.get("news_context", [])
    _stock        = result.get("stock_data", {})
    _retrieved_r  = result.get("retrieved", {})
    _steps_log    = result.get("steps_log", [])
    _sub_qs       = result.get("sub_queries", [])
    _node_timings = result.get("node_timings", {})

    # ── Item 3：工具啟用卡片（[UX-1] 與 Self-Reflection 徽章一併移至
    #     「分析過程」tab 渲染 —— 系統遙測不佔決策版面）────────────────────────
    _retry_count = max(0, _iteration - 1)
    _q_count     = len(_retrieved_r)
    _chunk_count = sum(len(v) for v in _retrieved_r.values())
    _badges_html = [
        f'<span style="background:#dbeafe;border-radius:5px;padding:4px 11px;margin:2px 3px;'
        f'display:inline-block;font-size:13px">🗄️ RAG 知識庫 · {_q_count} 季 / {_chunk_count} 段落</span>',
    ]
    if "tavily" in _tool_plan or _news_ctx:
        _badges_html.append(
            f'<span style="background:#dcfce7;border-radius:5px;padding:4px 11px;margin:2px 3px;'
            f'display:inline-block;font-size:13px">📰 即時新聞 · {len(_news_ctx)} 篇</span>'
        )
    if "yfinance" in _tool_plan or (_stock and "error" not in _stock):
        _price = (
            f"{_stock.get('current_price','')} {_stock.get('currency','')}"
            if "error" not in _stock else "—"
        )
        _badges_html.append(
            f'<span style="background:#fef9c3;border-radius:5px;padding:4px 11px;margin:2px 3px;'
            f'display:inline-block;font-size:13px">📈 股價 · {_price}</span>'
        )
    # ── Item 4：Coverage Sweep 前後對比 ──────────────────────────────────────
    _cov_line = next((s for s in _steps_log if "補充" in s and "未覆蓋季度" in s), "")
    if _cov_line:
        _m_cov = re.search(r'補充 (\d+) 個未覆蓋季度', _cov_line)
        _n_cov = _m_cov.group(1) if _m_cov else "?"
        _badges_html.append(
            f'<span style="background:#ede9fe;border-radius:5px;padding:4px 11px;margin:2px 3px;'
            f'display:inline-block;font-size:13px">🔍 Coverage Sweep · 補充 {_n_cov} 季</span>'
        )

    # Tab 切換不同分析面向
    tab_report, tab_contradiction, tab_promise, tab_trend, tab_agent = st.tabs(
        ["📄 完整報告", "🚨 矛盾詳情", "📋 承諾追蹤", "📈 趨勢圖", "🤖 分析過程"]
    )

    with tab_report:
        # [f] Streamlit markdown 會把 $...$ 當 LaTeX 數學渲染；新聞標題裡的金額（如 $572.5B）
        #     會把整段文字吃成公式。在送進 st.markdown 前把 $ 轉義為 \$（標準 markdown 字面值）。
        #     PDF 匯出仍用原始 final_report（fpdf2 不處理 markdown），不受影響。
        final_report = result.get("final_report", "（報告生成中...）").replace("$", r"\$")

        # ── [UX-11] 晨會摘要：三行純文字，st.code 右上角自帶複製按鈕 ──────────
        with st.expander("📋 晨會摘要（點程式碼框右上角複製）", expanded=False):
            st.code(
                insights.morning_note(_company, _topic, _as_of, _verdict),
                language=None,
            )

        # ── D5：建議追問摘要框（[UX-5] 改按重大性排序，取前 3 條）─────────────
        _top3 = insights.top_follow_ups(contradictions, n=3)
        if _top3:
            _fq_items = "".join(
                f'<li style="margin:4px 0">{html.escape(q)}</li>'
                for q in _top3
            )
            st.markdown(
                f'<div style="background:#eff6ff;border-left:4px solid #3b82f6;'
                f'padding:12px 16px;border-radius:4px;margin-bottom:16px">'
                f'<strong>💡 建議追問管理層的問題</strong>'
                f'<ul style="margin:6px 0 0 0;padding-left:18px">'
                f'{_fq_items}</ul></div>',
                unsafe_allow_html=True,
            )

        st.markdown(final_report)

    with tab_contradiction:
        if not contradictions:
            st.info("需要至少 2 季資料才能做跨季比對")
        else:
            def _is_boilerplate(a: dict) -> bool:
                """偵測 LLM 引用的是相同 boilerplate 而非真正主題發言。"""
                ev_a = (a.get("evidence_early") or "").strip()
                ev_b = (a.get("evidence_later") or "").strip()
                # 兩段引文完全相同 → 明顯是同一段模板被重複抓到
                if ev_a and ev_b and ev_a == ev_b:
                    return True
                # LLM 明確判定兩段不討論同一主題
                if not a.get("same_topic", True):
                    return True
                return False

            # 過濾掉「無關」及 boilerplate 重複引用
            relevant = [
                c for c in contradictions
                if c.get("analysis", {}).get("stance_change") != "無關"
                and not _is_boilerplate(c.get("analysis", {}))
            ]
            irrelevant_count = len(contradictions) - len(relevant)

            if not relevant:
                st.info("所有比對組均判定為主題無關，請嘗試更換建議主題或縮小季度範圍。")
            else:
                if irrelevant_count > 0:
                    st.caption(f"已隱藏 {irrelevant_count} 組無效比對（主題無關 / 表格頁 / boilerplate）")
                st.caption("排序：重大性 × 信心度（財測指引 / 毛利率 > 資本支出 > 需求・庫存・產能 > 一般敘述）")

                # [UX-5] 毛利指引收回 ≠ 庫存措辭微調：按重大性排序而非季度配對順序
                for c in insights.sort_by_materiality(relevant):
                    a = c.get("analysis", {})
                    is_contradiction = a.get("has_contradiction", False)
                    card_class = "contradiction-card" if is_contradiction else "ok-card"
                    icon = "🚨" if is_contradiction else "✅"

                    # [f] 所有來自 LLM 的字串欄位，插入 HTML 前統一 html.escape()
                    #     防止 LLM 回傳 <script> 等惡意內容造成 XSS
                    q_a       = _sanitize_str(c.get("quarter_a"))
                    q_b       = _sanitize_str(c.get("quarter_b"))
                    stance    = _sanitize_str(a.get("stance_change", "-"))
                    detail    = _sanitize_str(a.get("change_detail", ""))
                    ev_early  = _sanitize_str(a.get("evidence_early", ""))
                    ev_later  = _sanitize_str(a.get("evidence_later", ""))
                    follow_up = _sanitize_str(a.get("follow_up_question", ""))
                    # [d] 信心度：顯示 LLM 對這組比對的把握程度（0%–100%）
                    _conf_val = a.get("confidence", None)
                    _conf_str = (
                        f'　<span style="font-size:12px;opacity:0.65">信心度 {_conf_val:.0%}</span>'
                        if isinstance(_conf_val, (int, float)) else ""
                    )
                    # [UX-5] 主題重大性標籤
                    _, _tag = insights.materiality_of(a)
                    _tag_chip = (
                        f'<span style="background:#fee2e2;border-radius:4px;padding:1px 8px;'
                        f'font-size:12px;margin-left:6px">{html.escape(_tag)}</span>'
                        if _tag != "一般敘述" else ""
                    )

                    # [UX-8] 引文驗證狀態：逐字 / 近似（合規等級的差異，資料原本就有）
                    def _quote_badge(fuzzy_key: str, quote: str) -> str:
                        if not quote:
                            return ""
                        label, bg = (
                            ("近似", "#fef3c7") if a.get(fuzzy_key) else ("逐字", "#d1fae5")
                        )
                        return (
                            f'<span style="background:{bg};border-radius:3px;'
                            f'padding:0 5px;font-size:11px;opacity:0.85">{label}</span>'
                        )

                    _ev_block = (
                        f'<em>「{ev_early}」</em>{_quote_badge("evidence_early_fuzzy", ev_early)}'
                        f' → <em>「{ev_later}」</em>{_quote_badge("evidence_later_fuzzy", ev_later)}'
                    )
                    if a.get("verification_failed") and not (ev_early or ev_later):
                        _ev_block = (
                            '<span style="opacity:0.7">（引文未通過原文驗證，已自動移除 — '
                            '信心度已對應下調）</span>'
                        )

                    st.markdown(f"""
<div class="{card_class}">
<strong>{icon} {q_a} vs {q_b}</strong>{_tag_chip}{_conf_str}<br>
立場變化：<strong>{stance}</strong><br>
{detail}<br>
<br>
{_ev_block}<br>
<br>
💡 <strong>建議追問</strong>：{follow_up}
</div>
""", unsafe_allow_html=True)

    with tab_promise:
        if not promises:
            st.info("未偵測到可追蹤的前瞻承諾")
        for p in promises:
            col_status, col_content = st.columns([1, 4])
            with col_status:
                # status 欄位含 emoji（✅ 達標 / ❌ 未兌現 / ⚠ 不明），取第一個 token 作圖示
                st.markdown(f"### {p.get('status', '⚠').split()[0]}")
            with col_content:
                # [f] promise_quarter / content / followup_quarter / detail 均來自 LLM
                #     以 st.markdown / st.caption 輸出（非 unsafe_allow_html），無 XSS 風險
                #     但仍做基本字串型別防呆
                promise_q   = str(p.get("promise_quarter", ""))
                content     = str(p.get("content", ""))
                followup_q  = str(p.get("followup_quarter", ""))
                detail      = str(p.get("detail", ""))
                st.markdown(f"**[{promise_q}承諾]** {content}")
                st.caption(f"後續（{followup_q}）：{detail}")

    with tab_trend:
        @st.fragment
        def _single_trend_fragment():
            """Fragment：只重跑此區塊，避免 radio 點擊重置 tab 選擇。"""
            import streamlit.components.v1 as components
            from src.ui.chart import build_stance_series, render_trend_chart, chart_to_scrollable_html
            # 重新從 UIState 取（fragment 重跑時取同一實例）
            _ui   = UIState.get()
            _res  = _ui.result or {}
            _meta = _ui.meta or {}
            _c    = _meta.get("company", "")
            _t    = _meta.get("topic", "")
            _cont = _res.get("contradictions", [])

            s = build_stance_series(_cont)
            if not s:
                st.info("此主題無有效立場變化資料（所有比對均為「無關」或 boilerplate）")
                return

            mode = st.radio(
                "顯示模式",
                ["累積分數", "逐季變化"],
                horizontal=True,
                key="single_chart_mode",
            )

            # ── [UX-4] 股價疊圖：語氣領先股價 = 訊號；落後 = 追認 ──────────
            _price_map = None
            if mode == "累積分數":
                from src.agent.tools import STOCK_CODE_MAP
                _ticker = STOCK_CODE_MAP.get(_c, "")
                if _ticker:
                    _closes = _quarterly_closes(
                        _ticker, tuple(e["quarter"] for e in s)
                    )
                    _idx = insights.index_prices(_closes)
                    if len(_idx) >= 2:
                        _price_map = {_c: _idx}

            fig = render_trend_chart(
                {_c: s}, _t,
                mode="cumulative" if mode == "累積分數" else "delta",
                price_by_company=_price_map,
            )
            components.html(chart_to_scrollable_html(fig), height=420, scrolling=False)
            st.caption(
                "累積分數：每季立場（更樂觀 +1 / 維持不變 0 / 更保守 −1）的累積加總。"
                "正值代表整體樂觀偏移，負值代表趨向保守。"
                "　淺灰 bar / 空心圓 = 此季度法說會未提及此主題。"
                + ("　虛線 = 同期股價（期初 = 100，右軸）。" if _price_map else "")
                + " ⚠ 語氣分數屬方向性指標（diffusion index 性質），"
                  "分值本身無基本面意義，請勿作水準值比較。"
            )
            # [UX-7] 區分「公司特有 vs 產業共性」是基本面分析的第一刀
            st.caption(
                "💡 想判斷此語氣轉變是公司特有還是產業共性？"
                "開啟側欄「🌐 多公司比較模式」看同業同主題對照。"
            )

            # ── 季度覆蓋摘要 ──────────────────────────────────────────
            discussed   = [e["quarter"] for e in s if e.get("is_relevant")]
            not_discussed = [e["quarter"] for e in s if not e.get("is_relevant")]

            st.divider()
            st.markdown("##### 📅 季度覆蓋摘要")
            col_yes, col_no = st.columns(2)
            with col_yes:
                st.markdown("**✅ 有討論此主題**")
                if discussed:
                    st.markdown(
                        " ".join(
                            f'<span style="background:#d4edda;border-radius:4px;'
                            f'padding:2px 8px;margin:2px;display:inline-block">{html.escape(q)}</span>'
                            for q in discussed
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("（無）")
            with col_no:
                st.markdown("**⬜ 未提及此主題**")
                if not_discussed:
                    st.markdown(
                        " ".join(
                            f'<span style="background:#f0f0f0;color:#888;border-radius:4px;'
                            f'padding:2px 8px;margin:2px;display:inline-block">{html.escape(q)}</span>'
                            for q in not_discussed
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("（每季都有提及）")

        _single_trend_fragment()

    with tab_agent:
        st.markdown("#### 🤖 Agent 分析過程")
        st.caption(
            "此頁面揭示 EarningsWatch RAG Agent 的內部運作："
            "Query 拆解 → 工具路由 → 並行檢索 → 矛盾偵測 → Self-Reflection → 報告生成"
        )

        # ── [UX-1] 系統遙測集中於此：模型信心度 + Self-Reflection + 工具卡片 ──
        _t_col1, _t_col2 = st.columns([1, 3])
        with _t_col1:
            st.metric(
                "模型分析信心度", f"{confidence:.0%}",
                help="Self-Reflection 節點對檢索品質與比對結果的自我評分（系統遙測，非投資指標）",
            )
        with _t_col2:
            if _retry_count > 0:
                st.info(
                    f"🔄 **Self-Reflection 觸發**：Agent 自動重查 **{_retry_count} 次** 後達標 "
                    f"→ 最終信心度 **{confidence:.0%}**　（代表初次檢索品質不足，系統自動擴展搜尋）",
                )
            else:
                st.success(
                    f"✅ **Agent 一次達標** — 信心度 **{confidence:.0%}**，"
                    f"Self-Reflection 評估無需重查"
                )
        st.markdown(
            "<div style='margin-bottom:4px'><strong>🤖 Agent 工具使用</strong>　"
            + "".join(_badges_html) + "</div>",
            unsafe_allow_html=True,
        )
        st.divider()

        # ── Item 5：Sub-query 拆解 ─────────────────────────────────────
        if _sub_qs:
            st.markdown("##### 📋 Query Decomposer — 問題拆解")
            st.caption("Agent 不直接搜尋，而是先將問題拆解為結構化子任務：")
            _tool_icons = {"bigquery": "🗄️", "tavily": "📰", "yfinance": "📈"}
            for _sq in _sub_qs:
                _ti = _tool_icons.get(_sq.get("tool", ""), "🔧")
                st.markdown(
                    f"{_ti} **{_sq.get('purpose', '')}**　→　`{_sq.get('query', '')}`"
                )
            st.divider()

        # ── Item 1：節點執行時間軸 ────────────────────────────────────
        if _node_timings:
            st.markdown("##### ⏱️ 節點執行時間軸")
            _NODE_ORDER = [
                ("classify",  "分析意圖",   "🔍"),
                ("decompose", "分解問題",   "📋"),
                ("route",     "選擇工具",   "🔧"),
                ("retrieve",  "檢索知識庫", "📚"),
                ("detect",    "矛盾偵測",   "🔎"),
                ("reflect",   "自我評估",   "🤔"),
                ("report",    "生成報告",   "📝"),
            ]
            _total_t = sum(_node_timings.values()) or 1
            _hdr0, _hdr1, _hdr2 = st.columns([2, 1, 5])
            _hdr0.markdown("**節點**")
            _hdr1.markdown("**耗時**")
            _hdr2.markdown("**比例**")
            for _nk, _nz, _ni in _NODE_ORDER:
                if _nk in _node_timings:
                    _t   = _node_timings[_nk]
                    _pct = _t / _total_t
                    _c0, _c1, _c2 = st.columns([2, 1, 5])
                    _c0.markdown(f"{_ni} {_nz}")
                    _c1.markdown(f"`{_t:.1f}s`")
                    _c2.markdown(
                        f'<div style="background:#3b82f6;height:16px;'
                        f'width:{max(2, int(_pct * 100))}%;'
                        f'border-radius:3px;display:inline-block"></div>',
                        unsafe_allow_html=True,
                    )
            st.caption(
                f"總執行時間：**{_total_t:.1f}s**　｜　"
                f"Self-Reflection 迭代：**{_iteration} 輪**"
            )
            st.divider()

        # ── Item 6：完整思考步驟 (Steps Log) ──────────────────────────
        st.markdown("##### 📜 完整思考步驟（Steps Log）")
        if _steps_log:
            # [f] html.escape 防 XSS：步驟記錄可能含 LLM 回傳的任意文字
            steps_html = "\n".join(
                f'<div class="step-log">{html.escape(s)}</div>'
                for s in _steps_log
            )
            st.markdown(steps_html, unsafe_allow_html=True)
        else:
            st.info("（使用快取結果，無即時步驟記錄）")

        # ── Item 7：Agent 架構圖 ──────────────────────────────────────
        st.divider()
        st.markdown("##### 🗺️ RAG Agent 架構")
        st.markdown("""
<div style="background:#f8fafc;border-radius:8px;padding:14px 18px;font-family:monospace;font-size:13px;line-height:2">
<strong>查詢輸入</strong><br>
&nbsp;&nbsp;&nbsp;&nbsp;↓<br>
<span style="color:#1d4ed8">① 分析意圖</span>（Classifier）　→　萃取公司 / 主題 / 季度<br>
&nbsp;&nbsp;&nbsp;&nbsp;↓<br>
<span style="color:#1d4ed8">② 分解問題</span>（Decomposer）　→　拆成 3 條子查詢<br>
&nbsp;&nbsp;&nbsp;&nbsp;↓<br>
<span style="color:#1d4ed8">③ 選擇工具</span>（Router）　→　決定 RAG / 新聞 / 股價<br>
&nbsp;&nbsp;&nbsp;&nbsp;↓<br>
<span style="color:#1d4ed8">④ 並行檢索</span>（Retrieval）　→　BigQuery + Coverage Sweep + Cohere Rerank<br>
&nbsp;&nbsp;&nbsp;&nbsp;↓<br>
<span style="color:#7c3aed">⑤ 矛盾偵測</span>（Detector）　→　LLM 跨季語意比對<br>
&nbsp;&nbsp;&nbsp;&nbsp;↓<br>
<span style="color:#b45309">⑥ 自我評估</span>（Self-Reflect）　→　信心度 &lt; 0.75 ？ 重查 ↩④ : 繼續<br>
&nbsp;&nbsp;&nbsp;&nbsp;↓<br>
<span style="color:#065f46">⑦ 生成報告</span>（Reporter）　→　Markdown 偵查報告輸出
</div>
""", unsafe_allow_html=True)

    # ── 匯出按鈕（單公司）────────────────────────────────────────────────────
    st.divider()
    st.markdown("#### 📥 匯出報告")
    from src.ui.export import to_csv_single, to_pdf_single

    _fname_base = f"EarningsWatch_{_company}_{_topic}_{date.today()}"

    # ── PDF 快取：只在結果改變時重新生成，避免每次重渲染都重算 ──────────────
    # [b] 納入 quarters / custom_query，換查詢維度後強制重建 PDF
    _pdf_cache_key = f"single_pdf_{cache_key(_company, _topic, _s_quarters, _s_custom_query)}"
    if ui.pdf_cache_key != _pdf_cache_key:
        ui.single_pdf_bytes = None
        ui.pdf_cache_key = _pdf_cache_key

    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        csv_bytes = to_csv_single(result, _company, _topic)
        st.download_button(
            label="⬇️ 下載矛盾資料 CSV",
            data=csv_bytes,
            file_name=f"{_fname_base}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with dl_col2:
        if ui.single_pdf_bytes is None:
            with st.spinner("生成 PDF（首次需數秒）..."):
                ui.single_pdf_bytes = to_pdf_single(result, _company, _topic)
        st.download_button(
            label="⬇️ 下載完整報告 PDF",
            data=ui.single_pdf_bytes,
            file_name=f"{_fname_base}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
