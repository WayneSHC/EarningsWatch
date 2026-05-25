"""
src/core/bq_client.py
自動偵測 GCP 環境，提供 BigQuery 實例。
"""

import os
import re
from functools import lru_cache
from google.cloud import bigquery

# 這裡需使用與你的 GCP project 一致的名稱
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "earningswatch-demo")
DATASET_ID = "earnings_data"
TABLE_ID = "earnings_calls"

# [f] 只允許 GCP project/dataset/table ID 的合法字元，防止 SQL f-string 注入
_SAFE_BQ_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")

@lru_cache(maxsize=1)
def get_bq_client() -> bigquery.Client:
    """回傳 singleton BigQuery Client。無金鑰認證（使用 ADC）。"""
    return bigquery.Client(project=PROJECT_ID)

def get_table_path() -> str:
    """回傳完整 Table 絕對路徑 (e.g. project.dataset.table)。"""
    for part_name, part_val in (
        ("GOOGLE_CLOUD_PROJECT", PROJECT_ID),
        ("DATASET_ID", DATASET_ID),
        ("TABLE_ID", TABLE_ID),
    ):
        if not _SAFE_BQ_ID_RE.match(part_val):
            raise ValueError(
                f"[f] {part_name} 含非法字元，拒絕組成 SQL 路徑: {part_val!r}"
            )
    return f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"

def ensure_dataset_and_table(client: bigquery.Client | None = None) -> None:
    """
    建立 Dataset 與 Table Schema (含 VECTOR_SEARCH 所需的 embedding 欄位)。
    """
    client = client or get_bq_client()
    
    # 確保 Dataset 存在
    dataset_ref = bigquery.Dataset(f"{PROJECT_ID}.{DATASET_ID}")
    dataset_ref.location = "asia-east1" # 建議設定 region，這裡以 asia-east1 為例
    try:
        client.get_dataset(dataset_ref)
        print(f"[BigQuery] Dataset {DATASET_ID} 已存在")
    except Exception:
        client.create_dataset(dataset_ref)
        print(f"[BigQuery] Dataset {DATASET_ID} 已建立")

    # 確保 Table 存在
    table_ref = dataset_ref.table(TABLE_ID)
    schema = [
        bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("company", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("quarter", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("section", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("content", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("source_file", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("source_page", "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("chunk_index", "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField(
            "embedding", 
            "FLOAT64", 
            mode="REPEATED" # 用來存儲 VECTOR 的陣列
        ),
    ]
    
    try:
        client.get_table(table_ref)
        print(f"[BigQuery] Table {TABLE_ID} 已存在")
    except Exception:
        table = bigquery.Table(table_ref, schema=schema)
        client.create_table(table)
        print(f"[BigQuery] Table {TABLE_ID} 已建立")

if __name__ == "__main__":
    ensure_dataset_and_table()
    print("✅ BigQuery 環境初始化完成")
