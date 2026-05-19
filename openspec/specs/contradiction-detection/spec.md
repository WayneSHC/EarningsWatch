# contradiction-detection Specification

## Purpose

定義 `src/core/contradiction.py` 模組對「跨季發言一致性」與「承諾兌現」的偵測行為：包含季度對策略、boilerplate 過濾、JSON 萃取的三層 fallback、LLM 引文驗證（Evidence Verifier）、token 截斷、per-pair 容錯。本 spec 確保 LLM 在語意分析任務上的回傳能被乾淨地解析、虛構引文不會污染結論，且單一 LLM 呼叫失敗不會中止整批比對。

## Requirements

### Requirement: `batch_detect` SHALL 依 `pair_mode` 選擇季度比對策略

`batch_detect(statements_by_quarter, topic, pair_mode="adjacent", chunks_per_pair=4)` MUST 在 `pair_mode="adjacent"`（預設）時，對排序後的季度產生 N-1 個相鄰對；在 `pair_mode="all_pairs"` 時產生 N*(N-1)/2 個全組合對。任何其他 `pair_mode` 值 MUST 退化為 `"adjacent"` 行為（與目前實作一致）。

#### Scenario: adjacent 預設產生 N-1 對
- **GIVEN** `statements_by_quarter` 含 4 個季度 `{2024Q1, 2024Q2, 2024Q3, 2024Q4}`，每季皆有非空 chunks
- **WHEN** `batch_detect(..., pair_mode="adjacent")` 被呼叫
- **THEN** LLM `detect_contradiction` 至多被呼叫 3 次，對應比對 `(Q1,Q2), (Q2,Q3), (Q3,Q4)`

#### Scenario: all_pairs 產生 N*(N-1)/2 對
- **GIVEN** 同上 4 季資料
- **WHEN** `batch_detect(..., pair_mode="all_pairs")` 被呼叫
- **THEN** LLM 至多被呼叫 6 次，涵蓋所有兩兩組合

### Requirement: `batch_detect` MUST 對每季內容套用 _MAX_CONTENT=2000 字元截斷

`batch_detect` MUST 在送進 LLM 之前，將每季 chunks `payload.content` 串接後截斷至 2000 字元；目的是防止 token 過長造成 API 超時或費用暴增。

#### Scenario: 超長內容被截斷
- **GIVEN** 一季的 `content` 串接後超過 2000 字元
- **WHEN** `batch_detect(...)` 被呼叫
- **THEN** 實際傳給 `detect_contradiction` 的 `stmt["content"]` 長度 `≤ 2000`

### Requirement: 兩季任一內容為空 MUST 跳過比對

`batch_detect` MUST 在任一季 chunks 為空 OR 串接 content 經 `strip()` 後為空時，跳過該季度對的 LLM 呼叫；MUST 不 raise，並印出包含季度名的警告訊息。

#### Scenario: 一季 chunks 為空時跳過
- **GIVEN** `statements_by_quarter = {"2024Q1": [...有效chunks...], "2024Q3": []}`
- **WHEN** `batch_detect(...)` 被呼叫
- **THEN** 不會對 `(Q1,Q3)` 呼叫 LLM
- **AND** 回傳結果不含該 pair

#### Scenario: chunks 存在但 content 全為空白時跳過
- **GIVEN** 一季 chunks 的 `payload.content` 全是空字串或空白
- **WHEN** `batch_detect(...)` 被呼叫
- **THEN** 該季度對被跳過，並在 stdout 印出包含季度名的警告

### Requirement: 兩季皆為 boilerplate 時 MUST 跳過比對

`_is_boilerplate(text)` 識別法律免責聲明（forward-looking statements / safe harbor / 前瞻性陳述 等）。`batch_detect` MUST 在兩季 content 同時被 `_is_boilerplate` 判定為 boilerplate 時跳過該季度對的 LLM 呼叫，並印出包含兩季名稱的警告。

#### Scenario: 兩季都是免責聲明
- **GIVEN** 兩季 content 都含 `前瞻性陳述` 或 `forward-looking statements subject to significant risks`
- **WHEN** `batch_detect(...)` 被呼叫
- **THEN** 該季度對不送進 LLM，不出現在回傳結果中

#### Scenario: boilerplate 判斷大小寫與空白不敏感
- **WHEN** `_is_boilerplate("FORWARD-LOOKING STATEMENTS Subject To Significant Risks")` 被呼叫
- **THEN** 回傳 `True`
- **AND** `_is_boilerplate("前瞻 性 陳　述")`（含全形空白與 ASCII 空白）也回傳 `True`

#### Scenario: 真正主題內容不被誤判
- **WHEN** `_is_boilerplate("台積電本季毛利率為 53%，受惠於 N3 良率提升")` 被呼叫
- **THEN** 回傳 `False`

### Requirement: 單一季度對失敗 MUST 不影響其餘比對

`batch_detect` MUST 對每組季度對的 LLM 呼叫獨立 try/except；任一對失敗只造成該對的 `analysis` 降級為預設空殼（`stance_change="無關"`、`has_contradiction=False`、`confidence=0.0`），其他季度對的結果 MUST 正常回傳。

#### Scenario: 一對失敗時其餘正常回傳
- **GIVEN** 三季資料，LLM 在第二對呼叫時 raise
- **WHEN** `batch_detect(...)` 被呼叫
- **THEN** 回傳列表長度仍為 2（adjacent 模式）
- **AND** 失敗對的 `analysis.confidence == 0.0` 且 `analysis.change_detail` 含 `偵測失敗`

### Requirement: `_extract_json` MUST 提供四層 fallback

`_extract_json(text)` MUST 嘗試以下順序解析 LLM 回應：
1. `json.loads(text.strip())`
2. Markdown fence `` ```json ... ``` ``（同時匹配 `{}` 與 `[]`）
3. 首尾掃描（`find('{')` 配對 `rfind('}')` 與 `find('[')` 配對 `rfind(']')`，取最早出現的合法候選）
4. 全部失敗 → 回傳預設 dict（`confidence=0.0`、`stance_change="無關"` 等）並印警告

任何路徑 MUST 不 raise；MUST 回傳 `dict` 或 `list` 之一。

#### Scenario: 直接 JSON parse 成功
- **WHEN** `_extract_json('{"stance_change": "更樂觀", "confidence": 0.9}')` 被呼叫
- **THEN** 回傳 dict 含 `stance_change == "更樂觀"`

#### Scenario: Markdown fence 包裹的 JSON
- **WHEN** `_extract_json('```json\n{"a": 1}\n```')` 或無 lang tag 的 fence 被呼叫
- **THEN** 回傳 `{"a": 1}`

#### Scenario: 首尾掃描處理外圍雜訊
- **WHEN** `_extract_json('Sure! Here is the result: {"x": 2} thanks!')` 被呼叫
- **THEN** 回傳 `{"x": 2}`

#### Scenario: 完全無法解析時降級回傳並警告
- **GIVEN** 一段不含任何合法 JSON 的純文字
- **WHEN** `_extract_json(...)` 被呼叫
- **THEN** 回傳預設 dict 含 `confidence == 0.0`、`stance_change == "無關"`
- **AND** stdout 印出包含 `JSON 解析失敗` 字樣的警告

### Requirement: `detect_contradiction` MUST 校正季度時序

`detect_contradiction(stmt_a, stmt_b, topic)` MUST 在 `stmt_a["quarter"] > stmt_b["quarter"]`（字典序）時，自動交換兩個 statement，確保 prompt 中的「較早季度」永遠出現在 `evidence_early` 對應的位置。MUST 對非 dict 輸入 raise `ValueError`。

#### Scenario: 反序輸入被交換
- **GIVEN** `stmt_a = {"quarter": "2024Q3", ...}`、`stmt_b = {"quarter": "2024Q1", ...}`
- **WHEN** `detect_contradiction(stmt_a, stmt_b, "AI需求")` 被呼叫
- **THEN** prompt 中的「較早季度」為 `2024Q1`，較晚為 `2024Q3`

#### Scenario: 非 dict 輸入立刻 raise
- **WHEN** `detect_contradiction(None, {}, "topic")` 被呼叫
- **THEN** raise `ValueError`

### Requirement: Evidence Verifier MUST 在引文驗證失敗時降低 confidence 並清空引文

`batch_detect` 對每個季度對的 LLM 結果 MUST 呼叫 `_verify_quote(quote, source_text)` 驗證 `evidence_early` / `evidence_later` 是否確實出現於該季 content：
- 精確子串 → 接受
- 模糊滑動視窗（`SequenceMatcher.ratio() ≥ 0.85`，視窗寬度 ≈ `quote × 1.4`）→ 接受並設 `evidence_*_fuzzy = True`
- 兩者皆失敗 → 設 `verification_failed = True`、`confidence -= 0.2`（最低 0.0）、該欄引文清空（`""`）

少於 `_MIN_QUOTE_LEN=10` 字元的 quote MUST 視為通過（避免短詞誤判）。

#### Scenario: 精確匹配通過
- **GIVEN** LLM 回傳 `evidence_early == "本季毛利率提升至 53%"`，該語句出現在 `stmt_a.content`
- **WHEN** `batch_detect(...)` 完成該對
- **THEN** 該 pair 的 `analysis.evidence_early` 保留原文
- **AND** `analysis.verification_failed` 未被設或為 `False`

#### Scenario: 完全幻覺引文時降分清空
- **GIVEN** LLM 回傳 `evidence_early` 為一段未出現在 source 的虛構句子
- **WHEN** `batch_detect(...)` 完成該對
- **THEN** `analysis.evidence_early == ""`
- **AND** `analysis.verification_failed == True`
- **AND** `analysis.confidence` 比 LLM 原回傳值低 0.2（下限 0.0）

#### Scenario: 模糊匹配通過時加標記
- **GIVEN** LLM 引文與原文僅標點 / 字元級小差異（`ratio ≥ 0.85`）
- **WHEN** `batch_detect(...)` 完成該對
- **THEN** `analysis.evidence_early` 保留原引文
- **AND** `analysis.evidence_early_fuzzy == True`

### Requirement: 來源頁碼 MUST 從 chunks 去重萃取

`_extract_sources(chunks)` MUST 從每個 chunk 的 `payload.source_file` + `payload.source_page` 萃取唯一的 `(file, page)` 組合並回傳為 `[{file, page}, ...]`；重複組合 MUST 去重；缺 `source_file` 的 chunk MUST 跳過。`batch_detect` 結果的 `sources_a` / `sources_b` 來自此函數。

#### Scenario: 重複頁碼去重
- **GIVEN** chunks 含三筆，其中兩筆 `source_file` 與 `source_page` 完全相同
- **WHEN** `_extract_sources(chunks)` 被呼叫
- **THEN** 回傳長度為 2（去重一筆）

### Requirement: `detect_promises` MUST 跳過無前瞻承諾的季度對

`detect_promises(chunks_by_quarter, topic)` MUST 比對排序後相鄰的兩季（`q_prev → q_next`），對 LLM 回傳 `has_promise == False` 的 pair MUST 不出現在結果中。結果項目 MUST 包含 `promise_quarter`、`followup_quarter`、`content`、`status`（含 emoji 前綴）、`detail`、`confidence`。

#### Scenario: 無承諾的 pair 被過濾
- **GIVEN** LLM 對所有 pair 回傳 `{"has_promise": false}`
- **WHEN** `detect_promises(...)` 被呼叫
- **THEN** 回傳空 list

#### Scenario: 達標 / 未兌現 / 不明分別對應 emoji
- **GIVEN** LLM 回傳 `status` 為 `"達標"` / `"未兌現"` / `"不明"`
- **WHEN** `detect_promises(...)` 完成
- **THEN** 對應 `result.status` 分別為 `"✅ 達標"` / `"❌ 未兌現"` / `"⚠ 不明"`

#### Scenario: 單一 task 失敗不影響其他承諾
- **GIVEN** 三季資料，LLM 在第一個 pair raise
- **WHEN** `detect_promises(...)` 被呼叫
- **THEN** 第二個 pair 的結果（若有 `has_promise`）正常出現，不 raise
