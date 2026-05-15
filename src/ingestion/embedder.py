"""
src/ingestion/embedder.py
批次 embedding + 寫入 BigQuery。
使用 google-genai SDK + gemini-embedding-2（MRL 截斷至 768 維，保留 BigQuery schema 相容）。

gemini-embedding-2 與 001 的差異：
  - 不再使用 task_type 參數，改以 prompt prefix 表達意圖：
      doc 側：`title: none | text: {content}`
      query 側：`task: search result | query: {content}`
  - 較小維度（< 3072）會自動 L2 normalize，呼叫端不需手動 normalize
"""

import hashlib
import os
from functools import lru_cache
from typing import Any

from google import genai
from google.genai import types as genai_types
from tqdm import tqdm

from src.core.bq_client import get_bq_client, get_table_path, ensure_dataset_and_table
from src.core.secrets import get_secret

EMBEDDING_MODEL = "gemini-embedding-2"
EMBED_DIM = 768  # MRL 截斷：預設 3072 → 截至 768 維與既有 BQ schema 相容（< 3072 已自動 normalize）
BATCH_SIZE = 1  # gemini-embedding-2 是多模態模型，contents 為 list 時被當成一個多 part 輸入；
                # 必須一次傳一個文字才能得到逐筆 embedding。
UPSERT_BATCH = 100

@lru_cache(maxsize=1)
def _get_client() -> genai.Client:
    api_key = get_secret("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 GEMINI_API_KEY / GOOGLE_API_KEY")
    print(f"[Embedder] 初始化 google-genai client，模型 {EMBEDDING_MODEL}...")
    return genai.Client(api_key=api_key)

def _embed(texts: list[str]) -> list[list[float]]:
    """texts 必須已套上 prompt prefix（gemini-embedding-2 無 task_type 參數）。"""
    if not texts:
        return []
    client = _get_client()
    config = genai_types.EmbedContentConfig(output_dimensionality=EMBED_DIM)
    embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[i:i + BATCH_SIZE]
        resp = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=batch_texts,
            config=config,
        )
        embeddings.extend([e.values for e in resp.embeddings])
    return embeddings

def embed_documents(texts: list[str]) -> list[list[float]]:
    """文件側 embedding：套 `title: none | text: {content}` prefix。"""
    prefixed = [f"title: none | text: {t}" for t in texts]
    return _embed(prefixed)

def embed_query_texts(texts: list[str]) -> list[list[float]]:
    """查詢側 embedding：套 `task: search result | query: {content}` prefix。"""
    prefixed = [f"task: search result | query: {t}" for t in texts]
    return _embed(prefixed)

# 向後相容：舊呼叫端 (upsert_chunks) 仍走文件側
def embed_texts(texts: list[str]) -> list[list[float]]:
    return embed_documents(texts)

def upsert_chunks(chunks: list[dict], show_progress: bool = True) -> int:
    """
    將 chunks 批次 embedding 後寫入 BigQuery。
    回傳成功寫入的筆數。
    """
    if not chunks:
        print("[Embedder] 沒有 chunk 可寫入")
        return 0

    client = get_bq_client()
    ensure_dataset_and_table(client)

    texts = [c["content"] for c in chunks]
    print(f"[Embedder] 開始 embedding {len(texts)} 個 chunk...")

    embeddings = embed_texts(texts)

    rows_to_insert = []
    for chunk, vector in zip(chunks, embeddings):
        # Deterministic ID：source_file + source_page + chunk_index 的 SHA-256 → UUID 格式
        id_seed = (
            f"{chunk.get('source_file', '')}"
            f"::{chunk.get('source_page', 0)}"
            f"::{chunk.get('chunk_index', 0)}"
        )
        chunk_id = hashlib.sha256(id_seed.encode()).hexdigest()
        chunk_id = f"{chunk_id[:8]}-{chunk_id[8:12]}-{chunk_id[12:16]}-{chunk_id[16:20]}-{chunk_id[20:32]}"
        
        row = {
            "id": chunk_id,
            "company": chunk.get("company"),
            "quarter": chunk.get("quarter"),
            "section": chunk.get("section"),
            "content": chunk.get("content"),
            "source_file": chunk.get("source_file"),
            "source_page": chunk.get("source_page"),
            "chunk_index": chunk.get("chunk_index"),
            "embedding": vector,
        }
        rows_to_insert.append(row)

    total_written = 0
    table_id = get_table_path()
    
    iterator = range(0, len(rows_to_insert), UPSERT_BATCH)
    if show_progress:
        iterator = tqdm(iterator, desc="寫入 BigQuery", unit="批")

    for i in iterator:
        batch = rows_to_insert[i:i + UPSERT_BATCH]
        errors = client.insert_rows_json(table_id, batch)
        if errors:
            print(f"[Embedder] BigQuery 寫入錯誤: {errors}")
        else:
            total_written += len(batch)

    print(f"[Embedder] 完成，共寫入 {total_written} 筆")
    return total_written
