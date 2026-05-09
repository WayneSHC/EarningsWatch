"""
src/ui/views/multi.py

多公司比較模式的渲染：比較指標列、tab 佈局（比較摘要 / 趨勢比較 / 各公司報告）、
匯出按鈕。

由 app.py 在 ui.mode == "multi" 時呼叫一次 render_multi_company_result(ui)。
fragment 內仍呼叫 UIState.get() 重新取得實例，這是 Streamlit fragment 的標準
模式：fragment 重跑時不繼承 closure 的 UIState 引用。
"""

from __future__ import annotations

import html
from datetime import date

import streamlit as st

from src.ui.cache import cache_key
from src.ui.state import UIState


def render_multi_company_result(ui: UIState) -> None:
    """渲染多公司比較結果。所有資料從 ui (UIState) 讀取。"""
    from src.core.comparison import build_comparison_table, synthesize_diff
    from src.ui.chart import build_stance_series, render_trend_chart, chart_to_scrollable_html

    multi_results     = ui.multi_results
    _companies        = ui.multi_companies
    _topic            = ui.multi_topic
    _m_quarters       = ui.multi_quarters
    _m_custom_query   = ui.multi_custom_query

    st.success("✅ 分析完成")
    st.divider()

    # ── 比較指標列 ────────────────────────────────────────────────────
    metric_cols = st.columns(len(_companies))
    for i, cname in enumerate(_companies):
        r = multi_results.get(cname, {})
        conf = r.get("confidence", 0.0)
        n_cont = len(r.get("contradictions", []))
        n_shift = sum(
            1 for c in r.get("contradictions", [])
            if c.get("analysis", {}).get("stance_change") not in ("維持不變", "無關", None)
        )
        with metric_cols[i]:
            st.markdown(f"### {cname}")
            st.metric("信心度", f"{conf:.0%}")
            st.metric("跨季組數", f"{n_cont} 組")
            st.metric("立場轉變", f"{n_shift} 處")

    st.divider()

    # ── Tab 佈局 ──────────────────────────────────────────────────────
    tab_compare, tab_trend, *company_tabs = st.tabs(
        ["📊 比較摘要"] +
        ["📈 趨勢比較"] +
        [f"📄 {c}" for c in _companies]
    )

    # Tab 1：比較摘要
    with tab_compare:
        comparison_table = build_comparison_table(multi_results)
        if comparison_table:
            st.markdown("#### 跨季立場對照表")
            import pandas as pd
            df = pd.DataFrame(comparison_table).rename(columns={"quarter_pair": "季度對"})
            def _color_stance(val: str):
                if val == "更樂觀":
                    return "background-color: #d4edda; color: #155724"
                if val == "更保守":
                    return "background-color: #f8d7da; color: #721c24"
                if val == "維持不變":
                    return "background-color: #e2e3e5; color: #383d41"
                return ""
            # pandas >= 2.1 renamed applymap → map; support both versions
            _style_fn = (
                df.style.map
                if hasattr(df.style, "map")
                else df.style.applymap
            )
            st.dataframe(
                _style_fn(_color_stance, subset=[c for c in df.columns if c != "季度對"]),
                use_container_width=True,
                hide_index=True,
            )
            st.divider()
            st.markdown("#### 差異摘要")
            with st.spinner("生成差異摘要..."):
                summary = synthesize_diff(comparison_table, _topic, _companies)
            st.markdown(summary)
        else:
            st.info("各公司在此主題上無可對齊的有效比對資料")

    # Tab 2：趨勢比較圖
    with tab_trend:
        @st.fragment
        def _compare_trend_fragment():
            """Fragment：只重跑此區塊，避免 radio 點擊重置 tab 選擇。"""
            import streamlit.components.v1 as components
            # 重新從 UIState 取（fragment 重跑時取同一實例）
            _ui = UIState.get()
            _mr = _ui.multi_results or {}
            _cs = _ui.multi_companies
            _tp = _ui.multi_topic
            s_map = {
                c: build_stance_series(_mr.get(c, {}).get("contradictions", []))
                for c in _cs
            }
            mode = st.radio(
                "顯示模式",
                ["累積分數", "逐季變化"],
                horizontal=True,
                key="compare_chart_mode",
            )
            fig = render_trend_chart(
                s_map, _tp,
                mode="cumulative" if mode == "累積分數" else "delta",
            )
            components.html(chart_to_scrollable_html(fig), height=420, scrolling=False)
            st.caption(
                "累積分數：每季立場（更樂觀+1 / 維持不變 0 / 更保守−1）的累積加總。"
                "　淺灰 bar / 空心圓 = 此季度法說會未提及此主題。"
            )

            # ── 各公司季度覆蓋摘要 ────────────────────────────────────
            st.divider()
            st.markdown("##### 📅 季度覆蓋摘要")
            for cname, series in s_map.items():
                discussed     = [e["quarter"] for e in series if e.get("is_relevant")]
                not_discussed = [e["quarter"] for e in series if not e.get("is_relevant")]
                with st.expander(f"**{cname}**　✅ {len(discussed)} 季有討論　⬜ {len(not_discussed)} 季未提及",
                                 expanded=True):
                    col_yes, col_no = st.columns(2)
                    with col_yes:
                        st.markdown("**✅ 有討論此主題**")
                        if discussed:
                            st.markdown(
                                " ".join(
                                    # [f] html.escape 防止季度值含非預期字元時的注入
                                    f'<span style="background:#d4edda;border-radius:4px;'
                                    f'padding:2px 8px;margin:2px;display:inline-block">'
                                    f'{html.escape(q)}</span>'
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
                                    f'padding:2px 8px;margin:2px;display:inline-block">'
                                    f'{html.escape(q)}</span>'
                                    for q in not_discussed
                                ),
                                unsafe_allow_html=True,
                            )
                        else:
                            st.caption("（每季都有提及）")

        _compare_trend_fragment()

    # 個別公司 Tabs
    for i, cname in enumerate(_companies):
        with company_tabs[i]:
            r = multi_results.get(cname, {})
            if "error" in r:
                st.error(f"分析失敗：{r['error']}")
            else:
                st.markdown(r.get("final_report", "報告不可用"))

    # ── 匯出按鈕（多公司）────────────────────────────────────────────────────
    st.divider()
    st.markdown("#### 📥 匯出報告")
    from src.ui.export import to_csv_compare, to_pdf_compare

    _exp_table = build_comparison_table(multi_results)
    _fname_base = f"EarningsWatch_{'_vs_'.join(_companies)}_{_topic}_{date.today()}"

    # ── PDF 快取：只在結果改變時重新生成，避免每次重渲染都重算 ──────────────
    # [b] 納入 quarters / custom_query，換查詢維度後強制重建 PDF
    _pdf_cache_key = f"multi_pdf_{cache_key('_'.join(_companies), _topic, _m_quarters, _m_custom_query)}"
    if ui.pdf_cache_key != _pdf_cache_key:
        ui.multi_pdf_bytes = None
        ui.pdf_cache_key = _pdf_cache_key

    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        csv_bytes = to_csv_compare(_exp_table, _companies, _topic)
        st.download_button(
            label="⬇️ 下載比較表 CSV",
            data=csv_bytes,
            file_name=f"{_fname_base}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with dl_col2:
        if ui.multi_pdf_bytes is None:
            with st.spinner("生成 PDF（首次需數秒）..."):
                try:
                    _exp_summary = synthesize_diff(_exp_table, _topic, _companies) if _exp_table else ""
                except Exception:
                    _exp_summary = ""
                ui.multi_pdf_bytes = to_pdf_compare(
                    multi_results, _exp_table, _exp_summary, _companies, _topic
                )
        st.download_button(
            label="⬇️ 下載比較報告 PDF",
            data=ui.multi_pdf_bytes,
            file_name=f"{_fname_base}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
