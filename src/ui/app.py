"""
src/ui/app.py
EarningsWatch Streamlit 主介面

功能：
  - 下拉選單選擇公司與主題（目標用戶不需技術背景）
  - 即時顯示 Agent 思考步驟（展示 Agentic 特徵）
  - 偵查報告卡片（矛盾 / 承諾 / 新聞）
  - Demo 快取保底（防止 API 失敗的 0 分保護）
"""

# ── 修正 Streamlit 工作目錄問題（確保 src/ 可被 import）──────────────────────
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import os
import re
import json
import time
import html  # [f] 仍被 app.py 多處 html.escape() 直接使用（XSS 防護）
from datetime import date
from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

import streamlit as st

# UI 子模組（純函數、無 session 耦合）
from src.ui.styles import CUSTOM_CSS
from src.ui.auth import require_password
from src.ui.cache import (
    sanitize_str as _sanitize_str,    # [f] 既有呼叫點命名保留 _sanitize_str
    load_cache,
    cache_key,
    get_cached_result,
    save_to_cache,
    CACHE_PATH,
)
from src.ui.state import UIState  # session_state 集中保管

# ── 頁面設定（必須在其他 st 呼叫之前）─────────────────────────────────────
st.set_page_config(
    page_title="EarningsWatch｜法說會一致性審計",
    page_icon="🕵️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# 取得本次 session 的 UIState（首次建立、之後重用同一實例）
ui = UIState.get()

# ── 常數 ─────────────────────────────────────────────────────────────────────
COMPANIES = ["台積電", "聯發科", "鴻海", "台達電"]
TOPICS = ["AI需求", "毛利率", "產能與擴產", "庫存狀況", "市場展望", "CoWoS"]


@st.cache_data(ttl=300, show_spinner=False)
def get_available_quarters() -> list[str]:
    """
    從 Qdrant 動態讀取實際存在的季度列表，按時間排序。

    效能優化：優先使用 Qdrant facet API（v1.10+，一次查詢即可取得所有唯一值）。
    若版本不支援，改用 scroll + 提早停止策略，避免全資料掃描。
    """
    try:
        from src.core.qdrant_client import get_qdrant_client, COLLECTION_NAME
        client = get_qdrant_client()

        # ── 優先：facet API（Qdrant >= 1.10，效率最高）──────────────
        if hasattr(client, "facet"):
            resp = client.facet(
                collection_name=COLLECTION_NAME,
                key="quarter",
                limit=100,   # 季度數量不會超過 100
            )
            quarters = [hit.value for hit in resp.hits if hit.value]
            if quarters:
                return sorted(quarters, key=lambda x: (x[:4], x[4:]))

        # ── 降級：scroll 限制最大掃描筆數（避免 full scan）────────────
        quarters: set[str] = set()
        offset = None
        MAX_SCAN = 2000      # 最多掃 2000 筆即可涵蓋所有季度組合
        # [b] 用獨立計數器，不依賴 offset 型別
        # （Qdrant >= 1.7 的 offset 是 UUID 字串，(offset or 0) 會 TypeError）
        total_scanned = 0

        while True:
            results, next_offset = client.scroll(
                collection_name=COLLECTION_NAME,
                limit=200,
                offset=offset,
                with_payload=["quarter"],
                with_vectors=False,
            )
            for r in results:
                q = r.payload.get("quarter", "")
                if q:
                    quarters.add(q)

            total_scanned += len(results)
            if next_offset is None or total_scanned >= MAX_SCAN:
                break
            offset = next_offset

        # 排序：2022Q4, 2023Q1, ... 2026Q1
        return sorted(quarters, key=lambda x: (x[:4], x[4:]))

    except (ConnectionError, TimeoutError, OSError):
        # Qdrant 未啟動 / 連線失敗時的 fallback
        pass
    except Exception as _e:
        # [b] 非預期的程式錯誤（如 API 格式變更）記錄但不讓它靜默吞掉
        print(f"[UI] get_available_quarters 意外失敗: {type(_e).__name__}: {_e}")
    return ["2022Q4", "2023Q1", "2023Q2", "2023Q3",
            "2024Q2", "2024Q3", "2024Q4",
            "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1"]

# Demo 快取 / sanitize / 密碼閘門：實作位於 src/ui/{cache,auth}.py
# [f] 密碼閘門：APP_PASSWORD 已設定且未通過 → 此呼叫內部會 st.stop()
require_password()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🕵️ EarningsWatch")
    st.caption("法說會 Agentic RAG 一致性審計平台")
    st.divider()

    # 多公司比較模式切換
    compare_mode = st.toggle("🌐 多公司比較模式", value=False)

    if compare_mode:
        selected_companies = st.multiselect(
            "選擇公司（最多 3 家）",
            COMPANIES,
            default=[COMPANIES[0]],
            max_selections=3,
        )
        # 主公司（用於快取 key 等單一公司邏輯）
        company = selected_companies[0] if selected_companies else COMPANIES[0]
        # 選擇不足時給出明確提示
        if len(selected_companies) < 2:
            st.warning("⚠ 比較模式需至少選擇 **2 家**公司", icon="⚠️")
    else:
        company = st.selectbox("選擇公司", COMPANIES, index=0)
        selected_companies = [company]

    topic = st.selectbox("分析主題", TOPICS, index=0)
    available_quarters = get_available_quarters()
    quarter_selection = st.multiselect(
        f"季度範圍（留空 = 全部，共 {len(available_quarters)} 季）",
        available_quarters,
        default=[],
    )
    quarters = quarter_selection if quarter_selection else []

    custom_query = st.text_area(
        "自訂問題（選填）",
        placeholder="例：台積電在 AI 需求上前後是否有矛盾？",
        height=80,
    )

    st.divider()
    use_cache = st.toggle("使用 Demo 快取（API 呼叫失敗時的保底）", value=False)

    st.caption("⚠ 此工具為文件分析平台，不提供選股建議或股價預測")
    # [S1] 只顯示目前 LLM 後端資訊（唯讀）。
    # 不提供切換介面：set_backend() 修改 os.environ 是 process-wide 狀態，
    # 多使用者環境下任何人切換都會影響其他 session，也可能切到高成本模型。
    # 若需切換後端，請在 .env 設定 LLM_BACKEND 後重啟服務。
    try:
        from src.core.llm_client import which_backend
        _info = which_backend()
        if _info and not _info.startswith("❌"):
            st.caption(f"🤖 目前：{_info}")
    except Exception:
        pass

    # [S4] LangSmith tracing 狀態（透明度 / Demo 觀測用）
    try:
        from src.agent.graph import tracing_status
        st.caption(tracing_status())
    except Exception:
        pass

    # ── Token / Cost telemetry（本次 session 累計）─────────────────────────
    # 只在已有資料時顯示，避免空白佔版面
    try:
        from src.core import telemetry as _tm
        _summary = _tm.summary()
        if _summary["total_calls"] > 0:
            st.divider()
            st.caption("📊 **本次 Session LLM 用量**")
            cols = st.columns(2)
            cols[0].metric("呼叫次數", _summary["total_calls"])
            cols[1].metric(
                "預估成本",
                f"${_summary['estimated_cost_usd']:.4f}",
                help="依 OpenAI / Gemini / Cohere 公開定價估算（非實際帳單）"
            )
            st.caption(
                f"Tokens：{_summary['total_tokens']:,}  "
                f"（input {_summary['prompt_tokens']:,} / output {_summary['completion_tokens']:,}）"
            )
            if st.button("🔄 重置統計", key="_reset_telemetry"):
                _tm.reset()
                st.rerun()
    except Exception:
        pass

# ── 主區域 ───────────────────────────────────────────────────────────────────
st.title("🕵️ EarningsWatch")
st.markdown("**法說會 Agentic RAG 一致性審計平台** — 追蹤管理層跨季發言，找矛盾・追承諾・抓話術")
st.divider()

# ── [f] Rate Limiting：雙層防護 ──────────────────────────────────────────────
# Layer 1：session-based（清 cookie / 開新 tab 即可繞過）
# Layer 2：[P1-9] IP-based（跨 session 跨 tab，配合 X-Forwarded-For）
# 只要任一層在冷卻中，按鈕就會 disabled
from src.core import rate_limiter as _rl

_COOLDOWN_SEC = 10   # 兩次查詢間最短間隔（秒）
now = time.time()
last_run = ui.last_run_time
session_cooldown = max(0.0, _COOLDOWN_SEC - (now - last_run))

_client_ip = _rl.get_client_ip()
ip_cooldown = _rl.check(_client_ip) if _client_ip else 0.0

cooldown_remaining = max(session_cooldown, ip_cooldown)

_not_enough_companies = compare_mode and len(selected_companies) < 2

col_btn, col_info = st.columns([1, 3])
with col_btn:
    run_btn = st.button(
        "🔍 開始偵查" if cooldown_remaining == 0 else f"⏳ 請稍候 {cooldown_remaining:.0f}s",
        type="primary",
        use_container_width=True,
        disabled=cooldown_remaining > 0 or _not_enough_companies,
    )
with col_info:
    companies_label = " + ".join(selected_companies) if compare_mode else company
    st.info(f"目標：**{companies_label}** ／ 主題：**{topic}** ／ 季度：{'全部' if not quarters else ', '.join(quarters)}")

# ══════════════════════════════════════════════════════════════════════════════
# 執行 Agent（只在按下按鈕時執行，結果存入 session_state）
# ══════════════════════════════════════════════════════════════════════════════
if run_btn:
    ui.last_run_time = time.time()
    # [P1-9] 同步記錄到 IP 限流器（無 IP 時為 no-op）
    if _client_ip:
        _rl.record(_client_ip)

    # ── [f] 輸入驗證：防止超長輸入、HTML/Script 注入、非法參數 ──────────────
    _MAX_QUERY_LEN = 500   # 超過此長度截斷，避免 token 爆炸與 prompt injection
    if len(custom_query) > _MAX_QUERY_LEN:
        st.warning(
            f"⚠ 自訂問題過長（{len(custom_query)} 字），"
            f"已自動截斷至 {_MAX_QUERY_LEN} 字"
        )
    custom_query = custom_query[:_MAX_QUERY_LEN]
    # 移除所有 HTML/XML 標籤，防止標籤注入進 LLM prompt 或快取後的頁面
    custom_query = re.sub(r'<[^>]{0,100}>', '', custom_query).strip()
    # 白名單驗證：即使 Streamlit widget 被繞過，也確保值合法
    # （防禦 session state 竄改 / 自動化腳本直接 POST）
    _invalid_company = any(c not in COMPANIES for c in selected_companies)
    if _invalid_company or topic not in TOPICS:
        st.error("❌ 非法參數：公司或主題不在允許清單內，請重新選擇")
        st.stop()
    # [f] 季度白名單：只允許 Qdrant 實際存在的季度值，格式 YYYYQN（4碼年 + Q + 1碼季）
    #     防止使用者傳入非預期字串進入 Qdrant filter
    _valid_quarters = set(available_quarters)
    _quarter_pattern = re.compile(r"^\d{4}Q[1-4]$")
    quarters = [
        q for q in quarters
        if q in _valid_quarters and _quarter_pattern.match(q)
    ]

    # ══════════════════════════════════════════════════════════════════════
    # 分支 A：多公司比較模式
    # ══════════════════════════════════════════════════════════════════════
    if compare_mode and len(selected_companies) >= 2:
        from src.core.comparison import run_multi_company
        from src.ui.chart import build_stance_series, render_trend_chart

        with st.spinner(f"⚙️ 並行分析 {' + '.join(selected_companies)}（需 30–90 秒）..."):
            try:
                multi_results = run_multi_company(
                    companies=selected_companies,
                    topic=topic,
                    quarters=quarters,
                    custom_query=custom_query.strip(),
                )
            except Exception as e:
                # [f] 不對使用者顯示 str(e)，避免洩漏 API key 片段或內部路徑
                _etype = type(e).__name__
                print(f"[UI] 多公司分析失敗: {_etype}: {e}")
                st.error(f"多公司分析失敗（{_etype}），請稍後再試或確認 API Key 設定。")
                st.stop()

        # 儲存至 session_state，供重渲染時使用
        ui.multi_results = multi_results
        ui.multi_companies = list(selected_companies)
        ui.multi_topic = topic
        ui.multi_quarters = quarters
        ui.multi_custom_query = custom_query.strip()
        ui.mode = "multi"

    # ══════════════════════════════════════════════════════════════════════
    # 分支 B：單公司模式
    # ══════════════════════════════════════════════════════════════════════
    else:
        query = custom_query.strip() or f"{company} 在「{topic}」方面，各季度發言是否有矛盾或立場轉變？請追蹤承諾兌現情況。"

        # 先查快取
        cached = get_cached_result(company, topic, quarters, custom_query.strip()) if use_cache else None
        if cached:
            st.success("⚡ 使用快取結果（Demo 保底模式）")
            result = cached
        else:
            # 真實執行 Agent
            step_container = st.expander("🤖 Agent 思考步驟（即時更新）", expanded=True)
            progress_bar = st.progress(0, text="初始化 Agent...")

            try:
                from src.agent.graph import run_agent, get_agent

                steps_placeholder = step_container.empty()
                all_steps = []

                agent = get_agent()
                initial_state = {
                    "query": query,
                    "company": company,
                    "topic": topic,
                    "quarters": quarters,
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
                    "final_report": "",
                    "steps_log": [],
                }

                NODE_NAMES = {
                    "classify": ("分析問題意圖", 1),
                    "decompose": ("分解子問題", 2),
                    "route": ("選擇工具", 3),
                    "retrieve": ("檢索知識庫", 4),
                    "detect": ("矛盾偵測", 6),
                    "reflect": ("自我評估", 7),
                    "report": ("生成報告", 8),
                }

                accumulated_state = dict(initial_state)
                accumulated_state["node_timings"] = {}
                _prev_node_time = time.time()
                for step in agent.stream(initial_state, stream_mode="updates"):
                    for node_name, node_output in step.items():
                        _now = time.time()
                        accumulated_state["node_timings"][node_name] = round(_now - _prev_node_time, 1)
                        _prev_node_time = _now
                        name_zh, prog = NODE_NAMES.get(node_name, (node_name, 5))
                        progress_bar.progress(min(prog * 12, 90), text=f"⚙️ {name_zh}...")
                        new_steps = node_output.get("steps_log", [])
                        all_steps.extend(new_steps)
                        # [f] html.escape 防 XSS：steps_log 可能含 LLM 回傳的 change_detail
                        steps_md = "\n\n".join(
                            f'<div class="step-log">{html.escape(s)}</div>'
                            for s in all_steps
                        )
                        steps_placeholder.markdown(steps_md, unsafe_allow_html=True)
                        for k, v in node_output.items():
                            if k == "steps_log":
                                accumulated_state["steps_log"] = all_steps
                            else:
                                accumulated_state[k] = v

                result = accumulated_state
                progress_bar.progress(100, text="✅ 完成！")
                save_to_cache(company, topic, result, quarters, custom_query.strip())

            except Exception as e:
                # [f] 不對使用者顯示 str(e)，避免洩漏 API key 片段或內部路徑
                _etype = type(e).__name__
                print(f"[UI] Agent 執行失敗: {_etype}: {e}")
                st.error(f"Agent 執行失敗（{_etype}），嘗試載入 Demo 快取...")
                st.warning("嘗試載入 Demo 快取...")
                result = get_cached_result(company, topic, quarters, custom_query.strip())
                if not result:
                    st.error("無快取資料，請確認：1) Qdrant Docker 執行中 2) API Keys 已設定 3) PDF 已匯入")
                    st.stop()

        if not result:
            st.error("無結果可顯示")
            st.stop()

        # 儲存至 session_state，供重渲染時使用
        ui.result = result
        ui.meta = {
            "company": company,
            "topic": topic,
            "quarters": quarters,
            "custom_query": custom_query.strip(),
        }
        ui.mode = "single"

# ══════════════════════════════════════════════════════════════════════════════
# 顯示結果（從 UIState 讀取，每次重渲染都能保留）
# ══════════════════════════════════════════════════════════════════════════════
last_mode = ui.mode

# ── 多公司比較結果 ─────────────────────────────────────────────────────────
if last_mode == "multi":
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
    from src.core.comparison import build_comparison_table, synthesize_diff

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

# ── 單公司結果 ─────────────────────────────────────────────────────────────
elif last_mode == "single":
    result        = ui.result
    _meta         = ui.meta or {}
    _company      = _meta["company"]
    _topic        = _meta["topic"]
    _s_quarters   = _meta.get("quarters", [])
    _s_custom_query = _meta.get("custom_query", "")

    st.divider()

    # 信心度指標
    confidence = result.get("confidence", 0.0)
    col1, col2, col3, col4 = st.columns(4)
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

    with col1:
        st.metric("分析信心度", f"{confidence:.0%}")
    with col2:
        st.metric("跨季比對組數", f"{len(contradictions)} 組")
    with col3:
        st.metric(
            "立場轉變",
            f"{stance_shift_count} 處",
            delta="需留意" if stance_shift_count > 0 else None,
        )
    with col4:
        st.metric(
            "明確矛盾",
            f"{contradiction_count} 處",
            delta="⚠ 需關注" if contradiction_count > 0 else None,
        )

    st.divider()

    # ── Agentic RAG 行為展示 ──────────────────────────────────────────────────
    _iteration    = result.get("iteration", 1)
    _tool_plan    = result.get("tool_plan", ["qdrant"])
    _news_ctx     = result.get("news_context", [])
    _stock        = result.get("stock_data", {})
    _retrieved_r  = result.get("retrieved", {})
    _steps_log    = result.get("steps_log", [])
    _sub_qs       = result.get("sub_queries", [])
    _node_timings = result.get("node_timings", {})

    # ── Item 2：Self-Reflection 徽章 ──────────────────────────────────────────
    _retry_count = max(0, _iteration - 1)
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

    # ── Item 3：工具啟用卡片 ──────────────────────────────────────────────────
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
    st.markdown(
        "<div style='margin-bottom:4px'><strong>🤖 Agent 工具使用</strong>　"
        + "".join(_badges_html) + "</div>",
        unsafe_allow_html=True,
    )
    st.divider()

    # Tab 切換不同分析面向
    tab_report, tab_contradiction, tab_promise, tab_trend, tab_agent = st.tabs(
        ["📄 完整報告", "🚨 矛盾詳情", "📋 承諾追蹤", "📈 趨勢圖", "🤖 分析過程"]
    )

    with tab_report:
        final_report = result.get("final_report", "（報告生成中...）")

        # ── D5：建議追問摘要框（取有效立場變化的 follow_up，最多 3 條）────────
        _follow_ups = [
            a.get("follow_up_question", "").strip()
            for c in contradictions
            for a in [c.get("analysis", {})]
            if a.get("follow_up_question", "").strip()
            and a.get("stance_change") not in ("無關", "維持不變", None)
        ]
        # 去重、保序
        _seen: set[str] = set()
        _unique_fqs: list[str] = []
        for _fq in _follow_ups:
            if _fq not in _seen:
                _seen.add(_fq)
                _unique_fqs.append(_fq)
        if _unique_fqs:
            _top3 = _unique_fqs[:3]
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
                st.info("所有比對組均判定為主題無關，請嘗試更換分析主題或縮小季度範圍。")
            else:
                if irrelevant_count > 0:
                    st.caption(f"已隱藏 {irrelevant_count} 組無效比對（主題無關 / 表格頁 / boilerplate）")

                for c in relevant:
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

                    st.markdown(f"""
<div class="{card_class}">
<strong>{icon} {q_a} vs {q_b}</strong>{_conf_str}<br>
立場變化：<strong>{stance}</strong><br>
{detail}<br>
<br>
<em>「{ev_early}」</em> → <em>「{ev_later}」</em><br>
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
            fig = render_trend_chart(
                {_c: s}, _t,
                mode="cumulative" if mode == "累積分數" else "delta",
            )
            components.html(chart_to_scrollable_html(fig), height=420, scrolling=False)
            st.caption(
                "累積分數：每季立場（更樂觀 +1 / 維持不變 0 / 更保守 −1）的累積加總。"
                "正值代表整體樂觀偏移，負值代表趨向保守。"
                "　淺灰 bar / 空心圓 = 此季度法說會未提及此主題。"
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
            "此頁面揭示 EarningsWatch Agentic RAG 的內部運作："
            "Query 拆解 → 工具路由 → 並行檢索 → 矛盾偵測 → Self-Reflection → 報告生成"
        )

        # ── Item 5：Sub-query 拆解 ─────────────────────────────────────
        if _sub_qs:
            st.markdown("##### 📋 Query Decomposer — 問題拆解")
            st.caption("Agent 不直接搜尋，而是先將問題拆解為結構化子任務：")
            _tool_icons = {"qdrant": "🗄️", "tavily": "📰", "yfinance": "📈"}
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
        st.markdown("##### 🗺️ Agentic RAG 架構")
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
<span style="color:#1d4ed8">④ 並行檢索</span>（Retrieval）　→　Qdrant + Coverage Sweep + Cohere Rerank<br>
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

# ── 頁腳 ─────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "EarningsWatch 是文件分析工具，不提供投資建議。"
    "資料來源：MOPS 公開資訊觀測站法說會逐字稿。"
)
