"""
src/core/qdrant_client.py
自動偵測環境：本地 Docker (localhost:6333) 或 Qdrant Cloud
所有模組統一從這裡取得 client，不要直接 import QdrantClient。
"""

import os
from functools import lru_cache
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

COLLECTION_NAME = "earnings_calls"
VECTOR_SIZE = 768   # paraphrase-multilingual-mpnet-base-v2 維度


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    """
    回傳 singleton QdrantClient。
    - 有 QDRANT_URL → Qdrant Cloud 模式（Streamlit Cloud 部署用）
    - 沒有 QDRANT_URL → 本地 Docker 模式（本機開發用，完全免費）
    """
    qdrant_url = os.getenv("QDRANT_URL", "").strip()

    if qdrant_url:
        api_key = os.getenv("QDRANT_API_KEY")
        # [b] 雲端模式但未設定 API Key → 記錄警告，連線可能因認證失敗
        if not api_key:
            print("[Qdrant] ⚠ QDRANT_URL 已設定但 QDRANT_API_KEY 為空，連線可能失敗")
        print(f"[Qdrant] 雲端模式: {qdrant_url}")
        return QdrantClient(url=qdrant_url, api_key=api_key)
    else:
        host = os.getenv("QDRANT_HOST", "localhost")
        # [b] QDRANT_PORT 若為非數字字串，int() 會 ValueError → 防呆改為 try/except
        try:
            port = int(os.getenv("QDRANT_PORT", 6333))
        except ValueError:
            print("[Qdrant] ⚠ QDRANT_PORT 非數字，使用預設值 6333")
            port = 6333
        print(f"[Qdrant] 本地模式: {host}:{port}")
        return QdrantClient(host=host, port=port)


def ensure_collection(client: QdrantClient | None = None) -> None:
    """
    如果 Collection 不存在則建立，已存在則跳過。
    """
    client = client or get_qdrant_client()
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"[Qdrant] Collection '{COLLECTION_NAME}' 已建立")
    else:
        print(f"[Qdrant] Collection '{COLLECTION_NAME}' 已存在，跳過建立")


def health_check() -> bool:
    """簡單健康檢查，回傳 True 代表連線成功。"""
    try:
        client = get_qdrant_client()
        client.get_collections()
        return True
    except Exception as e:
        print(f"[Qdrant] 連線失敗: {e}")
        return False


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    if health_check():
        print("✅ Qdrant 連線成功")
        ensure_collection()
    else:
        print("❌ Qdrant 連線失敗，請確認 Docker 是否執行中：")
        print("   docker run -d --name qdrant -p 6333:6333 -v $(pwd)/qdrant_storage:/qdrant/storage qdrant/qdrant")
