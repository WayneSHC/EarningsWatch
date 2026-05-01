"""
src/ingestion/embedder.py
批次 embedding + 寫入 Qdrant。
使用 paraphrase-multilingual-mpnet-base-v2（768 維），本地執行，完全免費。
"""

import hashlib
from functools import lru_cache
from typing import Any

from sentence_transformers import SentenceTransformer
from tqdm import tqdm
from qdrant_client.models import PointStruct

from src.core.qdrant_client import get_qdrant_client, ensure_collection, COLLECTION_NAME

EMBEDDING_MODEL = "paraphrase-multilingual-mpnet-base-v2"
BATCH_SIZE = 64
UPSERT_BATCH = 100


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    print(f"[Embedder] 載入模型 {EMBEDDING_MODEL}（首次載入需下載約 420MB）...")
    return SentenceTransformer(EMBEDDING_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批次取得 embedding，回傳與 texts 等長的向量列表。"""
    model = _get_model()
    embeddings = model.encode(
        texts,
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return embeddings.tolist()


def upsert_chunks(chunks: list[dict], show_progress: bool = True) -> int:
    """
    將 chunks 批次 embedding 後寫入 Qdrant。
    回傳成功寫入的筆數。
    """
    if not chunks:
        print("[Embedder] 沒有 chunk 可寫入")
        return 0

    client = get_qdrant_client()
    ensure_collection(client)

    texts = [c["content"] for c in chunks]
    print(f"[Embedder] 開始 embedding {len(texts)} 個 chunk...")

    embeddings = embed_texts(texts)

    points = []
    for chunk, vector in zip(chunks, embeddings):
        payload = {k: v for k, v in chunk.items() if k != "content"}
        payload["content"] = chunk["content"]
        # Deterministic ID：source_file + source_page + chunk_index 的 SHA-256 → UUID 格式
        # 三欄組合確保跨頁的同序號 chunk 不互相覆蓋
        # 重複匯入同一 PDF 時 upsert 會覆蓋而非新增，徹底防止重複
        id_seed = (
            f"{chunk.get('source_file', '')}"
            f"::{chunk.get('source_page', 0)}"
            f"::{chunk.get('chunk_index', 0)}"
        )
        chunk_id = hashlib.sha256(id_seed.encode()).hexdigest()
        chunk_id = f"{chunk_id[:8]}-{chunk_id[8:12]}-{chunk_id[12:16]}-{chunk_id[16:20]}-{chunk_id[20:32]}"
        points.append(PointStruct(
            id=chunk_id,
            vector=vector,
            payload=payload,
        ))

    total_written = 0
    iterator = range(0, len(points), UPSERT_BATCH)
    if show_progress:
        iterator = tqdm(iterator, desc="寫入 Qdrant", unit="批")

    for i in iterator:
        batch = points[i:i + UPSERT_BATCH]
        client.upsert(collection_name=COLLECTION_NAME, points=batch)
        total_written += len(batch)

    print(f"[Embedder] 完成，共寫入 {total_written} 筆")
    return total_written
