# export-formats Specification

## Purpose

定義 `src/ui/export.py` 的 CSV / PDF 匯出行為：CSV `utf-8-sig` 編碼（Excel 中文相容）、CJK 字型 cascade、缺字型時 raise 而非靜默產生亂碼、emoji ASCII 替換、跨季比對與承諾追蹤的欄位定義。本 spec 確保匯出檔在 Windows Excel / macOS Preview / Linux Reader 都能正常開啟，且雲端部署的 Linux 環境也有字型支援。

## Requirements

### Requirement: CSV 匯出 MUST 使用 `utf-8-sig` 編碼

`to_csv_single(result, company, topic)` 與 `to_csv_compare(table, companies, topic)` MUST 回傳 `bytes`，且 MUST 以 `"utf-8-sig"` 編碼（前綴 UTF-8 BOM `\xef\xbb\xbf`）。原因：Windows 版 Excel 開啟無 BOM 的 UTF-8 CSV 會用 Big5 解讀，導致中文亂碼。

#### Scenario: BOM 前綴
- **WHEN** `to_csv_single({}, "台積電", "AI")` 被呼叫
- **THEN** 回傳的 bytes 以 `b"\xef\xbb\xbf"` 開頭

#### Scenario: 中文可解碼為 UTF-8
- **WHEN** 匯出含中文公司名
- **THEN** 跳過 BOM 後其餘 bytes 可用 `decode("utf-8")` 正常解碼

### Requirement: `to_csv_single` SHALL 涵蓋矛盾偵測與承諾追蹤兩區塊

`to_csv_single(result, company, topic)` 產生的 CSV MUST 含：
- 區塊 1（矛盾偵測）：標題列、metadata 列（公司 / 主題 / 匯出日期）、欄位列（季度A / 季度B / 立場變化 / 有明確矛盾 / 詳細說明 / 早期證據 / 後期證據 / 建議追問）、每筆 contradiction 一列
- 區塊 2（承諾追蹤）：標題列、欄位列（承諾季度 / 承諾內容 / 後續季度 / 兌現狀況 / 說明）、每筆 promise 一列

`has_contradiction == True` MUST 寫入字串 `"是"`、`False` 寫入 `"否"`。

#### Scenario: 含矛盾與承諾的單公司匯出
- **GIVEN** result 含 1 個 contradiction（has_contradiction=True）、1 個 promise
- **WHEN** `to_csv_single(...)` 被呼叫
- **AND** 結果 decode 後依 csv reader 解析
- **THEN** 存在 `# 矛盾偵測結果` 標題列
- **AND** 存在 `# 承諾追蹤` 標題列
- **AND** 矛盾資料列含 `"是"`

### Requirement: PDF 匯出 MUST 解析 CJK 字型 cascade，缺字型時 raise

`_new_pdf()` MUST 依下列順序嘗試字型路徑（第一個存在的勝出）：
1. macOS：`/System/Library/Fonts/STHeiti Light.ttc` 或 `STHeiti Medium.ttc`
2. Debian/Ubuntu：`/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`（含 NotoSerifCJK / truetype 變體）
3. 萬用後備：`/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc`、`/usr/share/fonts/truetype/arphic/uming.ttc`

任何字型都找不到時 MUST raise `RuntimeError`，訊息含 `找不到中文字型` 與部署提示（`fonts-noto-cjk`）。MUST 不退回 Helvetica（會在中文寫入時拋 `FPDFUnicodeEncodingException`）。

#### Scenario: macOS 環境找到 STHeiti
- **GIVEN** `/System/Library/Fonts/STHeiti Light.ttc` 存在
- **WHEN** `_resolve_cjk_font()` 被呼叫
- **THEN** 回傳 `("STHeiti", "/System/Library/Fonts/STHeiti Light.ttc")`

#### Scenario: 所有候選都不存在 raise
- **GIVEN** 所有 `_FONT_CANDIDATES` 路徑都不存在
- **WHEN** `_new_pdf()` 被呼叫
- **THEN** raise `RuntimeError` 含 `找不到中文字型` 與 `fonts-noto-cjk` 字樣

### Requirement: Emoji SHALL 在 PDF 寫入前被替換為 ASCII

`_strip_emoji(text)` MUST 把 `_EMOJI_MAP` 中的 emoji（`✅` → `[OK]`、`❌` → `[X]`、`⚠` → `[!]`、`🚨` → `[!!]`、`✓` → `[OK]`、`📄` / `📋` / `📈` / `📅` → `""`）替換為對應 ASCII 字串。所有 PDF 寫入函數（`_pdf_title` / `_pdf_h2` / `_pdf_body`）MUST 在寫入前呼叫 `_strip_emoji`，避免 STHeiti / Noto 不支援的字元造成輸出方框。

#### Scenario: emoji 被替換
- **WHEN** `_strip_emoji("✅ 已完成 ❌ 失敗")` 被呼叫
- **THEN** 回傳 `"[OK] 已完成 [X] 失敗"`

#### Scenario: 無 emoji 字串原樣
- **WHEN** `_strip_emoji("一般文字")` 被呼叫
- **THEN** 回傳 `"一般文字"`

### Requirement: `to_pdf_single` SHALL 產生含關鍵段落的 PDF

`to_pdf_single(result, company, topic)` MUST 回傳合法 PDF bytes（以 `%PDF-` magic 開頭）。內容 MUST 含下列段落：
1. 標題（`EarningsWatch 分析報告`）
2. 公司 / 主題 / 日期一行
3. 信心度摘要（含 `跨季比對` / `立場轉變` / `明確矛盾` 計數）
4. 完整分析報告（從 `result.final_report`）
5. 矛盾 / 立場轉變詳情（僅 stance_change 非 `"無關"`/`None` 的條目）
6. 承諾追蹤（若有 promises）
7. 頁腳免責聲明（含 `不提供投資建議` 字樣）

#### Scenario: 輸出為合法 PDF
- **WHEN** `to_pdf_single({"final_report": "", "contradictions": [], "promises": []}, "台積電", "AI")` 被呼叫
- **THEN** 回傳 bytes 以 `b"%PDF-"` 開頭

### Requirement: `to_csv_compare` SHALL 對齊公司欄位、缺值填破折號

`to_csv_compare(comparison_table, companies, topic)` MUST：
- 第一列為 metadata（含主題 / 匯出日期）
- 表頭列為 `["季度對", *companies]`
- 每行依 `comparison_table` 中對應 `quarter_pair` 寫入，缺值 MUST 寫 `"—"`

#### Scenario: 三公司比較表
- **GIVEN** `comparison_table = [{"quarter_pair": "2024Q1 vs 2024Q2", "A": "更樂觀", "B": "—", "C": "更保守"}]`
- **WHEN** `to_csv_compare(table, ["A", "B", "C"], "AI")` 被呼叫
- **AND** 結果 decode 後解析
- **THEN** 存在資料列 `["2024Q1 vs 2024Q2", "更樂觀", "—", "更保守"]`
