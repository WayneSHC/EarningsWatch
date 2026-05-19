# tavily-news Specification

## Purpose

定義 `src/agent/tools.py` 對 Tavily 新聞 API 的整合：lazy client + lru_cache 單例、無 key 時 graceful degrade、公司別名過濾（避免同產業不同公司汙染結果）、發布日期解析與容錯、時間排序為主 / relevance 為次的 stable sort、yfinance 股價查詢、`decide_tools` 的 LLM 驅動工具路由與關鍵字降級。本 spec 確保新聞補強流程在缺 key / Tavily 失敗 / 解析失敗下都不影響主流程。

## Requirements

### Requirement: `_get_tavily()` SHALL lazy init 並單例化

`_get_tavily()` MUST 使用 `lru_cache(maxsize=1)`；首次呼叫時嘗試 `get_secret("TAVILY_API_KEY")` 與 `from tavily import TavilyClient`。MUST 在下列情境回傳 `None` 且不 raise：
1. API key 為空字串
2. `tavily` 套件無法 import

未安裝套件時 MUST print 警告含 `tavily-python 未安裝`。

#### Scenario: 缺 key 回 None
- **GIVEN** `TAVILY_API_KEY` 未設定
- **WHEN** `_get_tavily()` 被呼叫
- **THEN** 回傳 `None`，不 raise

#### Scenario: 套件未安裝回 None
- **GIVEN** `TAVILY_API_KEY` 有值，但 `import tavily` raise ImportError
- **WHEN** `_get_tavily()` 被呼叫
- **THEN** 回傳 `None`
- **AND** stdout 含 `tavily-python 未安裝` 字樣

### Requirement: `search_news` SHALL 在無 client 時短路回傳空 list

`search_news(query, company, max_results=5)` MUST 在 `_get_tavily()` 回 `None` 時直接回傳 `[]`，print 一則 `Tavily 未設定` 訊息，不發起任何網路呼叫。

#### Scenario: 無 Tavily 時短路
- **GIVEN** `_get_tavily()` 回 `None`
- **WHEN** `search_news("AI", "台積電")` 被呼叫
- **THEN** 回傳 `[]`

### Requirement: `search_news` SHALL 過濾未提及該公司的新聞

`search_news` MUST 用 `COMPANY_ALIASES`（含中文全稱 / 簡稱 / 英文 / 股票代號）過濾 Tavily 回傳的結果：只保留 `title + content` 經 `lower()` 後含任一別名（`lower()`）的條目。若公司不在 `COMPANY_ALIASES` 中，MUST 用公司名本身作為唯一別名。

#### Scenario: 同產業不同公司被過濾
- **GIVEN** Tavily 回傳一則新聞，title/content 只提到「聯發科」
- **AND** `search_news` 的 company 參數為 `"台積電"`
- **WHEN** `search_news(...)` 被呼叫
- **THEN** 回傳結果不含該則

#### Scenario: 英文別名匹配
- **GIVEN** Tavily 回傳新聞含 `TSMC` 字串、無中文
- **AND** company 為 `"台積電"`
- **WHEN** `search_news(...)` 被呼叫
- **THEN** 該則新聞被保留

#### Scenario: 股票代號匹配
- **GIVEN** 新聞 content 含 `2454`、company 為 `"聯發科"`
- **WHEN** `search_news(...)` 被呼叫
- **THEN** 該則新聞被保留

### Requirement: `search_news` SHALL 依時間優先排序，relevance 為次

`search_news` MUST 對結果做兩階段 stable sort：先依 `score`（Tavily relevance）降冪，再依 `published_date`（解析後的 datetime）降冪。無 `published_date` 的條目 MUST 排到末尾（使用 `datetime.min` UTC 作為 fallback）。

`_parse_pub_date(s)` MUST：
- 支援 ISO 8601 含 `Z` 後綴（取代為 `+00:00`）
- 純日期字串 `YYYY-MM-DD` 加上 UTC tzinfo
- 解析失敗或空字串回傳 `datetime.min.replace(tzinfo=timezone.utc)`

#### Scenario: 較新日期排前
- **GIVEN** 兩則新聞：A `published_date=2024-12-01`（score=0.5）、B `published_date=2024-11-01`（score=0.9）
- **WHEN** `search_news(...)` 完成排序
- **THEN** A 排在 B 之前（日期優先於 score）

#### Scenario: 無日期排末尾
- **GIVEN** 兩則新聞：A 無 published_date、B `published_date=2024-01-01`
- **WHEN** `search_news(...)` 完成排序
- **THEN** B 排在 A 之前

#### Scenario: Z 後綴解析
- **WHEN** `_parse_pub_date("2024-10-17T08:00:00Z")` 被呼叫
- **THEN** 回傳 `datetime(2024, 10, 17, 8, 0, 0, tzinfo=UTC)`

#### Scenario: 純日期加 UTC
- **WHEN** `_parse_pub_date("2024-10-17")` 被呼叫
- **THEN** 回傳值的 `tzinfo` 不為 `None`

#### Scenario: 解析失敗回 fallback
- **WHEN** `_parse_pub_date("not a date")` 被呼叫
- **THEN** 回傳 `datetime.min` 且 tzinfo 為 UTC

### Requirement: `search_news` 失敗 SHALL 不 raise 並回傳空 list

Tavily API 呼叫 raise 時 MUST 被捕獲，print 錯誤訊息（含原例外）並回傳 `[]`，不影響 Agent 主流程。

#### Scenario: Tavily raise 時降級
- **GIVEN** `client.search(...)` raise
- **WHEN** `search_news(...)` 被呼叫
- **THEN** 回傳 `[]`
- **AND** stdout 含 `Tavily 搜尋失敗` 字樣

### Requirement: `get_stock_price` SHALL 用 `STOCK_CODE_MAP` 轉換代號

`get_stock_price(company, period="1y")` MUST 從 `STOCK_CODE_MAP` 查 ticker symbol；查不到 MUST 回傳 `{"error": "找不到 {company} 的股票代號"}`，不呼叫 yfinance。yfinance 例外 MUST 被捕獲，回傳 `{"error": str(e)}`。

#### Scenario: 未知公司
- **WHEN** `get_stock_price("未知公司")` 被呼叫
- **THEN** 回傳 dict 含 `error` 鍵
- **AND** yfinance SDK 不被呼叫

#### Scenario: 已知公司用對應 ticker
- **GIVEN** `STOCK_CODE_MAP["台積電"] == "TSM"`
- **WHEN** `get_stock_price("台積電")` 被呼叫
- **THEN** `yf.Ticker("TSM")` 被建立

### Requirement: `decide_tools` SHALL LLM 驅動，失敗時降級為關鍵字匹配

`decide_tools(query, topic)` MUST 嘗試呼叫 LLM 解析 JSON `{"tools": [...], "reasoning": "..."}`；`tools` 中只接受 `TOOL_SPECS` 列出的名稱。LLM 失敗、回傳缺 `tools` 鍵、或結果為空 MUST 降級為 `decide_tools_by_keyword(query, topic)`。`bigquery` MUST 永遠包含（`always_required=True`）。

#### Scenario: bigquery 永遠存在
- **GIVEN** LLM 回 `{"tools": ["tavily"]}`
- **WHEN** `decide_tools(...)` 被呼叫
- **THEN** 回傳含 `"bigquery"` 與 `"tavily"`

#### Scenario: LLM 失敗降級為關鍵字
- **GIVEN** LLM raise、query 含 `"最新"`
- **WHEN** `decide_tools(query, topic)` 被呼叫
- **THEN** 回傳含 `"bigquery"` 與 `"tavily"`（關鍵字觸發）

#### Scenario: LLM 回未知工具被過濾
- **GIVEN** LLM 回 `{"tools": ["bigquery", "fake_tool"]}`
- **WHEN** `decide_tools(...)` 被呼叫
- **THEN** 回傳只含 `"bigquery"`

### Requirement: `decide_tools_by_keyword` SHALL 為降級路徑

`decide_tools_by_keyword(query, topic)` MUST 永遠回傳含 `"bigquery"` 的 list；當 `query + topic` 含 `_NEWS_KEYWORDS` 任一詞時加入 `"tavily"`；含 `_STOCK_KEYWORDS` 任一詞時加入 `"yfinance"`。

#### Scenario: 前瞻關鍵字觸發 Tavily
- **WHEN** `decide_tools_by_keyword("未來 AI 需求展望", "AI")` 被呼叫
- **THEN** 回傳含 `"bigquery"` 與 `"tavily"`

#### Scenario: 股價關鍵字觸發 yfinance
- **WHEN** `decide_tools_by_keyword("股價漲跌", "")` 被呼叫
- **THEN** 回傳含 `"bigquery"` 與 `"yfinance"`

#### Scenario: 無觸發關鍵字只回 bigquery
- **WHEN** `decide_tools_by_keyword("毛利率歷史", "毛利率")` 被呼叫
- **THEN** 回傳 `["bigquery"]`
