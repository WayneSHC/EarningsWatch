# report-generation Specification

## Purpose

定義 LangGraph `report` 節點 `report_generator`（`src/agent/nodes.py`）的行為：棄權報告、cost guard 提示、直接回答合成、off-topic 偵測與網路新聞 fallback、跨季比對 / 承諾追蹤 / 新聞 / 股價區塊、來源索引、XSS escaping、URL 白名單。本 spec 確保最終 Markdown 報告在資料不足 / 主題未涵蓋 / LLM 失敗等情境下都有合理輸出，且所有 LLM / 外部來源字串插入前都經過轉義。

## Requirements

### Requirement: `abstain == True` SHALL 輸出棄權報告

`report_generator(state)` MUST 在 `state["abstain"] == True` 時輸出「資料不足，無法完成分析」的棄權報告，取代常規偵查報告。棄權報告 MUST 包含：標題、公司 / 主題 / 信心度、系統說明、發現的問題（`reflection_issues`）、仍缺乏的資訊（`reflection_gaps`）、改善建議。MUST 不進入直接回答 / 跨季比對等區塊。

#### Scenario: 棄權狀態輸出說明報告
- **GIVEN** `state["abstain"] == True`、`reflection_issues` 非空
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `final_report` 含「資料不足」字樣
- **AND** 含 `reflection_issues` 中的每一條問題
- **AND** 不含「跨季發言比對」區塊

### Requirement: `cost_guard_triggered` SHALL 在報告加註提示

`report_generator` MUST 在 `state["cost_guard_triggered"] == True`（且非棄權）時，於報告前段加入一則「預算保護觸發」提示，說明 Self-Reflection 因成本上限提早結束。

#### Scenario: cost guard 觸發時報告含提示
- **GIVEN** `state["cost_guard_triggered"] == True`、`abstain == False`
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `final_report` 含「預算保護觸發」字樣

### Requirement: 直接回答 SHALL 由 LLM 合成、失敗時降級

`report_generator` MUST 在有 `query` 與 `retrieved` 時，彙整各季前 2 筆 chunk（總長截斷至 4000 字元）送 LLM 合成一段 150~300 字的直接回答（`mode="demo"`、`max_tokens=600`）。LLM 失敗 MUST 捕獲，用 `friendly_error_message(e)` 顯示乾淨摘要（原始 SDK 例外只進 stdout），報告改放一則降級提示且繼續輸出其餘區塊。

#### Scenario: 正常合成直接回答
- **GIVEN** `query` 與 `retrieved` 非空、LLM 正常回應
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `final_report` 含「## 直接回答」區塊與 LLM 回應內容

#### Scenario: LLM 失敗時降級不中斷
- **GIVEN** 直接回答的 LLM 呼叫 raise
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `final_report` 含「## 直接回答」區塊與一則降級提示（friendly message）
- **AND** 報告仍繼續輸出跨季比對 / 承諾追蹤等區塊
- **AND** 不 raise

### Requirement: off-topic 偵測 SHALL 切換為網路新聞補充模式

`report_generator` MUST 在下列任一情境判定為 off-topic：
1. 直接回答文字命中 `_is_off_topic_answer()`（含「並未提及 / 未涵蓋 / 無法回答」等 `_OFF_TOPIC_PHRASES`）
2. `retrieved` 所有季度的 chunk 總數為 `0`

off-topic 時 MUST 跳過跨季比對 / 承諾追蹤 / 趨勢分析（對未涵蓋主題無意義），改以網路新聞為主呈現。`news_context` 為空時 MUST 現場觸發一次 `search_news()` 補強。

#### Scenario: 直接回答表示主題未涵蓋
- **GIVEN** LLM 直接回答含「資料中並未提及此主題」
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `final_report` 含「本主題未在法說會逐字稿中找到相關內容」字樣
- **AND** 不含「## 一、跨季發言比對」區塊

#### Scenario: 零檢索結果也判 off-topic
- **GIVEN** `retrieved` 為空 dict（總 chunk 數 0）
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** 進入 off-topic 模式

#### Scenario: off-topic 且無新聞時現場觸發 Tavily
- **GIVEN** off-topic 判定成立、`news_context` 為空
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `search_news()` 被現場呼叫一次

### Requirement: 常規報告 SHALL 含跨季比對 / 承諾 / 新聞 / 股價 / 來源索引

非 off-topic、非棄權時，`report_generator` MUST 依序輸出下列區塊：
- 「## 一、跨季發言比對」：列出每組 contradiction，含立場變化、具體改變、雙季原文引用（含來源頁碼）、建議追問
- 「## 二、承諾兌現追蹤」：列出每筆 promise
- 「## 三、即時新聞背景」：有 `news_context` 時列出（最多 3 則）
- 「## 四、股價參考」：`stock_data` 無 `error` 時列出
- 「## 來源索引」：列出 `retrieved` 中所有去重後的來源檔案與頁碼

#### Scenario: 完整資料輸出全部區塊
- **GIVEN** 非 off-topic、有 contradictions / promises / news / stock
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `final_report` 含「跨季發言比對」「承諾兌現追蹤」「即時新聞背景」「股價參考」「來源索引」五個區塊

#### Scenario: 無 contradictions 時顯示資料不足提示
- **GIVEN** `contradictions` 為空、非 off-topic
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** 跨季比對區塊含「資料不足，無法完成跨季比對」字樣

### Requirement: 所有 LLM / 外部來源字串 MUST 經 XSS escaping 與 URL 白名單

`report_generator` MUST：
- 把 LLM 回傳字串（`stance_change` / `change_detail` / `evidence_*` / `follow_up_question` / 承諾內容 / 直接回答等）插入報告前一律 `html.escape()`（`_he`）
- 新聞 URL 僅接受 `http://` / `https://` 開頭，其他協定（`javascript:` / `data:` 等）一律捨棄為空字串
- 新聞標題移除 Markdown link 特殊字元（`[` `]` `(` `)`）

#### Scenario: LLM 輸出被轉義
- **GIVEN** 某 contradiction 的 `change_detail` 含 `<script>`
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** `final_report` 中該段為 `&lt;script&gt;`，不含原始 `<script>`

#### Scenario: 非 http(s) URL 被捨棄
- **GIVEN** 某新聞 `url` 為 `javascript:alert(1)`
- **WHEN** `report_generator(state)` 被呼叫
- **THEN** 該新聞不以連結形式呈現（URL 被捨棄為空）

### Requirement: `_clean_news_snippet` SHALL 過濾 Markdown 雜質與 SPA fallback 文字

`_clean_news_snippet(text, max_len=180)` MUST：
- text 含 `_NEWS_BOILERPLATE` 任一片段（「請啟用 JavaScript」「更新瀏覽器」等）→ 回傳 `""`
- 移除 Markdown 結構字元（`# * ` > = ~ _ [ ]`）
- 摺疊連續空白
- 截斷至 `max_len` 字元

#### Scenario: SPA fallback 文字被清空
- **WHEN** `_clean_news_snippet("請啟用 JavaScript 以繼續瀏覽")` 被呼叫
- **THEN** 回傳 `""`

#### Scenario: Markdown 雜質被移除
- **WHEN** `_clean_news_snippet("## 標題 **粗體** [連結]")` 被呼叫
- **THEN** 回傳值不含 `#` / `*` / `[` / `]`

#### Scenario: 過長片段被截斷
- **GIVEN** 一段超過 180 字元的乾淨文字
- **WHEN** `_clean_news_snippet(text)` 被呼叫
- **THEN** 回傳長度 `<= 180`

### Requirement: `report_generator` 回傳 SHALL 含 `final_report` 與 `steps_log`

`report_generator` 回傳 dict MUST 至少含 `final_report`（Markdown 字串）與 `steps_log`（list）。off-topic 模式若現場觸發 Tavily，回傳 dict MUST 同時更新 `news_context`。

#### Scenario: 回傳結構
- **WHEN** `report_generator(state)` 在任一路徑完成
- **THEN** 回傳 dict 含 `final_report`（str）與 `steps_log`（list）
