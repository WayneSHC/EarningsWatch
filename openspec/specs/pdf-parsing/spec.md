# pdf-parsing Specification

## Purpose

定義 `src/ingestion/smart_parser.py` 對 PDF 法說會逐字稿的兩階段解析：pdfplumber 為主、LlamaParse 為失敗補救；表格 → 自然語言敘述、純文字 → 直接抽取；亂碼偵測；metadata 注入；空頁過濾。本 spec 確保（a）表格頁的數字語意可被向量模型理解、（b）pdfplumber 抽不出表格時可選擇性走 LlamaParse、（c）缺 LlamaParse key 時主流程不中斷。

## Requirements

### Requirement: `parse_pdf` SHALL 採用 pdfplumber → LlamaParse 兩階段策略

`parse_pdf(pdf_path, company, quarter, date, stock_code, lang="", fallback_to_llama=True)` MUST：
1. **Phase 1**：用 `pdfplumber` 逐頁呼叫 `parse_page_with_pdfplumber`，收集所有 `parse_success == False` 的頁碼至 `failed_pages`
2. **Phase 2**：僅在 `fallback_to_llama == True` 且 `_get_llama_parser()` 回傳非 `None` 且 `failed_pages` 非空時，呼叫 LlamaParse 對失敗頁面補救
3. 過濾 `content.strip() == ""` 的頁，回傳剩餘有效頁列表

#### Scenario: 全頁 pdfplumber 成功時跳過 LlamaParse
- **GIVEN** 所有頁 `parse_success == True`
- **WHEN** `parse_pdf(...)` 被呼叫
- **THEN** LlamaParse `load_data` 不被呼叫

#### Scenario: 失敗頁 + 啟用 LlamaParse 時觸發補救
- **GIVEN** 有 2 頁失敗、`fallback_to_llama=True`、`LLAMA_CLOUD_API_KEY` 有效
- **WHEN** `parse_pdf(...)` 被呼叫
- **THEN** LlamaParse `load_data` 被呼叫

#### Scenario: 缺 LlamaParse key 時主流程不中斷
- **GIVEN** `LLAMA_CLOUD_API_KEY` 未設、有頁面失敗
- **WHEN** `parse_pdf(...)` 被呼叫
- **THEN** 回傳 pdfplumber 階段的結果（即使含失敗頁），不 raise

#### Scenario: LlamaParse 補救失敗時不中斷
- **GIVEN** `LlamaParse.load_data` raise
- **WHEN** `parse_pdf(...)` 被呼叫
- **THEN** 函數正常結束，回傳 pdfplumber 結果

### Requirement: `parse_page_with_pdfplumber` SHALL 抓表格與純文字並合併

`parse_page_with_pdfplumber(page)` MUST：
1. 呼叫 `page.extract_tables()`；若回傳非空，設 `has_table = True`，對每個 table 嘗試 `_table_to_narrative` 轉成自然語言文字（含 `（第N頁財務表格）` 前綴），失敗或結果非 valid 時設 `parse_success = False`
2. 呼叫 `page.extract_text()` 取純文字
3. 合併 `[raw_text, *table_texts]`（過濾空字串）為最終 `content`
4. 若最終 `content` 非 valid narrative，`parse_success = False`

回傳 dict MUST 含 `page` / `content` / `has_table` / `parse_method ("pdfplumber")` / `parse_success`。

#### Scenario: 表格與文字並存時兩者合併
- **GIVEN** 頁面同時含一個合法表格與一段文字
- **WHEN** `parse_page_with_pdfplumber(page)` 被呼叫
- **THEN** `content` 含表格自然語言敘述 + 原始文字
- **AND** `has_table == True`
- **AND** `parse_success == True`

#### Scenario: 表格抽不出時 parse_success=False
- **GIVEN** 表格 `extract_tables()` raise（或 narrative 不 valid）
- **WHEN** `parse_page_with_pdfplumber(page)` 被呼叫
- **THEN** `parse_success == False`
- **AND** 函數仍正常 return（不 raise）

### Requirement: `_table_to_narrative` SHALL 將 DataFrame 轉為「欄位：值」格式

`_table_to_narrative(df, page_num)` MUST：
- 以 `（第{page_num}頁財務表格）` 為第一行
- 每行 DataFrame row 轉成 `"欄位A：值A、欄位B：值B"` 格式（用全形頓號 `、` 串接）
- 跳過 `NaN` 或 strip 後為空的 cell
- 所有行用 `\n` 串接

#### Scenario: 含 NaN 的列被跳過 cell
- **GIVEN** DataFrame `{"季度": "2024Q1", "營收": NaN, "毛利率": "53%"}`
- **WHEN** `_table_to_narrative(df, 5)` 被呼叫
- **THEN** 輸出含 `季度：2024Q1` 與 `毛利率：53%`，不含 `營收` 對應 cell

#### Scenario: 頁碼前綴正確
- **WHEN** `_table_to_narrative(df, 7)` 被呼叫
- **THEN** 輸出第一行為 `（第7頁財務表格）`

### Requirement: `_is_valid_narrative` SHALL 偵測亂碼 / 過短文字

`_is_valid_narrative(text)` MUST 回傳 `True`（有效）僅當：
- `text` 經 strip 後長度 `>= 20`
- 不滿足「中文字符 == 0 且 數字 / 空白 / 符號比例 > 0.8」（純數字表格 → pdfplumber 通常抽不出語意）

#### Scenario: 過短文字無效
- **WHEN** `_is_valid_narrative("hi")` 被呼叫
- **THEN** 回傳 `False`

#### Scenario: 純數字無效
- **WHEN** `_is_valid_narrative("12.3 45.6 7.8 9.0 1.2 3.4 5.6 7.8 9.0 1.2 3.4")` 被呼叫
- **THEN** 回傳 `False`

#### Scenario: 中文文字有效
- **WHEN** `_is_valid_narrative("本季營收創新高，毛利率提升至 53%。")` 被呼叫
- **THEN** 回傳 `True`

### Requirement: Metadata SHALL 注入到每頁回傳的 dict

`parse_pdf` MUST 為每頁回傳的 dict 設 `metadata` 子 dict，內含 `company` / `stock_code` / `date` / `quarter` / `lang` / `source_page` / `source_file`（file 用 `Path(pdf_path).name`）。

#### Scenario: metadata 完整注入
- **WHEN** `parse_pdf("/tmp/TSMC 4Q24.pdf", company="台積電", quarter="2024Q4", date="2025-01-15", stock_code="2330")` 完成
- **THEN** 每頁 dict 的 `metadata.company == "台積電"`、`metadata.quarter == "2024Q4"`、`metadata.source_file == "TSMC 4Q24.pdf"`、`metadata.source_page` 等於 pdfplumber page_number

### Requirement: LlamaParse 補救 SHALL 依 `page_label` 對應頁碼

Phase 2 中，`parse_pdf` MUST 用 `doc.metadata.get("page_label")` 解析 LlamaParse 回傳的每個 document 的頁碼，並僅用 `failed_pages` 集合中的頁碼進行覆寫。覆寫條件 MUST 為 LlamaParse 回的 `text` 通過 `_is_valid_narrative` 檢查。成功覆寫後 MUST 設：
- `content` = LlamaParse 文字
- `parse_method` = `"llama_parse"`
- `parse_success` = `True`

#### Scenario: page_label 非數字時被忽略
- **GIVEN** LlamaParse 回傳 doc 含 `page_label="cover"`
- **WHEN** `parse_pdf(...)` 補救階段
- **THEN** 該 doc 不被用於覆寫，且不 raise

#### Scenario: 補救後 parse_method 變更
- **GIVEN** Phase 1 第 3 頁失敗、LlamaParse 補救成功
- **WHEN** `parse_pdf(...)` 完成
- **THEN** 第 3 頁的 `parse_method == "llama_parse"`、`parse_success == True`

### Requirement: 空 content 的頁面 MUST 從最終結果濾除

`parse_pdf` 在回傳前 MUST 過濾 `content.strip() == ""` 的頁面（不論 parse_success 為何）。

#### Scenario: 空頁不出現在最終結果
- **GIVEN** 第 1 頁 content 為空白、第 2 頁有內容
- **WHEN** `parse_pdf(...)` 被呼叫
- **THEN** 回傳列表長度為 1，僅含第 2 頁
