"""
src/ingestion/smart_parser.py
混合 PDF 解析策略：
  - 純文字段落 → pdfplumber（免費，速度快）
  - 財務表格解析失敗 → LlamaParse 補救（僅失敗頁面，省 quota）

輸出格式：
  [{
      "page": int,
      "content": str,           # 可被向量化的自然語言文字
      "has_table": bool,
      "parse_method": "pdfplumber" | "llama_parse",
  }, ...]
"""

import os
import re
from pathlib import Path
from typing import Any
import pdfplumber
import pandas as pd


# ── 是否啟用 LlamaParse（需要 LLAMA_CLOUD_API_KEY）──────────────────────────
def _get_llama_parser():
    key = os.getenv("LLAMA_CLOUD_API_KEY", "")
    if not key:
        return None
    try:
        from llama_parse import LlamaParse
        return LlamaParse(api_key=key, result_type="markdown", language="zh")
    except ImportError:
        print("[Parser] llama-parse 未安裝，表格補救停用")
        return None


def _table_to_narrative(df: pd.DataFrame, page_num: int) -> str:
    """把 DataFrame 轉成自然語言描述，讓向量模型能理解財務數字語意。"""
    lines = [f"（第{page_num}頁財務表格）"]
    for _, row in df.iterrows():
        pairs = [f"{col}：{val}" for col, val in row.items() if pd.notna(val) and str(val).strip()]
        if pairs:
            lines.append("、".join(pairs))
    return "\n".join(lines)


def _is_valid_narrative(text: str) -> bool:
    """判斷 pdfplumber 提取的文字是否有效（避免亂碼或空白）。"""
    if not text or len(text.strip()) < 20:
        return False
    # 中文字符比例低於 5% 且非純英數 → 可能是亂碼表格
    chinese_chars = len(re.findall(r'[一-鿿]', text))
    total_chars = len(text.strip())
    digit_ratio = len(re.findall(r'[\d\s%,\.\-\+]', text)) / max(total_chars, 1)
    if chinese_chars == 0 and digit_ratio > 0.8:
        # 純數字表格，pdfplumber 通常抽不出結構
        return False
    return True


def parse_page_with_pdfplumber(page) -> dict:
    """
    用 pdfplumber 解析單頁：
    1. 先嘗試抓表格 → 轉自然語言
    2. 再抓純文字
    """
    page_num = page.page_number
    result = {
        "page": page_num,
        "content": "",
        "has_table": False,
        "parse_method": "pdfplumber",
        "parse_success": True,
    }

    # ── 嘗試解析表格 ───────────────────────────────────────────────────────
    tables = page.extract_tables()
    table_texts = []
    if tables:
        result["has_table"] = True
        for tbl in tables:
            try:
                df = pd.DataFrame(tbl[1:], columns=tbl[0])
                narrative = _table_to_narrative(df, page_num)
                if _is_valid_narrative(narrative):
                    table_texts.append(narrative)
                else:
                    result["parse_success"] = False
            except Exception:
                result["parse_success"] = False

    # ── 抓純文字 ─────────────────────────────────────────────────────────
    raw_text = page.extract_text() or ""
    combined = "\n".join(filter(None, [raw_text, *table_texts])).strip()
    result["content"] = combined

    if not _is_valid_narrative(combined):
        result["parse_success"] = False

    return result


def parse_pdf(
    pdf_path: str | Path,
    company: str,
    quarter: str,
    date: str,
    stock_code: str,
    lang: str = "",
    fallback_to_llama: bool = True,
) -> list[dict]:
    """
    主入口：解析整份法說會 PDF，回傳結構化頁面列表。

    Args:
        pdf_path: PDF 檔案路徑
        company: 公司名稱（台積電）
        quarter: 季度（2024Q3）
        date: 法說日期（2024-10-17）
        stock_code: 股票代號（2330）
        lang: 語言代碼（M=中文，E=英文）
        fallback_to_llama: 表格失敗時是否用 LlamaParse 補救

    Returns:
        [{page, content, has_table, parse_method, metadata}, ...]
    """
    pdf_path = Path(pdf_path)
    print(f"[Parser] 開始解析: {pdf_path.name} ({company} {quarter})")

    llama_parser = _get_llama_parser() if fallback_to_llama else None
    pages_result = []
    failed_pages = []

    # ── Phase 1: pdfplumber 解析所有頁面 ───────────────────────────────
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"[Parser] 共 {total_pages} 頁")

        for page in pdf.pages:
            parsed = parse_page_with_pdfplumber(page)
            parsed["metadata"] = {
                "company": company,
                "stock_code": stock_code,
                "date": date,
                "quarter": quarter,
                "lang": lang,
                "source_page": page.page_number,
                "source_file": pdf_path.name,
            }
            pages_result.append(parsed)
            if not parsed["parse_success"]:
                failed_pages.append(page.page_number)

    print(f"[Parser] pdfplumber 完成，失敗頁數: {len(failed_pages)}")

    # ── Phase 2: LlamaParse 補救失敗頁面 ───────────────────────────────
    if failed_pages and llama_parser:
        print(f"[Parser] LlamaParse 補救第 {failed_pages} 頁...")
        try:
            llama_docs = llama_parser.load_data(str(pdf_path))
            # LlamaParse 以整份文件回傳，按頁碼對應
            llama_content_map = {}
            for doc in llama_docs:
                page_label = doc.metadata.get("page_label", "")
                try:
                    p_num = int(page_label)
                    llama_content_map[p_num] = doc.text
                except (ValueError, TypeError):
                    pass

            for i, page_data in enumerate(pages_result):
                if page_data["page"] in failed_pages:
                    llama_text = llama_content_map.get(page_data["page"], "")
                    if llama_text and _is_valid_narrative(llama_text):
                        pages_result[i]["content"] = llama_text
                        pages_result[i]["parse_method"] = "llama_parse"
                        pages_result[i]["parse_success"] = True
                        print(f"  ✅ 第 {page_data['page']} 頁 LlamaParse 補救成功")
        except Exception as e:
            print(f"[Parser] LlamaParse 失敗: {e}")

    # ── 過濾空白頁 ────────────────────────────────────────────────────
    valid_pages = [p for p in pages_result if p["content"].strip()]
    print(f"[Parser] 最終有效頁數: {len(valid_pages)} / {total_pages}")
    return valid_pages


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    import sys

    if len(sys.argv) < 2:
        print("用法: python smart_parser.py <pdf_path>")
        sys.exit(1)

    pages = parse_pdf(
        pdf_path=sys.argv[1],
        company="台積電",
        quarter="2024Q3",
        date="2024-10-17",
        stock_code="2330",
    )
    for p in pages[:3]:
        print(f"\n=== 第 {p['page']} 頁 ({p['parse_method']}) ===")
        print(p["content"][:300])
