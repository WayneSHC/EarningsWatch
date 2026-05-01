"""
scripts/migrate_to_cloud.py
Day 11：將本地 Qdrant Docker 的所有向量資料遷移至 Qdrant Cloud

執行方式：
    # 先在 .env 填入 QDRANT_URL 和 QDRANT_API_KEY
    # 然後：
    python scripts/migrate_to_cloud.py

注意：遷移完成後，Streamlit Cloud 部署時設定同樣的環境變數即可自動連接雲端。
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from src.core.qdrant_client import COLLECTION_NAME, VECTOR_SIZE


def migrate():
    # ── 建立本地和雲端 client ────────────────────────────────────────
    local_host = os.getenv("QDRANT_HOST", "localhost")
    local_port = int(os.getenv("QDRANT_PORT", 6333))
    cloud_url = os.getenv("QDRANT_URL", "").strip()
    cloud_key = os.getenv("QDRANT_API_KEY", "").strip()

    if not cloud_url:
        print("❌ 請先在 .env 填入 QDRANT_URL（Qdrant Cloud 的 endpoint URL）")
        print("   格式：https://xxxx-xxxx.qdrant.io")
        sys.exit(1)

    if not cloud_key:
        print("❌ 請先在 .env 填入 QDRANT_API_KEY（Qdrant Cloud 的 API Key）")
        sys.exit(1)

    print(f"📡 連接本地 Qdrant: {local_host}:{local_port}")
    local = QdrantClient(host=local_host, port=local_port)

    print(f"☁️  連接 Qdrant Cloud: {cloud_url}")
    cloud = QdrantClient(url=cloud_url, api_key=cloud_key)

    # ── 確認本地 Collection 存在 ─────────────────────────────────────
    local_collections = {c.name for c in local.get_collections().collections}
    if COLLECTION_NAME not in local_collections:
        print(f"❌ 本地 Collection '{COLLECTION_NAME}' 不存在，請先執行 run_ingestion.py")
        sys.exit(1)

    # ── 查詢本地資料量 ────────────────────────────────────────────────
    local_count = local.get_collection(COLLECTION_NAME).points_count
    print(f"📊 本地資料量：{local_count} 筆向量")

    # ── 在雲端建立 Collection ─────────────────────────────────────────
    cloud_collections = {c.name for c in cloud.get_collections().collections}
    if COLLECTION_NAME in cloud_collections:
        print(f"⚠  雲端 Collection '{COLLECTION_NAME}' 已存在")
        confirm = input("是否覆蓋？(y/N): ").strip().lower()
        if confirm != "y":
            print("取消遷移")
            sys.exit(0)
        cloud.delete_collection(COLLECTION_NAME)

    cloud.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"✅ 雲端 Collection '{COLLECTION_NAME}' 建立完成")

    # ── 分批遷移 ─────────────────────────────────────────────────────
    offset = None
    total_migrated = 0
    batch_size = 100

    print("\n🚀 開始遷移...")
    while True:
        results, next_offset = local.scroll(
            collection_name=COLLECTION_NAME,
            limit=batch_size,
            offset=offset,
            with_vectors=True,
            with_payload=True,
        )
        if not results:
            break

        cloud.upsert(collection_name=COLLECTION_NAME, points=results)
        total_migrated += len(results)
        pct = total_migrated / local_count * 100 if local_count > 0 else 100
        print(f"  已遷移 {total_migrated} / {local_count} 筆 ({pct:.1f}%)", end="\r")

        if next_offset is None:
            break
        offset = next_offset

    print(f"\n✅ 遷移完成！共 {total_migrated} 筆向量已上傳至 Qdrant Cloud")
    print(f"\n下一步：")
    print(f"  1. 在 Streamlit Cloud Secrets 設定：")
    print(f"     QDRANT_URL = {cloud_url}")
    print(f"     QDRANT_API_KEY = <your-key>")
    print(f"  2. 其他 API Keys 也一併設定")
    print(f"  3. 點擊 Deploy，約 3-5 分鐘後取得公開 URL")


if __name__ == "__main__":
    migrate()
