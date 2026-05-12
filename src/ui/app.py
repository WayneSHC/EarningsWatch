"""
src/ui/app.py
EarningsWatch Streamlit 主介面（薄殼）

職責：page config / sidebar / 執行 Agent / 路由到 view 模組。
渲染本身位於 src/ui/views/{single,multi}.py，加新 view 不必動本檔的 if/elif。
"""

# ── 修正 Streamlit 工作目錄問題（確保 src/ 可被 import）──────────────────────
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import re
import time
from datetime import date  # noqa: F401  (kept for future date-stamped messages)
from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

import streamlit as st

# UI 子模組（純函數、無 session 耦合）
from src.ui.styles import CUSTOM_CSS, INFINITY_SPINNER_HTML
from src.ui.auth import require_password
from src.ui.cache import get_cached_result, save_to_cache
from src.ui.state import UIState
from src.ui.quarters import get_available_quarters
from src.ui.views.single import render_single_company_result
from src.ui.views.multi import render_multi_company_result

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

# Demo 快取 / sanitize / 密碼閘門：實作位於 src/ui/{cache,auth}.py
# [f] 密碼閘門：APP_PASSWORD 已設定且未通過 → 此呼叫內部會 st.stop()
require_password()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🕵️ EarningsWatch")
    st.caption("法說會 RAG Agent 一致性審計平台")
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

    # [P2] 主題改為可選：預設「自動推導」讓 intent_classifier 從問題語意提煉，
    #      使用者也可繼續從下拉固定主題。教學回饋：每家公司關注的主題不同，
    #      固定 6 項清單無法 generalize。
    _TOPIC_AUTO = "（自動推導）"
    topic_choice = st.selectbox(
        "建議主題",
        [_TOPIC_AUTO] + TOPICS,
        index=0,
        help="預設讓 Agent 從你的問題自動推導主題；想固定就從清單選擇",
    )
    topic = "" if topic_choice == _TOPIC_AUTO else topic_choice
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
st.markdown("**法說會 RAG Agent 一致性審計平台** — 追蹤管理層跨季發言，找矛盾・追承諾・抓話術")
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
        # help tooltip：懸停時顯示按鈕功能說明，降低使用者疑惑
        help="啟動 AI Agent，對所選公司進行跨季法說會發言矛盾分析、承諾追蹤與矛盾偵測",
    )
with col_info:
    companies_label = " + ".join(selected_companies) if compare_mode else company
    _topic_label = topic if topic else "由問題自動推導"
    st.info(f"目標：**{companies_label}** ／ 主題：**{_topic_label}** ／ 季度：{'全部' if not quarters else ', '.join(quarters)}")

# ══════════════════════════════════════════════════════════════════════════════
# 執行 Agent（只在按下按鈕時執行，結果存入 UIState）
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
    # [f] 主題：空字串（自動推導模式）或在白名單內 → 兩者皆視為合法
    _invalid_topic = bool(topic) and topic not in TOPICS
    if _invalid_company or _invalid_topic:
        st.error("❌ 非法參數：公司或主題不在允許清單內，請重新選擇")
        st.stop()
    # [P2] 多公司比較需要明確主題（共享主題才能比較）；自動推導目前僅單公司支援
    if compare_mode and not topic:
        st.error("⚠️ 多公司比較模式請從主題清單選定一項（自動推導目前僅單公司模式可用）")
        st.stop()
    # [P2] 單公司：主題與自訂問題至少要有一個（否則 default query 會產生 "在「」方面…" 破碎字串）
    if not compare_mode and not topic and not custom_query.strip():
        st.error("⚠️ 請填寫自訂問題，或從主題清單選定一項")
        st.stop()
    # [f] 季度白名單：只允許 BigQuery 實際存在的季度值，格式 YYYYQN（4碼年 + Q + 1碼季）
    #     防止使用者傳入非預期字串進入 SQL 條件
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

        _multi_spinner = st.empty()
        _multi_spinner.markdown(
            INFINITY_SPINNER_HTML.format(
                text=f"並行分析　{' ＋ '.join(selected_companies)}　（需 30–90 秒）…"
            ),
            unsafe_allow_html=True,
        )
        try:
            multi_results = run_multi_company(
                companies=selected_companies,
                topic=topic,
                quarters=quarters,
                custom_query=custom_query.strip(),
            )
        except Exception as e:
            _multi_spinner.empty()  # 發生錯誤時立即清除動畫
            # [f] 不對使用者顯示 str(e)，避免洩漏 API key 片段或內部路徑
            from src.core.llm_client import friendly_error_message
            _etype = type(e).__name__
            _msg = friendly_error_message(e)
            print(f"[UI] 多公司分析失敗: {_etype}: {e}")
            st.error(f"多公司分析失敗：{_msg}")
            st.stop()
        finally:
            _multi_spinner.empty()  # 無論成功失敗都清除動畫

        # 儲存至 UIState，供重渲染時使用
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
            # 顯示無限動畫 spinner（取代原本的 progress_bar）
            _agent_spinner = st.empty()
            _agent_spinner.markdown(
                INFINITY_SPINNER_HTML.format(text="AI Agent 分析中，請稍候…"),
                unsafe_allow_html=True,
            )
            step_container = st.expander("🤖 Agent 思考步驟（即時更新）", expanded=True)

            try:
                import html  # [f] 步驟記錄即時渲染需要 html.escape 防 XSS
                from src.agent.graph import get_agent

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
                        # [移除 progress_bar，改由 infinity spinner 持續顯示]
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
                _agent_spinner.empty()  # Agent 完成後清除動畫
                save_to_cache(company, topic, result, quarters, custom_query.strip())

            except Exception as e:
                _agent_spinner.empty()  # [b] 例外發生時確保動畫清除，不讓 UI 停在 loading 狀態
                # [f] 不對使用者顯示 str(e)，避免洩漏 API key 片段或內部路徑
                from src.core.llm_client import friendly_error_message, LLMUnavailableError
                _etype = type(e).__name__
                _msg = friendly_error_message(e)
                print(f"[UI] Agent 執行失敗: {_etype}: {e}")
                # LLM 全掛時用更明確的 banner，避免使用者誤以為是程式 bug
                if isinstance(e, LLMUnavailableError):
                    st.error(f"⚠️ {_msg}")
                else:
                    st.error(f"Agent 執行失敗：{_msg}")
                st.info("正在嘗試載入 Demo 快取作為保底…")
                result = get_cached_result(company, topic, quarters, custom_query.strip())
                if not result:
                    # 區分兩種 cache-miss 場景：自訂問題 vs 無 demo 快取的公司
                    if custom_query.strip():
                        st.warning(
                            f"⚠ 自訂問題「{custom_query.strip()[:40]}」無對應 Demo 快取。"
                            "Demo 快取只涵蓋預設公司／主題組合；請等 LLM 配額恢復後重試，"
                            "或改用左側選單的預設主題（不要填自訂問題）以命中快取。"
                        )
                    else:
                        st.warning(
                            f"⚠ 此公司／主題（{company} × {topic}）尚未產生 Demo 快取。"
                            "請等 LLM 配額恢復後重試，或先以其他預設組合進行展示。"
                        )
                    st.caption("環境檢查：1) BigQuery 連線正常 2) 至少一個 API Key 配額未用完 3) PDF 已匯入並完成 embedding")
                    st.stop()

        if not result:
            st.error("無結果可顯示")
            st.stop()

        # [P2] 若 UI 沒指定 topic（自動推導模式），用 Agent 萃取後的主題覆寫；
        #      auto_detected 旗標供 UI 顯示「🎯 已自動推導主題」橫幅
        _final_topic = topic or (result.get("topic") if isinstance(result, dict) else "") or ""
        ui.result = result
        ui.meta = {
            "company": company,
            "topic": _final_topic,
            "quarters": quarters,
            "custom_query": custom_query.strip(),
            "auto_detected_topic": not topic,
        }
        ui.mode = "single"

# ══════════════════════════════════════════════════════════════════════════════
# 顯示結果（由 view 模組負責，從 UIState 讀取）
# ══════════════════════════════════════════════════════════════════════════════
if ui.mode == "multi":
    render_multi_company_result(ui)
elif ui.mode == "single":
    render_single_company_result(ui)

# ── 頁腳 ─────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "EarningsWatch 是文件分析工具，不提供投資建議。"
    "資料來源：MOPS 公開資訊觀測站法說會逐字稿。"
)
