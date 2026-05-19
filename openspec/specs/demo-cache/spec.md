# demo-cache Specification

## Purpose

定義 `src/ui/cache.py` 的 Demo 保底快取與 HTML 字串清理工具：MD5 cache key 涵蓋公司 / 主題 / 季度 / 自訂查詢、`load_cache` graceful read、`save_to_cache` atomic write、`retrieved` chunks 不寫入（體積考量）、`sanitize_str` 對所有 LLM 輸出統一 escape。本 spec 確保（a）LLM / BigQuery 故障時 demo 仍可展示結果、（b）並行 / 中斷不會留下半截 JSON、（c）插入 HTML 的字串都有 XSS 防護。

## Requirements

### Requirement: `sanitize_str(val)` SHALL `html.escape` 任意值

`sanitize_str(val)` MUST 對 `None` 回傳 `""`（不為 `"None"`）；對其他值 MUST 先 `str(val)` 再 `html.escape(...)`，使 `<` / `>` / `&` / `"` / `'` 被轉義。

#### Scenario: None 安全
- **WHEN** `sanitize_str(None)` 被呼叫
- **THEN** 回傳 `""`

#### Scenario: HTML 特殊字元被轉義
- **WHEN** `sanitize_str("<script>alert(1)</script>")` 被呼叫
- **THEN** 回傳含 `&lt;script&gt;`，不含原始 `<script>`

#### Scenario: 非字串值被轉字串
- **WHEN** `sanitize_str(123)` 被呼叫
- **THEN** 回傳 `"123"`

### Requirement: `cache_key` SHALL 涵蓋公司 / 主題 / 排序後季度 / 自訂查詢

`cache_key(company, topic, quarters, custom_query)` MUST 用 `f"{company}|{topic}|{sorted(quarters or [])}|{custom_query.strip()}"` 計算 MD5；MUST 對相同輸入回傳相同 key、不同輸入回傳不同 key（含 quarters 順序不影響）。

#### Scenario: 相同輸入相同 key
- **WHEN** `cache_key("A", "AI", ["Q1"], "")` 被呼叫兩次
- **THEN** 回傳值相同

#### Scenario: quarters 順序不影響 key
- **GIVEN** quarters = `["2024Q2", "2024Q1"]` 與 `["2024Q1", "2024Q2"]`
- **WHEN** 分別呼叫 `cache_key(...)`
- **THEN** 兩者回傳相同 key（因 `sorted()`）

#### Scenario: 自訂查詢進入 key
- **GIVEN** 兩次呼叫除 `custom_query` 不同（一空一非空）
- **WHEN** 比較 key
- **THEN** 兩 key 不同

### Requirement: `load_cache()` SHALL 對檔案不存在 / 損毀 graceful

`load_cache()` MUST 在下列情境回傳 `{}` 而非 raise：
1. `CACHE_PATH` 不存在
2. 檔案存在但解析失敗（`json.JSONDecodeError`、檔案損毀、權限錯誤等）

#### Scenario: 檔案不存在
- **GIVEN** `CACHE_PATH` 不存在
- **WHEN** `load_cache()` 被呼叫
- **THEN** 回傳 `{}`，不 raise

#### Scenario: 檔案損毀
- **GIVEN** `CACHE_PATH` 為非合法 JSON 內容
- **WHEN** `load_cache()` 被呼叫
- **THEN** 回傳 `{}`，不 raise

### Requirement: `get_cached_result` SHALL 命中時回傳 entry，未命中回 None

`get_cached_result(company, topic, quarters, custom_query)` MUST 在快取中對應 key 存在時回傳該 entry dict，不存在時回傳 `None`。

#### Scenario: 未命中
- **GIVEN** 空 cache
- **WHEN** `get_cached_result("A", "AI")` 被呼叫
- **THEN** 回傳 `None`

#### Scenario: 命中
- **GIVEN** 已 `save_to_cache("A", "AI", {"final_report": "x"})`
- **WHEN** `get_cached_result("A", "AI")` 被呼叫
- **THEN** 回傳 dict 含 `final_report == "x"`

### Requirement: `save_to_cache` SHALL atomic write + 不存 chunks

`save_to_cache(company, topic, result, quarters, custom_query)` MUST：
1. 用 `tempfile.mkstemp(dir=CACHE_PATH.parent)` 建臨時檔，先寫入再 `os.replace` 覆寫目標 — 保證寫入是原子的
2. 寫入失敗時 MUST `os.unlink` 臨時檔再 re-raise
3. 保留下列 result 欄位：`final_report` / `contradictions` / `promises` / `steps_log` / `confidence` / `iteration` / `tool_plan` / `news_context` / `stock_data` / `sub_queries` / `node_timings`
4. `retrieved` 欄位 MUST 只保留季度 key（值為空 list），避免向量 chunks 把快取撐爆

#### Scenario: 寫入後可讀回
- **WHEN** `save_to_cache("A", "AI", {"final_report": "x"})` 被呼叫
- **AND** 之後 `load_cache()`
- **THEN** 回傳 dict 中對應 key 的 entry `final_report == "x"`

#### Scenario: retrieved 只保留季度 key
- **GIVEN** result `{"retrieved": {"2024Q1": [{"big": "chunk"}], "2024Q2": [...]}}`
- **WHEN** `save_to_cache(...)` 完成
- **THEN** 寫入的 cache entry 中 `retrieved == {"2024Q1": [], "2024Q2": []}`

#### Scenario: 父目錄不存在時自動建立
- **GIVEN** `CACHE_PATH.parent` 不存在
- **WHEN** `save_to_cache(...)` 被呼叫
- **THEN** 目錄被建立、寫入成功
