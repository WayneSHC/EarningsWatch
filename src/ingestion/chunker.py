"""
src/ingestion/chunker.py
智能切割策略：
  - QA 段落 → 依問答對切割（問題+回答為一個 chunk）
  - Opening / Guidance → 滑動視窗切割（chunk_size=500, overlap=100 字元）
  - 表格段落 → 整頁為一個 chunk（不切割，保留數字完整性）

每個 chunk 保留完整 metadata，支援跨季比對過濾。
"""

import re
from typing import Any

CHUNK_SIZE = 500       # 一般文字 chunk 最大字元數
OVERLAP = 100          # 滑動視窗重疊字元數
MIN_CHUNK_LEN = 50    # 小於此長度的 chunk 丟棄

# QA 對話的問題起始模式（繁體中文法說會常見格式）
QA_PATTERNS = [
    r'^[\w\s]{1,30}（分析師[^）]*）[:：]',
    r'^Q\d*[\s：:]',
    r'^提問[人者][:：]',
    r'^[問答][:：]',
]
QA_REGEX = re.compile("|".join(QA_PATTERNS), re.MULTILINE)


def _classify_section(content: str) -> str:
    """根據內容特徵判斷段落類型。"""
    if re.search(r'(分析師|提問|Q\d*[\s：]|問[:：])', content):
        return "QA"
    if re.search(r'(本季|展望|預估|guidance|毛利率|營收|指引)', content, re.IGNORECASE):
        return "guidance"
    return "opening"


def _sliding_window(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """滑動視窗切割，保留段落重疊。"""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()
        if len(chunk) >= MIN_CHUNK_LEN:
            chunks.append(chunk)
        if end == len(text):
            break
        start = end - overlap
    return chunks


def _split_qa(text: str) -> list[str]:
    """
    依 QA 模式切割問答段落。
    每個 chunk = 一個問題 + 對應的回答。
    """
    splits = QA_REGEX.split(text)
    delimiters = QA_REGEX.findall(text)

    chunks = []
    for i, part in enumerate(splits):
        part = part.strip()
        if not part or len(part) < MIN_CHUNK_LEN:
            continue
        if i < len(delimiters):
            # 問題 + 回答合併
            qa_pair = delimiters[i] + part
        else:
            qa_pair = part
        # QA chunk 若太長仍需切割
        if len(qa_pair) > CHUNK_SIZE * 2:
            chunks.extend(_sliding_window(qa_pair))
        else:
            chunks.append(qa_pair)
    return chunks if chunks else _sliding_window(text)


def chunk_page(page_data: dict) -> list[dict]:
    """
    切割單頁資料，回傳 chunk 列表。

    Args:
        page_data: smart_parser 回傳的單頁字典

    Returns:
        [{content, chunk_index, section, metadata, ...}, ...]
    """
    content = page_data.get("content", "").strip()
    if not content:
        return []

    metadata = page_data.get("metadata", {})
    has_table = page_data.get("has_table", False)
    parse_method = page_data.get("parse_method", "pdfplumber")

    # ── 表格頁：整頁為一個 chunk ──────────────────────────────────────
    if has_table and parse_method in ("pdfplumber", "llama_parse"):
        raw_chunks = [content]
        section = "table"
    else:
        section = _classify_section(content)
        if section == "QA":
            raw_chunks = _split_qa(content)
        else:
            raw_chunks = _sliding_window(content)

    result = []
    for i, chunk_text in enumerate(raw_chunks):
        chunk_text = chunk_text.strip()
        if len(chunk_text) < MIN_CHUNK_LEN:
            continue

        # 判斷 chunk 特徵
        contains_guidance = bool(re.search(
            # 中文前瞻詞
            r'(預估|展望|指引|預期|下季|全年|毛利率目標|目標毛利率|營收目標)'
            # 英文法說常見前瞻術語（涵蓋英文 transcript）
            r'|(guidance|forecast|outlook|we expect|we anticipate|we project'
            r'|next quarter|full.?year|going forward|we target'
            r'|gross margin.*\d|revenue.*\d.*quarter)',
            chunk_text, re.IGNORECASE
        ))
        contains_number = bool(re.search(r'\d+\.?\d*\s*(%|億|萬|元|美元|季)', chunk_text))

        # 萃取主題 tag（粗略）
        topics = []
        topic_map = {
            "AI": r'(AI|人工智慧|CoWoS|HBM)',
            "毛利率": r'毛利率',
            "營收": r'(營收|Revenue)',
            "庫存": r'(庫存|inventory)',
            "產能": r'(產能|擴產|CoWoS)',
        }
        for tag, pattern in topic_map.items():
            if re.search(pattern, chunk_text, re.IGNORECASE):
                topics.append(tag)

        result.append({
            "content": chunk_text,
            "chunk_index": i,
            "section": section,
            "contains_guidance": contains_guidance,
            "contains_number": contains_number,
            "topics": topics,
            **metadata,   # company, quarter, date, stock_code, source_page, source_file
        })

    return result


def chunk_document(pages: list[dict]) -> list[dict]:
    """
    切割整份法說會，回傳所有 chunk 的扁平列表。
    """
    all_chunks = []
    for page_data in pages:
        chunks = chunk_page(page_data)
        all_chunks.extend(chunks)
    return all_chunks
