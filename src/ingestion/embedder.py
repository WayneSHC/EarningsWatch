"""
src/ingestion/embedder.py
批次 embedding + 寫入 BigQuery。
使用 Vertex AI text-multilingual-embedding-002 (768 維)。
"""

import hashlib
from functools import lru_cache
from typing import Any

from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel
from tqdm import tqdm

from src.core.bq_client import get_bq_client, get_table_path, ensure_dataset_and_table

EMBEDDING_MODEL = "text-multilingual-embedding-002"
BATCH_SIZE = 64
UPSERT_BATCH = 100

@lru_cache(maxsize=1)
def _get_model() -> TextEmbeddingModel:
    print(f"[Embedder] 載入模型 Vertex AI {EMBEDDING_MODEL}...")
    return TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)

def embed_texts(texts: list[str]) -> list[list[float]]:
    """批次取得 embedding，回傳與 texts 等長的向量列表。"""
    if not texts:
        return []
    model = _get_model()
    embeddings = []
    
    # 批次發送請求
    for i in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[i:i + BATCH_SIZE]
        inputs = [TextEmbeddingInput(t, "RETRIEVAL_DOCUMENT") for t in batch_texts]
        # model.get_embeddings 支援 list of TextEmbeddingInput
        batch_embeddings = model.get_embeddings(inputs)
        embeddings.extend([emb.values for emb in batch_embeddings])
        
    return embeddings

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
