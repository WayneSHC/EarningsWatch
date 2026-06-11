"""
src/core/bq_client.py
自動偵測 GCP 環境，提供 BigQuery 實例。
"""

import os
import re
from functools import lru_cache
from google.cloud import bigquery

DATASET_ID = "earnings_data"
TABLE_ID = "earnings_calls"

# [f] 只允許 GCP project/dataset/table ID 的合法字元，防止 SQL f-string 注入
_SAFE_BQ_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _sa_info_from_streamlit_secrets() -> dict | None:
    # [b] Streamlit Cloud 沒有 ADC，金鑰放在 st.secrets["gcp_service_account"]。
    #     本機 / Cloud Run 沒有 secrets.toml → 回 None 走 ADC。
    #     注意：這是 core 層唯一允許 soft-import streamlit 的例外
    #     （ImportError / 無 secrets 都安全降級，不影響獨立測試）。
    try:
        import streamlit as st
    except ImportError:
        return None
    try:
        return dict(st.secrets["gcp_service_account"])
    except Exception:
        return None


def _credentials_from_streamlit_secrets():
    sa_info = _sa_info_from_streamlit_secrets()
    if sa_info is None:
        return None
    from google.oauth2 import service_account
    # [b] 故意不包 try：金鑰存在但格式壞掉應該大聲失敗，
    #     而非靜默退回 ADC 把錯誤藏到更難排查的地方。
    return service_account.Credentials.from_service_account_info(sa_info)


def _resolve_project_id() -> str:
    # [b] 專案 ID 解析順序：env → SA 金鑰內的 project_id → demo 預設。
    #     確保只設了 gcp_service_account（忘了 GOOGLE_CLOUD_PROJECT）的
    #     Streamlit Cloud 部署，client 與 get_table_path() 仍指向同一個專案。
    env_project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if env_project:
        return env_project
    sa_info = _sa_info_from_streamlit_secrets()
    if sa_info and sa_info.get("project_id"):
        return sa_info["project_id"]
    return "earningswatch-demo"


# 這裡需使用與你的 GCP project 一致的名稱
PROJECT_ID = _resolve_project_id()


@lru_cache(maxsize=1)
def get_bq_client() -> bigquery.Client:
    """回傳 singleton BigQuery Client。優先讀 Streamlit secrets，否則走 ADC。"""
    creds = _credentials_from_streamlit_secrets()
    if creds is not None:
        return bigquery.Client(project=PROJECT_ID, credentials=creds)
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
