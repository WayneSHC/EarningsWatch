"""
scripts/run_ingestion.py
PDF 批次匯入 Pipeline — 唯一入口

功能：
  1. 掃描 data/raw_pdfs/ 下所有 PDF
  2. 支援兩種檔名格式自動解析 metadata：
       格式 A（MOPS 官方）：{股號4碼}{YYYYMMDD}{M/E}{序號3碼}.pdf
         例：233020260115M001.pdf
       格式 B（英文逐字稿）：TSMC {季號}Q{2位年} Transcript{後綴}.pdf
         例：TSMC 4Q25 Transcript.pdf、TSMC 4Q24 Transcript-2.pdf
  3. 讀取 data/processed/ingestion_log.json，跳過已成功處理的檔案
  4. 依序執行：smart_parser → chunker → embedder → Qdrant upsert
  5. 任一檔案失敗不中斷批次，繼續處理其餘檔案

使用方式：
    python scripts/run_ingestion.py                        # 匯入全部未處理的 PDF
    python scripts/run_ingestion.py --force                # 強制重新匯入所有檔案
    python scripts/run_ingestion.py --dry-run              # 預覽待匯入清單，不實際寫入
    python scripts/run_ingestion.py --lang M               # 只匯入中文版（M）
    python scripts/run_ingestion.py --lang E               # 只匯入英文版（E）
    python scripts/run_ingestion.py --pdf 233020260115M001.pdf  # 只匯入單一檔案
"""

import os
import sys
import re
import json
import argparse
import traceback
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# ── 確保 project root 在 sys.path，讓 src.* import 正常運作 ──────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env")

# ── 路徑常數 ──────────────────────────────────────────────────────────────────
RAW_PDF_DIR   = _PROJECT_ROOT / "data" / "raw_pdfs"
PROCESSED_DIR = _PROJECT_ROOT / "data" / "processed"
INGESTION_LOG = PROCESSED_DIR / "ingestion_log.json"

# ── 股票代號 → 公司名稱（格式 A 使用） ───────────────────────────────────────
STOCK_CODE_TO_COMPANY: dict[str, str] = {
    "2330": "台積電",
    "2454": "聯發科",
    "2317": "鴻海",
    "2308": "台達電",
}


def _date_to_quarter(year: int, month: int) -> str:
    """
    將法說召開年月換算成對應財報季度標籤。
    台股法說慣例：
      1～3月  → 前一年 Q4（報告去年 10～12 月業績）
      4～6月  → 當年 Q1
      7～9月  → 當年 Q2
      10～12月 → 當年 Q3
    例：(2026, 1) → '2025Q4'
    """
    if month <= 3:
        return f"{year - 1}Q4"
    elif month <= 6:
        return f"{year}Q1"
    elif month <= 9:
        return f"{year}Q2"
    else:
        return f"{year}Q3"


# ══════════════════════════════════════════════════════════════════════════════
# 檔名解析：支援格式 A（MOPS）與格式 B（TSMC Transcript）
# ══════════════════════════════════════════════════════════════════════════════

# 格式 A：4碼股號 + 8碼日期(YYYYMMDD) + 1碼語言(M/E) + 3碼序號
_PATTERN_A = re.compile(
    r'^(?P<stock>\d{4})'
    r'(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})'
    r'(?P<lang>[ME])'
    r'(?P<seq>\d{3})\.pdf$',
    re.IGNORECASE,
)

# 格式 B：TSMC {季號:1}Q{2位年} Transcript{後綴:-N}?.pdf
_PATTERN_B = re.compile(
    r'^TSMC\s+(?P<qnum>[1-4])Q(?P<yr2>\d{2})\s+Transcript(?P<suffix>-\d+)?\.pdf$',
    re.IGNORECASE,
)

# 格式 C：{股號4碼}_{季號}Q{2位年}_{文件類型}.pdf
#   例：2454_1Q24_Earnings_Call_Transcript.pdf
#       2454_2Q25_Investor_Conference.pdf
_PATTERN_C = re.compile(
    r'^(?P<stock>\d{4})_(?P<qnum>[1-4])Q(?P<yr2>\d{2})_(?P<doctype>.+)\.pdf$',
    re.IGNORECASE,
)

# 格式 D：Hon Hai / Hon_Hai 法說逐字稿（鴻海官方命名）
#   例：Hon_Hai_1Q22_Results_Chinese.pdf
#       Hon Hai 1Q25 Results_Chinese_20250514_5223.pdf
#       Hon_Hai_4Q22_Investor_Conference_Call_Transcript_Chinese.pdf
#       Hon_Hai_4Q23_Results_Transcript_Chinese(1).pdf
_PATTERN_D = re.compile(
    r'^Hon[_ ]Hai[_ ](?P<qnum>[1-4])Q(?P<yr2>\d{2})[_ ](?P<rest>.+)\.pdf$',
    re.IGNORECASE,
)


def parse_filename(filename: str) -> dict | None:
    """
    從 PDF 檔名解析 metadata。
    成功回傳：{company, stock_code, quarter, date, lang, source_file, [part]}
    無法辨識回傳 None → 呼叫端記錄後略過。
    """
    # ── 格式 A ─────────────────────────────────────────────────────────────
    m = _PATTERN_A.match(filename)
    if m:
        stock = m.group("stock")
        year  = int(m.group("year"))
        month = int(m.group("month"))
        day   = int(m.group("day"))
        lang  = m.group("lang").upper()

        company = STOCK_CODE_TO_COMPANY.get(stock)
        if not company:
            # 未知股票代號 → 無法產生有意義的 metadata
            return None

        return {
            "company":    company,
            "stock_code": stock,
            "quarter":    _date_to_quarter(year, month),
            "date":       f"{year}-{month:02d}-{day:02d}",
            "lang":       lang,
            "source_file": filename,
        }

    # ── 格式 B ─────────────────────────────────────────────────────────────
    m = _PATTERN_B.match(filename)
    if m:
        qnum   = int(m.group("qnum"))     # 財報季度數（1～4）
        yr2    = int(m.group("yr2"))      # 2位年份（25 → 2025）
        suffix = m.group("suffix") or ""  # "-2" 或空字串

        fiscal_year = 2000 + yr2
        quarter = f"{fiscal_year}Q{qnum}"

        # 依財報季推算法說召開月份（近似）
        # Q1→4月, Q2→7月, Q3→10月, Q4→隔年1月
        call_month_map = {
            1: (fiscal_year,     4),
            2: (fiscal_year,     7),
            3: (fiscal_year,    10),
            4: (fiscal_year + 1, 1),
        }
        call_year, call_month = call_month_map[qnum]

        return {
            "company":    "台積電",
            "stock_code": "2330",
            "quarter":    quarter,
            "date":       f"{call_year}-{call_month:02d}-01",  # 近似日期
            "lang":       "E",   # TSMC Transcript 系列均為英文
            "source_file": filename,
            "part":        suffix.lstrip("-") if suffix else "1",
        }

    # ── 格式 C ─────────────────────────────────────────────────────────────
    m = _PATTERN_C.match(filename)
    if m:
        stock   = m.group("stock")
        qnum    = int(m.group("qnum"))
        yr2     = int(m.group("yr2"))
        doctype = m.group("doctype")  # Earnings_Call_Transcript / Investor_Conference

        company = STOCK_CODE_TO_COMPANY.get(stock)
        if not company:
            return None

        fiscal_year = 2000 + yr2
        quarter = f"{fiscal_year}Q{qnum}"

        # 依財報季推算法說召開月份（近似）
        call_month_map = {
            1: (fiscal_year,     4),
            2: (fiscal_year,     7),
            3: (fiscal_year,    10),
            4: (fiscal_year + 1, 1),
        }
        call_year, call_month = call_month_map[qnum]

        return {
            "company":    company,
            "stock_code": stock,
            "quarter":    quarter,
            "date":       f"{call_year}-{call_month:02d}-01",
            "lang":       "E",      # 聯發科英文法說系列
            "source_file": filename,
            "doc_subtype": doctype.replace("_", " "),
        }

    # ── 格式 D：Hon Hai 法說系列 ──────────────────────────────────────────
    m = _PATTERN_D.match(filename)
    if m:
        qnum = int(m.group("qnum"))
        yr2  = int(m.group("yr2"))
        rest = m.group("rest")

        fiscal_year = 2000 + yr2
        quarter = f"{fiscal_year}Q{qnum}"

        # 鴻海法說實際召開月份（對應財報季）
        # Q1→5月, Q2→8月, Q3→11月, Q4→隔年3月
        call_month_map = {
            1: (fiscal_year,     5),
            2: (fiscal_year,     8),
            3: (fiscal_year,    11),
            4: (fiscal_year + 1, 3),
        }
        call_year, call_month = call_month_map[qnum]

        # 若檔名內嵌 YYYYMMDD 日期（如 _20250514_）優先使用精確日期
        date_m = re.search(r'[_\s](\d{4})(\d{2})(\d{2})[_\s]', rest)
        exact_date = (
            f"{date_m.group(1)}-{date_m.group(2)}-{date_m.group(3)}"
            if date_m else f"{call_year}-{call_month:02d}-01"
        )

        # 語言判斷：含 "Chinese" → 中文版（M）
        lang = "M" if "chinese" in rest.lower() else "E"

        return {
            "company":    "鴻海",
            "stock_code": "2317",
            "quarter":    quarter,
            "date":       exact_date,
            "lang":       lang,
            "source_file": filename,
        }

    # ── 無法辨識格式 ────────────────────────────────────────────────────────
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 匯入日誌：防止重複處理
# ══════════════════════════════════════════════════════════════════════════════

def _load_log() -> dict:
    """
    讀取匯入日誌。
    日誌結構：{filename: {"status": "ok"|"failed", "chunks": int, "ts": ISO時間, ...}}
    讀取失敗時回傳空 dict，不中止程式。
    """
    if INGESTION_LOG.exists():
        try:
            return json.loads(INGESTION_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[警告] 無法讀取匯入日誌，將從頭建立：{e}")
    return {}


def _save_log(log: dict) -> None:
    """
    將更新後的日誌寫回磁碟。
    每處理完一個 PDF 就立即儲存，避免批次中途崩潰遺失進度。
    """
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    INGESTION_LOG.write_text(
        json.dumps(log, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 單一 PDF 匯入
# ══════════════════════════════════════════════════════════════════════════════

def _ingest_one(pdf_path: Path, meta: dict) -> int:
    """
    執行單一 PDF 的完整匯入流程：
      smart_parser → chunker → embedder（含 Qdrant upsert）

    Args:
        pdf_path: PDF 檔案絕對路徑
        meta:     由 parse_filename 產生的 metadata dict

    Returns:
        成功寫入 Qdrant 的 chunk 數量

    Raises:
        ValueError: PDF 無有效內容時
        其他 Exception: embedder 或 Qdrant 通訊錯誤（由呼叫端捕捉）
    """
    from src.ingestion.smart_parser import parse_pdf
    from src.ingestion.chunker import chunk_document
    from src.ingestion.embedder import upsert_chunks

    # Step 1：PDF 解析（pdfplumber 主解 + LlamaParse 補救表格失敗頁）
    pages = parse_pdf(
        pdf_path   = pdf_path,
        company    = meta["company"],
        quarter    = meta["quarter"],
        date       = meta["date"],
        stock_code = meta["stock_code"],
        lang       = meta.get("lang", ""),
    )
    if not pages:
        raise ValueError("PDF 解析結果為空（可能是掃描版、加密 PDF 或純圖片）")

    # Step 2：智能切割（QA 對話 / guidance / 表格各用不同策略）
    chunks = chunk_document(pages)
    if not chunks:
        raise ValueError("chunker 未產生任何 chunk，請確認 PDF 內容格式")

    print(f"  → 解析：{len(pages)} 頁 → {len(chunks)} 個 chunk")

    # Step 3：Embedding + Qdrant upsert（批次處理，含進度條）
    written = upsert_chunks(chunks, show_progress=True)
    return written


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="EarningsWatch PDF 批次匯入工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python scripts/run_ingestion.py                        # 匯入全部未處理
  python scripts/run_ingestion.py --force                # 強制重新匯入
  python scripts/run_ingestion.py --dry-run              # 預覽，不實際寫入
  python scripts/run_ingestion.py --lang M               # 只匯入中文版
  python scripts/run_ingestion.py --pdf TSMC\\ 4Q25\\ Transcript.pdf
        """,
    )
    arg_parser.add_argument(
        "--force",   action="store_true",
        help="忽略匯入日誌，強制重新匯入所有符合條件的 PDF",
    )
    arg_parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="只列出待處理清單，不實際寫入 Qdrant",
    )
    arg_parser.add_argument(
        "--lang", choices=["M", "E"],
        help="只匯入指定語言版本（M=中文，E=英文）；不指定則全部匯入",
    )
    arg_parser.add_argument(
        "--pdf", type=str, metavar="FILENAME",
        help="只匯入指定的單一 PDF 檔名（只需檔名，不含路徑）",
    )
    args = arg_parser.parse_args()

    print("=" * 60)
    print("  EarningsWatch PDF 批次匯入工具")
    print(f"  時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.force:   print("  模式：強制重新匯入（--force）")
    if args.dry_run: print("  模式：預覽模式（--dry-run）")
    if args.lang:    print(f"  過濾：只處理語言 = {args.lang}")
    print("=" * 60)

    # ── Qdrant 連線確認（dry-run 模式跳過，避免不必要的網路需求）────────────
    if not args.dry_run:
        from src.core.qdrant_client import health_check
        if not health_check():
            print("\n❌ 無法連線至 Qdrant，請先啟動 Docker：")
            print("   docker run -d --name qdrant -p 6333:6333 \\")
            print("     -v $(pwd)/qdrant_storage:/qdrant/storage qdrant/qdrant")
            sys.exit(1)
        print("✅ Qdrant 連線正常\n")

    # ── 確認 PDF 目錄存在 ─────────────────────────────────────────────────
    if not RAW_PDF_DIR.exists():
        print(f"❌ PDF 目錄不存在：{RAW_PDF_DIR}")
        print("   請將法說會 PDF 放入 data/raw_pdfs/ 目錄後再執行")
        sys.exit(1)

    # ── 收集候選 PDF 檔案 ─────────────────────────────────────────────────
    if args.pdf:
        # 支援子路徑（如 2454_MediaTek/2454_1Q24_Earnings_Call_Transcript.pdf）
        target = RAW_PDF_DIR / args.pdf
        if not target.exists():
            print(f"❌ 找不到指定的 PDF：{target}")
            sys.exit(1)
        candidates = [target]
    else:
        # rglob("*.pdf") 遞迴掃描所有子目錄
        candidates = sorted(RAW_PDF_DIR.rglob("*.pdf"))

    if not candidates:
        print("⚠  data/raw_pdfs/ 目錄中沒有 PDF 檔案")
        sys.exit(0)

    # ── 解析 metadata + 套用過濾條件 ─────────────────────────────────────
    log = _load_log()
    to_process: list[tuple[Path, dict]] = []
    stats = {"no_parse": [], "cached": [], "lang_skip": []}

    for pdf_path in candidates:
        fname = pdf_path.name
        meta  = parse_filename(fname)

        if meta is None:
            stats["no_parse"].append(fname)
            continue

        if args.lang and meta.get("lang") != args.lang:
            stats["lang_skip"].append(fname)
            continue

        # 已成功匯入且未指定 --force → 略過
        if not args.force and log.get(fname, {}).get("status") == "ok":
            stats["cached"].append(fname)
            continue

        to_process.append((pdf_path, meta))

    # ── 印出統計摘要 ──────────────────────────────────────────────────────
    print(f"📂 掃描到 {len(candidates)} 個 PDF")
    print(f"  🔜 待匯入：            {len(to_process)} 個")
    print(f"  ⏭  已匯入（略過）：    {len(stats['cached'])} 個")
    if stats["lang_skip"]:
        print(f"  🔤 語言過濾（略過）：  {len(stats['lang_skip'])} 個")
    if stats["no_parse"]:
        print(f"  ⚠  無法解析檔名：     {len(stats['no_parse'])} 個")
        for f in stats["no_parse"]:
            print(f"     - {f}")

    if not to_process:
        print("\n✅ 沒有需要匯入的 PDF（全部已處理或被過濾）")
        print("   如需重新匯入，請加上 --force 參數")
        return

    # ── 預覽模式：列出清單後結束 ──────────────────────────────────────────
    if args.dry_run:
        print(f"\n📋 [Dry Run] 以下 {len(to_process)} 個檔案將被匯入：")
        for pdf_path, meta in to_process:
            lang_label = {"M": "中文", "E": "英文"}.get(meta.get("lang", ""), "?")
            print(f"  {pdf_path.name}")
            print(f"    公司：{meta['company']}  季度：{meta['quarter']}"
                  f"  語言：{lang_label}  日期：{meta['date']}")
        print("\n（實際執行請移除 --dry-run 參數）")
        return

    # ── 批次匯入 ──────────────────────────────────────────────────────────
    print(f"\n🚀 開始匯入 {len(to_process)} 個 PDF...\n")
    success_count = 0
    fail_count    = 0

    for idx, (pdf_path, meta) in enumerate(to_process, start=1):
        fname      = pdf_path.name
        lang_label = {"M": "中文", "E": "英文"}.get(meta.get("lang", ""), "?")
        print(f"[{idx}/{len(to_process)}] {fname}")
        print(f"  公司：{meta['company']}  季度：{meta['quarter']}"
              f"  語言：{lang_label}")

        try:
            written = _ingest_one(pdf_path, meta)

            # 成功：寫入日誌並立即儲存（防止中途崩潰遺失進度）
            log[fname] = {
                "status":  "ok",
                "chunks":  written,
                "quarter": meta["quarter"],
                "company": meta["company"],
                "lang":    meta.get("lang", ""),
                "ts":      datetime.now().isoformat(),
            }
            _save_log(log)
            success_count += 1
            print(f"  ✅ 完成，共寫入 {written} 個 chunk\n")

        except Exception as e:
            # 單一檔案失敗不中斷整批匯入，記錄錯誤繼續下一個
            log[fname] = {
                "status": "failed",
                "error":  str(e),
                "ts":     datetime.now().isoformat(),
            }
            _save_log(log)
            fail_count += 1
            print(f"  ❌ 失敗：{e}")
            traceback.print_exc()   # 完整堆疊供除錯
            print()

    # ── 最終摘要 ──────────────────────────────────────────────────────────
    print("=" * 60)
    print(f"  匯入完成：成功 {success_count} 個 / 失敗 {fail_count} 個")
    if fail_count:
        print(f"  ⚠  失敗記錄已寫入 {INGESTION_LOG}")
        print("     確認原因後，加上 --force 重試")
    print(f"  📄 完整日誌：{INGESTION_LOG}")
    print("=" * 60)

    # 有失敗時以非零 exit code 通知 CI / 自動化流程
    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
