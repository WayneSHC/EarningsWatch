# pdf-ingestion-pipeline Specification

## Purpose

定義 `scripts/run_ingestion.py` 的 PDF 批次匯入流程：五種檔名格式的 metadata 解析（A: MOPS / B: TSMC Transcript / C: 2454 MediaTek / D: Hon Hai / E: Delta Analyst Meeting）、台股法說日期 → 財報季度換算、`--pdf` 參數的路徑越界防護、原子寫入 ingestion log、單檔失敗不中斷批次、`--lang` 與 `--force` 過濾語意。本 spec 確保各種命名慣例都能被正確識別、批次中斷不會遺失進度、惡意路徑無法逃逸 `data/raw_pdfs/`。

## Requirements

### Requirement: `_date_to_quarter` SHALL 依台股法說慣例換算

`_date_to_quarter(year, month)` MUST 依下列規則回傳對應的「報告季度」字串：
- `month ∈ [1, 3]` → `f"{year-1}Q4"`（前一年 Q4 報告）
- `month ∈ [4, 6]` → `f"{year}Q1"`
- `month ∈ [7, 9]` → `f"{year}Q2"`
- `month ∈ [10, 12]` → `f"{year}Q3"`

#### Scenario: 1 月召開報前一年 Q4
- **WHEN** `_date_to_quarter(2026, 1)` 被呼叫
- **THEN** 回傳 `"2025Q4"`

#### Scenario: 4 月召開報當年 Q1
- **WHEN** `_date_to_quarter(2024, 4)` 被呼叫
- **THEN** 回傳 `"2024Q1"`

#### Scenario: 10 月召開報當年 Q3
- **WHEN** `_date_to_quarter(2024, 10)` 被呼叫
- **THEN** 回傳 `"2024Q3"`

### Requirement: `parse_filename` SHALL 支援格式 A（MOPS）

格式 A 為 `{4碼股號}{YYYYMMDD}{M|E}{3碼序號}.pdf`。`parse_filename(name)` MUST 對符合此 pattern 的檔名回傳 dict 含 `company` / `stock_code` / `quarter` / `date`（`YYYY-MM-DD`）/ `lang` / `source_file`。未知股票代號 MUST 回傳 `None`。

#### Scenario: 已知股票代號解析成功
- **WHEN** `parse_filename("233020260115M001.pdf")` 被呼叫
- **THEN** 回傳 dict `{company: "台積電", stock_code: "2330", quarter: "2025Q4", date: "2026-01-15", lang: "M", source_file: "233020260115M001.pdf"}`

#### Scenario: 未知股票代號回 None
- **WHEN** `parse_filename("999920240115M001.pdf")` 被呼叫
- **THEN** 回傳 `None`

#### Scenario: 大小寫不敏感
- **WHEN** `parse_filename("233020240115m001.PDF")` 被呼叫
- **THEN** 回傳 dict，`lang == "M"`（uppercase）

### Requirement: `parse_filename` SHALL 支援格式 B（TSMC Transcript）

格式 B 為 `TSMC {季號:[1-4]}Q{2位年} Transcript{-N}?.pdf`。MUST 回傳 dict 含 `company == "台積電"`、`stock_code == "2330"`、`lang == "E"`、`quarter == f"{2000+yr2}Q{qnum}"`、`date` 為近似召開月 1 日（Q1→4月、Q2→7月、Q3→10月、Q4→隔年 1 月）、`part` 從 suffix 移除前導 `-`（無 suffix 時為 `"1"`）。

#### Scenario: 標準 TSMC Transcript
- **WHEN** `parse_filename("TSMC 4Q25 Transcript.pdf")` 被呼叫
- **THEN** 回傳 dict `{company: "台積電", quarter: "2025Q4", date: "2026-01-01", lang: "E", part: "1"}`

#### Scenario: 多版本 suffix
- **WHEN** `parse_filename("TSMC 4Q25 Transcript-2.pdf")` 被呼叫
- **THEN** 回傳 dict `part == "2"`

#### Scenario: Q1 召開月為 4 月
- **WHEN** `parse_filename("TSMC 1Q24 Transcript.pdf")` 被呼叫
- **THEN** 回傳 `quarter == "2024Q1"`、`date == "2024-04-01"`

### Requirement: `parse_filename` SHALL 支援格式 C（聯發科 / 通用股號）

格式 C 為 `{4碼股號}_{季號}Q{2位年}_{文件類型}.pdf`。MUST 用 `STOCK_CODE_TO_COMPANY` 對映公司名（未知時回 `None`），`lang` 強制 `"E"`，並把 `doctype` 中的 `_` 轉為空白存入 `doc_subtype`。

#### Scenario: 聯發科 Earnings Call
- **WHEN** `parse_filename("2454_1Q24_Earnings_Call_Transcript.pdf")` 被呼叫
- **THEN** 回傳 dict `{company: "聯發科", stock_code: "2454", quarter: "2024Q1", lang: "E", doc_subtype: "Earnings Call Transcript"}`

#### Scenario: 未知股號回 None
- **WHEN** `parse_filename("9999_1Q24_Earnings.pdf")` 被呼叫
- **THEN** 回傳 `None`

### Requirement: `parse_filename` SHALL 支援格式 D（鴻海 Hon Hai）

格式 D 為 `Hon[_ ]Hai[_ ]{季號}Q{2位年}[_ ]{rest}.pdf`（底線或空格分隔皆可）。MUST：
- 回傳 `company == "鴻海"`、`stock_code == "2317"`
- 日期：若 `rest` 內含 `YYYYMMDD`（用 `_` 或空白包夾）→ 用該精確日期；否則用 Q1→5月、Q2→8月、Q3→11月、Q4→隔年 3 月的 1 日近似
- `lang == "M"` 當 `rest` 含 `Chinese`（case-insensitive）；否則 `"E"`

#### Scenario: 嵌入精確日期
- **WHEN** `parse_filename("Hon Hai 1Q25 Results_Chinese_20250514_5223.pdf")` 被呼叫
- **THEN** 回傳 `quarter == "2025Q1"`、`date == "2025-05-14"`、`lang == "M"`

#### Scenario: 無精確日期用近似
- **WHEN** `parse_filename("Hon_Hai_1Q22_Results_Chinese.pdf")` 被呼叫
- **THEN** 回傳 `quarter == "2022Q1"`、`date == "2022-05-01"`、`lang == "M"`

#### Scenario: 無 Chinese 字樣時為英文
- **WHEN** `parse_filename("Hon_Hai_4Q23_Results_Transcript.pdf")` 被呼叫
- **THEN** 回傳 `lang == "E"`

### Requirement: `parse_filename` SHALL 支援格式 E（台達電 Analyst Meeting）

格式 E 為 `{季號}Q{2位年}[_ ]Analyst[_ ]Meeting.pdf`。MUST：
- 從父目錄名（如 `2308_Delta`）取股號（`_` 切分前段，否則取前 4 字）
- 股號不在 `STOCK_CODE_TO_COMPANY` 中 MUST 回 `None`
- 已知股號時回傳 `company` 對應名稱、`lang == "M"`
- Q1→5月、Q2→8月、Q3→11月、Q4→隔年 2 月近似日期

#### Scenario: 父目錄 2308_Delta 對映台達電
- **WHEN** `parse_filename("1Q22_Analyst Meeting.pdf", parent_dir="2308_Delta")` 被呼叫
- **THEN** 回傳 `{company: "台達電", stock_code: "2308", quarter: "2022Q1", date: "2022-05-01", lang: "M"}`

#### Scenario: 父目錄無法對映回 None
- **WHEN** `parse_filename("1Q22_Analyst Meeting.pdf", parent_dir="9999_Unknown")` 被呼叫
- **THEN** 回傳 `None`

### Requirement: 無法辨識的檔名 SHALL 回 None

`parse_filename` MUST 對所有五種格式皆不符的檔名回傳 `None`，不 raise。

#### Scenario: 完全不符合任何格式
- **WHEN** `parse_filename("random_name.pdf")` 被呼叫
- **THEN** 回傳 `None`

### Requirement: Ingestion log SHALL 以 atomic write 寫入

`_save_log(log)` MUST：
1. 確保 `PROCESSED_DIR` 存在（`mkdir(parents=True, exist_ok=True)`）
2. 寫入 `.json.tmp` 臨時檔，再 `os.replace(tmp, INGESTION_LOG)` 改名
3. 不執行 `--no-verify` 或非原子的 write（防止中途崩潰 / 斷電遺失整批進度）

`_load_log()` MUST 對檔案不存在或解析失敗回傳 `{}`，print 警告但不 raise。

#### Scenario: 原子寫入
- **WHEN** `_save_log({"a.pdf": {...}})` 被呼叫
- **THEN** 最終 `INGESTION_LOG` 內容為合法 JSON、含該筆 entry
- **AND** 不應留下 `.json.tmp` 殘檔

#### Scenario: 損毀檔案不中止程式
- **GIVEN** `INGESTION_LOG` 為非 JSON 內容
- **WHEN** `_load_log()` 被呼叫
- **THEN** 回傳 `{}`，不 raise

### Requirement: `--pdf` 參數 SHALL 防止路徑越界

`main()` 處理 `--pdf` 參數時 MUST：
1. 用 `(RAW_PDF_DIR / args.pdf).resolve()` 計算絕對路徑
2. 驗證 `.is_relative_to(RAW_PDF_DIR.resolve())`；否則 print 越界錯誤、`sys.exit(1)`
3. 檔案不存在時 print 錯誤、`sys.exit(1)`

此 spec 保護 demo 環境免於透過 `../..` path traversal 讀到任意檔案。

#### Scenario: 越界路徑 raise exit
- **GIVEN** `args.pdf == "../../etc/passwd"`
- **WHEN** `main()` 處理該參數
- **THEN** stdout 含 `路徑越界` 字樣
- **AND** 函式以 exit code 1 結束

#### Scenario: 合法子路徑接受
- **GIVEN** `args.pdf == "2454_MediaTek/2454_1Q24_Earnings_Call_Transcript.pdf"`、檔案存在
- **WHEN** `main()` 處理該參數
- **THEN** 該檔案被加入 candidates，不 sys.exit

### Requirement: 單檔失敗 SHALL 不中斷批次

`main()` 的批次匯入 loop MUST 對每個檔案獨立 try/except；任一檔案 raise MUST 被捕獲、寫入 log（含 `status: "failed"` 與 `error` 訊息）、`fail_count += 1`，並繼續下一個檔案。批次結束時 MUST `sys.exit(1)` 若 `fail_count > 0`、否則正常結束。

#### Scenario: 一檔失敗其餘繼續
- **GIVEN** 3 個檔案中第 2 個 raise
- **WHEN** 批次執行完成
- **THEN** `success_count == 2`、`fail_count == 1`
- **AND** 第 3 個檔案仍被處理（不被第 2 個失敗中斷）

### Requirement: `--dry-run` SHALL 不呼叫 BigQuery / embedder

`main()` 在 `args.dry_run == True` 時 MUST：
- 跳過 BigQuery 連線檢查
- 完成檔案解析與過濾後印出清單
- MUST 不呼叫 `_ingest_one`
- 正常 return（不 sys.exit）

#### Scenario: dry-run 跳過實際匯入
- **WHEN** `python run_ingestion.py --dry-run` 被執行
- **AND** 候選 PDF 有 5 個
- **THEN** 5 個 PDF 都列在預覽清單
- **AND** `upsert_chunks` / BQ insert 不被呼叫
