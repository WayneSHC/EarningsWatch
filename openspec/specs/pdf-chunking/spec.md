# pdf-chunking Specification

## Purpose

定義 `src/ingestion/chunker.py` 的智能切割行為：依段落類型（QA / opening / guidance / table）選擇不同策略、`CHUNK_SIZE=500` + `OVERLAP=100` 滑動視窗、`MIN_CHUNK_LEN=50` 過短 chunk 丟棄、guidance / number / topic tag 標註、表格頁不切割、metadata 注入。本 spec 確保切割後的 chunks 既保留完整語意，也帶足夠 metadata 供下游檢索與篩選。

## Requirements

### Requirement: 切割參數 SHALL 鎖定為常數

模組 MUST 暴露下列模組級常數：
- `CHUNK_SIZE == 500`
- `OVERLAP == 100`
- `MIN_CHUNK_LEN == 50`

#### Scenario: 常數值鎖定
- **WHEN** 讀取 `chunker.CHUNK_SIZE / OVERLAP / MIN_CHUNK_LEN`
- **THEN** 分別等於 `500`、`100`、`50`

### Requirement: 表格頁 SHALL 整頁為一個 chunk，section 標為 `"table"`

`chunk_page(page_data)` MUST 在 `page_data["has_table"] == True` 且 `parse_method ∈ {"pdfplumber", "llama_parse"}` 時，將整頁 content 作為單一 chunk，且 `section` 欄位設為 `"table"`。MUST 不對表格內容做滑動視窗切割，避免數字斷裂。

#### Scenario: 表格頁不被切割
- **GIVEN** `page_data["has_table"] == True`、`parse_method == "pdfplumber"`、`content` 長度 1500
- **WHEN** `chunk_page(page_data)` 被呼叫
- **THEN** 回傳長度為 1 的 list
- **AND** 該 chunk 的 `section == "table"`
- **AND** 該 chunk 的 `content` 等於原始 content 長度（未被切割）

### Requirement: 非表格頁 SHALL 依內容自動分類 section

`_classify_section(content)` MUST 回傳下列其中一個：
- `"QA"`：content 含 `分析師` / `提問` / `Q數字 [:：]` / `問[:：]` 模式
- `"guidance"`：content 含 `本季 / 展望 / 預估 / guidance / 毛利率 / 營收 / 指引`（case-insensitive）任一
- `"opening"`：其他

`chunk_page` MUST 對非表格頁先呼叫 `_classify_section`，QA 段落走 `_split_qa`，其他走 `_sliding_window`。

#### Scenario: QA 段落
- **GIVEN** content `"分析師：請問本季毛利率。\n答："`
- **WHEN** `_classify_section(content)` 被呼叫
- **THEN** 回傳 `"QA"`

#### Scenario: Guidance 段落
- **GIVEN** content `"本季毛利率展望 53%"`
- **WHEN** `_classify_section(content)` 被呼叫
- **THEN** 回傳 `"guidance"`

#### Scenario: Opening 段落
- **GIVEN** content `"歡迎各位來到 2024Q3 法說會"`（不含 QA / guidance 關鍵字）
- **WHEN** `_classify_section(content)` 被呼叫
- **THEN** 回傳 `"opening"`

### Requirement: `_sliding_window` SHALL 以 chunk_size / overlap 分塊，過短丟棄

`_sliding_window(text, chunk_size=CHUNK_SIZE, overlap=OVERLAP)` MUST：
- 從 `start=0` 開始，每塊長度 `chunk_size`、重疊 `overlap`（即下一塊起點為 `end - overlap`）
- 每塊 `strip()` 後若 `len < MIN_CHUNK_LEN` MUST 丟棄
- 最後一塊到達文末時停止

#### Scenario: 1200 字元文字切成多塊
- **GIVEN** 1200 字元的 content
- **WHEN** `_sliding_window(content)` 被呼叫（500 / 100）
- **THEN** 回傳至少 2 塊
- **AND** 每塊長度 `<= 500`

#### Scenario: 過短塊被丟棄
- **GIVEN** 30 字元的 content
- **WHEN** `_sliding_window(content)` 被呼叫
- **THEN** 回傳 `[]`（不滿 MIN_CHUNK_LEN）

### Requirement: `_split_qa` SHALL 依 QA 模式切割，過長 QA 對再走滑動視窗

`_split_qa(text)` MUST 用 `QA_REGEX` 切分 text；每個 chunk 為「問題前綴 + 對應回答」。`MIN_CHUNK_LEN` 以下的部分 MUST 丟棄。長度超過 `CHUNK_SIZE * 2`（1000 字元）的 QA pair MUST 再用 `_sliding_window` 二次切割。

當函式內部 chunk 列表為空（例如整段 text 經 strip 後仍短於 `MIN_CHUNK_LEN`），MUST 退回 `_sliding_window(text)` 結果。實務上 `_split_qa` 由 `chunk_page` 在 `_classify_section == "QA"` 時呼叫，故 text 必含 QA 標記。

#### Scenario: 文中含 QA 標記
- **GIVEN** content 含 `"分析師：請問...\n回答內容夠長..."`
- **WHEN** `_split_qa(content)` 被呼叫
- **THEN** 回傳至少 1 塊
- **AND** 該塊以 `分析師` 起始（保留問題前綴）

#### Scenario: text 過短時退回滑動視窗
- **GIVEN** content 經 strip 後長度 < `MIN_CHUNK_LEN`
- **WHEN** `_split_qa(content)` 被呼叫
- **THEN** 回傳值等同於 `_sliding_window(content)`（兩者都會回 `[]`）

### Requirement: 每個 chunk SHALL 帶 guidance / number / topic 三類 tag

`chunk_page` 對每個 chunk MUST 計算：
- `contains_guidance`: bool，content 含中文前瞻詞（`預估 / 展望 / 指引 / 預期 / 下季 / 全年 / 毛利率目標 / ...`）或英文前瞻術語（`guidance / forecast / outlook / we expect / we anticipate / we project / next quarter / full[-]?year / going forward / we target / gross margin\d / revenue\d.*quarter`，case-insensitive）任一
- `contains_number`: bool，content 含 `\d+\.?\d*\s*(%|億|萬|元|美元|季)` 任一
- `topics`: list，依 `AI / 毛利率 / 營收 / 庫存 / 產能` 對應 regex 命中的標籤

#### Scenario: 含「展望」標為 guidance
- **GIVEN** chunk content `"我們對下季展望維持審慎樂觀"`
- **WHEN** `chunk_page` 處理該 chunk
- **THEN** `contains_guidance == True`

#### Scenario: 含 `we expect` 也標為 guidance
- **GIVEN** chunk content `"We expect mid-50% gross margin"`
- **WHEN** `chunk_page` 處理
- **THEN** `contains_guidance == True`

#### Scenario: 含 `53%` 標為 contains_number
- **GIVEN** chunk content `"毛利率 53%"`
- **WHEN** `chunk_page` 處理
- **THEN** `contains_number == True`

#### Scenario: AI / CoWoS 觸發 AI topic
- **GIVEN** chunk content `"CoWoS 產能與 AI 需求"`
- **WHEN** `chunk_page` 處理
- **THEN** `topics` 含 `"AI"`

### Requirement: chunk metadata SHALL 從 page_data 注入

`chunk_page` 對每個 chunk MUST 把 `page_data["metadata"]`（含 `company / quarter / date / stock_code / source_page / source_file / lang` 等）展開到 chunk dict。`chunk_index` MUST 為該頁內 chunk 的序號（從 0 開始）。

#### Scenario: 公司與季度被注入
- **GIVEN** `page_data["metadata"] = {"company": "台積電", "quarter": "2024Q3", "source_page": 5}`
- **WHEN** `chunk_page(page_data)` 被呼叫
- **THEN** 每個 chunk 含 `company == "台積電"`、`quarter == "2024Q3"`、`source_page == 5`

#### Scenario: chunk_index 從 0 起算
- **GIVEN** 一頁切出 3 個 chunk
- **WHEN** `chunk_page(...)` 被呼叫
- **THEN** chunks 的 `chunk_index` 分別為 `0`、`1`、`2`

### Requirement: 空頁面 MUST 回傳空 list

`chunk_page(page_data)` MUST 在 `page_data.get("content", "").strip() == ""` 時直接回傳 `[]`，不呼叫切割函數。

#### Scenario: 空 content
- **WHEN** `chunk_page({"content": "   "})` 被呼叫
- **THEN** 回傳 `[]`

### Requirement: `chunk_document` SHALL 為所有頁的扁平 chunks

`chunk_document(pages)` MUST 對每頁呼叫 `chunk_page` 並用 `extend` 把結果攤平為單一 list。

#### Scenario: 三頁 → 攤平
- **GIVEN** 三頁分別產出 2 / 1 / 3 chunks
- **WHEN** `chunk_document(pages)` 被呼叫
- **THEN** 回傳 list 長度 6
