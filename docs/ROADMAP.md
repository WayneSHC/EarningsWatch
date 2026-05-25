# EarningsWatch — Roadmap & Tech Debt

> 待辦清單。新人接手請先讀完 `PROJECT_OVERVIEW.md` 再回頭看這份。
> 每項標明**優先度**與**動工觸發條件**——「真的有人抱怨再做」原則，避免過度設計。

---

## 進行中 / 已完成

| 完成日 | Commit | 內容 |
|---|---|---|
| 2026-05-03 | `2f73854` | feat: agentic RAG hardening — multi-LLM cascade, coverage sweep, error unwrap |
| 2026-05-03 | `046190a` | docs: comprehensive onboarding overview |
| 2026-05-03 | `37b37d1` | test+ci: 57 tests covering v1.1 hardening + GitHub Actions |
| 2026-05-03 | `e0cdde7` | refactor(ui): extract cache, auth, styles from app.py |
| 2026-05-09 | `3717df6` | refactor(ui): UIState dataclass — fulfils ROADMAP #1 (session-state 集中) |
| 2026-05-09 | `2acdc7b` | ci: Streamlit smoke test (script-health-check) — ROADMAP #5 |
| 2026-05-09 | `d0201f8` | refactor(ui): split app.py into views/single + views/multi — ROADMAP #2 |
| 2026-05-09 | `32e92f5` | chore: .env.example LangSmith vars / fix ingestion atomic write / docs PDF font — ROADMAP #8 / #9 / #10 |

---

## ✅ #1 — refactor(ui): extract SessionKeys constants from app.py

> **已完成 2026-05-09**（commit `3717df6` — P1-8 UIState 重構）。
> 採 dataclass 而非當初提議的 frozen dataclass / StrEnum，但達成同樣目標：
> session-state key 集中保管，typo 變 AttributeError。後續 #2 拆分（commit
> `d0201f8`）已 grep 驗證 `src/ui/views/*.py` 無任何 literal `st.session_state[...]`
> 存取 — 驗收條件中前兩條（無 literal 字串、pytest 全綠 144/144）皆已滿足。
> 第三條「手動 streamlit run 跑 single + multi」由開發者自行驗證，CI 的
> `script-health-check` smoke test 提供 import / set_page_config 層級的保證。

**優先度**：🟡 Medium
**預估**：2-3 小時
**動工觸發**：下次要對 `app.py` rendering 區塊做任何修改前

### 為什麼

`app.py` 還剩 ~1000 行 view 程式碼，散落著 magic strings：
- `st.session_state["last_result"]`
- `st.session_state["_pdf_cache_key"]`
- `st.session_state["_authenticated"]`
- ...

要安全拆分 single/multi rendering 進獨立檔案前，所有 session-state key 必須先集中到一個地方，不然改一個 key 名稱會散到 4-5 個檔案。

### 怎麼做

1. 建 `src/ui/session.py`，用 frozen dataclass 或 `StrEnum` 集中所有 key
2. grep `app.py` 抽出所有 `st.session_state["..."]` literal，列在這支援的 key 表：
   - `last_mode`, `last_result`, `last_meta`
   - `last_multi_results`, `last_multi_companies`, `last_multi_topic`
   - `_single_pdf_bytes`, `_multi_pdf_bytes`, `_pdf_cache_key`
   - `_authenticated`, `_pwd_input`, `_last_run_ts`
3. 全部換成命名常數

### 驗收條件

- [ ] `app.py` 不再出現 literal `st.session_state["...."]` 字串
- [ ] 既有 57 個 pytest 全綠
- [ ] 本地 `streamlit run` 完整跑一次 single + multi 查詢，UI 行為不變

---

## ✅ #2 — refactor(ui): split single/multi rendering into separate views

> **已完成 2026-05-09**（commit `d0201f8`）。app.py 由 1087 行縮到 385 行；
> single/multi 移到 `src/ui/views/`；views 不再使用 literal `st.session_state[...]`。

**優先度**：🟢 Low
**預估**：4-6 小時 + 手動 UI smoke test
**動工觸發**：要新增第三種 view（例如「同產業跨公司比較」）時
**Blocked by**：#1（SessionKeys）

### 為什麼

`app.py` 目前 1020 行，扣掉前置 setup / sidebar 後，**60% 是 single + multi 兩塊 rendering**：
- L283-470：query input + agent 執行
- L473-664：multi-company rendering
- L666-1086：single-company rendering

未來加新 view（例如 industry comparison、time-series 對標）會迫使再多一塊 ~400 行的 if/else，難以維護。

### 怎麼做

1. `src/ui/views/single.py` — `render_single_company_result(state: SingleCompanyResult)`
2. `src/ui/views/multi.py` — `render_multi_company_result(state: MultiCompanyResult)`
3. `app.py` 變薄成入口：sidebar + 路由 + dispatch

### 驗收條件

- [ ] `app.py` ≤ 400 行
- [ ] view 函數簽章只接受 `state` 物件，不直接讀 `st.session_state`
- [ ] 手動驗證：single 模式（含 PDF 匯出）+ multi 模式（3 家公司）UI 行為皆相同
- [ ] 既有 57 個 pytest 全綠

---

## #3 — feat: LangGraph SqliteSaver persistence

**優先度**：🟢 Low
**預估**：1-2 天
**動工觸發**：**有人實際抱怨**「agent 跑到一半斷了要重來」

### 為什麼

目前 agent 沒有 checkpoint：iteration 2/3 時 LLM 失敗，整次 run 全部丟掉，要從頭再跑（matches 7 個節點 × 多次 LLM 呼叫，浪費 30-60 秒）。

LangGraph 原生支援 `SqliteSaver` checkpoint，可從上次成功的節點 resume。

### 怎麼做

1. `src/agent/graph.py:build_graph()` 加 `checkpointer=SqliteSaver(...)` 參數
2. SQLite 檔放 `data/checkpoints.db`（gitignored）
3. 清理策略：cron 或啟動時刪除 > 7 天的 thread
4. UI：偵測到未完成的 thread_id 時顯示「Resume previous run?」按鈕

### 為什麼緩

加 checkpoint 會引入：
- SQLite 檔案管理（清理、遷移、壞檔修復）
- thread_id 在 UI session 間的關聯邏輯
- 測試：要驗證每個節點失敗都能 resume

**沒有實際痛點時做這個就是 over-engineering**。等有人吃過 3 次「重跑」苦頭再做。

### 若部署到 GCP — 改用 PostgresSaver

Cloud Run / GKE 容器檔案系統是 ephemeral，SqliteSaver 在 cold start 會掉資料、
replicas 也不共用 → SqliteSaver 在 GCP 上是錯的。改用：

- **PostgresSaver** (`langgraph-checkpoint-postgres`) + **Cloud SQL**（db-f1-micro
  約 $10–20/mo）：和 SqliteSaver 同 API，只換 connection string
- 或 **RedisSaver**（社群版本）+ **Memorystore**：快但 volatile（除非付 persistent tier）

觸發點不變 — 仍是「有人實際抱怨」。GCP 改的是 *怎麼做*，不是 *要不要做*。

---

## #4 — feat: per-user rate limiting

**優先度**：🟢 Low
**預估**：0.5 天
**動工觸發**：**對外公開部署**（multi-user 同時使用）OR 上游 API 開始 throttle

### 為什麼

目前 `app.py` 用 `st.session_state["_last_run_ts"]` + 10 秒冷卻擋濫用，**問題是這只是 per-session**——惡意 user 開無痕視窗就繞過了。

### 怎麼做

1. 把 cooldown 的 key 從 session 改為「APP_PASSWORD hash」或 IP（Streamlit 拿不到 IP，所以用 cookie hash 比較實際）
2. 抽成 decorator：`@rate_limit(seconds=10)` 套在 agent 入口
3. `RATE_LIMIT_SEC` 環境變數可調

### 為什麼緩

- 還沒對外公開時，這是假想敵
- Streamlit Cloud 本身有平台級 rate limit
- 上游（OpenAI / Gemini）的 quota 才是真正瓶頸，這層加了也不會變更穩

### 若部署到 GCP — 大半工作改由 Cloud Armor 承擔

GCP 上有現成的邊緣防護工具，自己寫的 in-process limiter 反而變雞肋：

- **Cloud Armor**（HTTP(S) Load Balancer 前面）：per-IP rate limiting、geo blocks、
  OWASP rule set、基本 DoS 防護。約 $5/mo + per-rule + per-million-requests。
  **比任何自己寫的程式都好**，因為攔在 edge 層 — bad traffic 連容器都碰不到。
- 配 Cloud Armor 後，目前的 `rate_limiter.py` IP 層可以拔掉。保留薄薄一層
  per-session UX cooldown（按鈕 disable 10 秒）即可。
- 多 replica 部署才需要 **Memorystore (Redis)** 後端共享 cooldown；單 replica
  Cloud Run 用記憶體 dict 就夠（Cloud Run 有 per-instance concurrency 限制）。

換句話說：上 GCP 後，這個項目的工作量從 0.5 天縮成「在 GLB 前掛 Cloud Armor 並
寫一條 rate-limit 規則」，0.5 小時搞定。觸發點不變 — 仍是「對外公開部署」。

---

## ✅ #5 — test: smoke-test Streamlit launches in CI

> **已完成 2026-05-09**（commit `2acdc7b`）。CI 改打 `/_stcore/script-health-check`
> 而非單純 `/_stcore/health` — 後者只證明 server 起得來，前者真正執行 script
> 並回 `does_script_run_without_error()`，能抓到 `set_page_config` 順序錯與
> import-time 例外。

**優先度**：🟢 Low
**預估**：1 小時
**動工觸發**：發生「CI 綠但生產壞」事件後

### 為什麼

目前 CI 跑 `compileall + pytest`，但**完全沒驗證 Streamlit 真的能起得來**。如果有人改壞 `set_page_config()` 或 import 順序，CI 不會抓到。

### 怎麼做

CI workflow 加一步：
```yaml
- name: Streamlit smoke test
  run: |
    streamlit run src/ui/app.py --server.port 8501 --server.headless true &
    sleep 10
    curl -fsS http://localhost:8501/_stcore/health
    pkill -f streamlit
```

本地已驗證 `HTTP 200 + /_stcore/health=ok` 可用，搬進 CI 即可。

---

## ✅ #6 — test: 補上 src/ui/chart.py 的 unit tests

> **已完成**（`tests/test_chart.py` 存在，涵蓋 `TestStanceScore` / `TestBuildStanceSeries` / `TestRenderTrendChart` / `TestChartToScrollableHtml`，437 tests 全綠）。

**優先度**：🟡 Medium
**預估**：1-2 小時
**動工觸發**：下次要修改 `chart.py` / 趨勢圖邏輯前；或趨勢圖出現使用者抱怨的行為

### 為什麼

`src/ui/chart.py` 263 行，**0 個 test 涵蓋**。`build_stance_series` 是純資料轉換
（grouping by quarter_b、取最顯著非零 delta、把「無關」季度放進 timeline 當 delta=0），
被 single + multi 兩個 view 同時用 — 一旦 regress，兩個模式的趨勢圖一起壞。

### 怎麼做

1. 新增 `tests/test_chart.py`
2. 涵蓋：
   - 空輸入 → `[]`
   - 單一 contradiction → series 含一筆
   - 同 quarter_b 多筆 → 取最顯著的非零 delta
   - 「無關」/「維持不變」季度 → `delta=0` 但仍出現在 timeline
   - boilerplate（兩段 evidence 完全相同）→ 該 quarter_b 視為非有效
3. 不需測 `render_trend_chart`（Plotly figure 物件結構難 assert，視覺輸出由人眼驗）

### 驗收條件

- [ ] 至少 5 個 test 通過
- [ ] 既有 144 個 pytest 全綠

---

## ✅ #7 — test: 補上 src/core/comparison.py 的 unit tests

> **已完成**（`tests/test_comparison.py` 存在，涵蓋 `run_multi_company` / `build_comparison_table` / `synthesize_diff`，437 tests 全綠）。

**優先度**：🟡 Medium
**預估**：1-2 小時
**動工觸發**：下次要修改多公司比較邏輯前；或多公司模式出現異常結果

### 為什麼

`src/core/comparison.py` 有 `build_comparison_table` / `synthesize_diff` /
`run_multi_company` 三個關鍵函式，**0 個 test**。多公司模式（P0/P1 期間新加）
是目前最年輕的功能，回歸風險最高。

### 怎麼做

1. 新增 `tests/test_comparison.py`
2. 涵蓋：
   - `build_comparison_table`：跨公司季度對齊、缺資料處理、stance_change 翻譯
   - `synthesize_diff`：mock LLM 回傳，驗證 prompt 結構與 fallback
   - `run_multi_company`：ThreadPoolExecutor 路徑可在單一公司失敗時保留其他結果
     （error 寫進回傳 dict 而不是 raise）
3. 不需測 LLM 內容（用 monkeypatch 樁掉 `chat()`）

### 驗收條件

- [ ] 至少 6 個 test 通過
- [ ] run_multi_company 失敗隔離行為有明確覆蓋

---

## ✅ #8 — chore: .env.example 補齊 LangSmith / LLM_PAIR_WORKERS 變數

> **已完成 2026-05-09**。`.env.example` 加上 LANGSMITH_TRACING /
> LANGSMITH_API_KEY / LANGSMITH_PROJECT、LANGCHAIN_TRACING_V2 /
> LANGCHAIN_API_KEY / LANGCHAIN_PROJECT（舊命名相容），以及 LLM_PAIR_WORKERS。

**優先度**：🟢 Low
**預估**：5 分鐘
**動工觸發**：anytime；新人接手時最有感

### 為什麼

程式有讀但 `.env.example` 沒列：
- `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` / `LANGSMITH_TRACING`
- `LANGCHAIN_API_KEY` / `LANGCHAIN_PROJECT` / `LANGCHAIN_TRACING_V2`（舊命名相容）
- `LLM_PAIR_WORKERS`（contradiction batch_detect 並行度）

新人想啟用 LangSmith tracing 得 grep 整個 codebase 才知道變數名 — 是純資訊不對稱
造成的摩擦，5 分鐘就能除掉。

### 怎麼做

在 `.env.example` 適當位置加上有註解的 placeholder（留空 = 不啟用）。
順便檢查 `CLAUDE.md` 的環境變數區塊是否同步更新。

---

## ✅ #9 — fix: 改用原子寫入避免 ingestion_log.json 損毀

> **已完成 2026-05-09**。`scripts/run_ingestion.py:_save_log()` 改成寫到
> `.json.tmp` 後 `os.replace()` 改名 — POSIX rename 在同檔案系統內是原子的，
> SIGKILL / 斷電 / OOM 都不會留下半截 JSON。

**優先度**：🟢 Low
**預估**：15 分鐘
**動工觸發**：實際發生 ingestion log 損毀（症狀：下次跑 ingestion 把所有 PDF 重做一遍）

### 為什麼

`scripts/run_ingestion.py:_save_log()` 用 `Path.write_text(json.dumps(...))`，
**非原子寫入**。寫到一半遇到 SIGKILL / 斷電 / OOM 會留下半截 JSON，
下次 `_load_log` 解析失敗 → 回傳空 dict → 把所有已匯入 PDF 當作新檔重做。

### 怎麼做

```python
def _save_log(log: dict) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INGESTION_LOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, INGESTION_LOG)   # POSIX 原子 rename
```

`os.replace` 在同一檔案系統內是原子的 — 要嘛新檔完全就位，要嘛舊檔還在。

### 為什麼緩

- 觸發條件少：必須是寫的瞬間（毫秒級）才會中斷
- 即使損毀，最壞情況是「重做一次匯入」，不會丟資料
- 本機開發不會碰到，CI / production 才有意義

但「為什麼緩」也是「為什麼不直接修 — 反正只 15 分鐘」。看到這項就修了吧。

---

## ✅ #10 — docs: CLAUDE.md PDF 字型描述與實作對齊

> **已完成 2026-05-09**。CLAUDE.md「Export」節點改寫，列出實際的 fallback
> 順序（macOS STHeiti → Debian/Ubuntu NotoSansCJK → WQYZenHei → AR PL UMing）
> 並說明 Streamlit Cloud / Linux 部署需在 packages.txt 裝 fonts-noto-cjk。

**優先度**：🟢 Low
**預估**：5 分鐘
**動工觸發**：anytime（純文件更新）

### 為什麼

`CLAUDE.md` 寫：

> PDF: `fpdf2` + STHeiti font (`/System/Library/Fonts/STHeiti Light.ttc`)

但 `src/ui/export.py:_resolve_cjk_font()` 實際有完整 cascade：
STHeiti（macOS）→ NotoSansCJK（Debian/Ubuntu）→ WQYZenHei → AR PL UMing。

文件落後實作會誤導兩件事：(a) 部署到 Linux 時誤以為 PDF 匯出會壞 (b) 有人想改字型
時看錯位置。

### 怎麼做

把 `CLAUDE.md` 的「Export」節點改寫，列出實際的 fallback 順序，並 cross-ref
`packages.txt` 在 Streamlit Cloud / Linux 部署的角色（裝 `fonts-noto-cjk`）。

---

## 不做的事（明確 out-of-scope）

- ❌ **AI agent 替代分析師寫研究報告**：本工具是審計工具，不取代分析判斷
- ❌ **股價預測 / 投資建議**：法律 / 合規風險，已寫進免責聲明
- ❌ **使用者帳號系統**：單一 demo 站，加 auth 過度工程；要做就直接接 SSO
- ❌ **支援所有美股**：scope creep；先把台股 4 家做穩
- ❌ **fine-tune 自有模型替代 LLM**：成本/收益不明，等 cost 真的成為瓶頸再評估

---

## 維護紀錄

| 日期 | 動作 |
|---|---|
| 2026-05-03 | 建立此文件，初始 5 項待辦 |
| 2026-05-09 | #5 完成（CI smoke test）；#2 完成（views/ 拆分） |
| 2026-05-09 | #1 補登已完成（事實上由先前 P1-8 UIState 重構達成） |
| 2026-05-09 | #3 / #4 加上 GCP 部署情境註記（PostgresSaver / Cloud Armor） |
| 2026-05-09 | 新增 #6–#10：chart/comparison test、.env.example、ingestion 原子寫入、PDF 字型 doc |
| 2026-05-09 | #8 / #9 / #10 完成（quick wins：env doc、原子寫入、PDF 字型 doc） |
| 2026-05-25 | #6 / #7 補登已完成（test_chart.py / test_comparison.py 已存在，437 tests 全綠） |
| 2026-05-25 | docs: 清除 CLAUDE.md / AGENTS.md / docs/* 中的 Qdrant / sentence-transformers 殘留文字 |

> 新增項目請：①給優先度 ②寫動工觸發條件 ③估時。
> 沒有觸發條件的項目視為「想要但不必要」，不應佔用排程。
