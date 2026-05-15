"""
src/ui/quarters.py

從 BigQuery 動態讀取實際存在的季度列表（給 sidebar 下拉用）。
"""

from __future__ import annotations

import streamlit as st

# [b] 公司未匯入時的兜底季度列表 — 僅在 BQ 連線失敗時使用，避免下拉空白。
_FALLBACK_QUARTERS = [
    "2022Q4", "2023Q1", "2023Q2", "2023Q3",
    "2024Q2", "2024Q3", "2024Q4",
    "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1",
]


@st.cache_data(ttl=300, show_spinner=False)
def get_available_quarters(company: str | None = None) -> list[str]:
    """
    從 BigQuery 動態讀取季度列表，按時間排序。

    Args:
        company: 指定公司時只回傳該公司實際已匯入的季度，
            避免使用者選到「下拉有但該公司沒資料」的季度導致 0 chunk。
            None 時回傳所有公司的季度聯集（向後相容）。
    """
    try:
        from src.core.bq_client import get_bq_client, get_table_path
        client = get_bq_client()
        table_path = get_table_path()

        if company:
            sql = f"""
            SELECT DISTINCT quarter
            FROM `{table_path}`
            WHERE company = @company AND quarter IS NOT NULL
            ORDER BY quarter
            """
            from google.cloud import bigquery
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("company", "STRING", company),
                ]
            )
            query_job = client.query(sql, job_config=job_config)
        else:
            sql = f"""
            SELECT DISTINCT quarter
            FROM `{table_path}`
            WHERE quarter IS NOT NULL
            ORDER BY quarter
            """
            query_job = client.query(sql)

        results = query_job.result()
        quarters = [row.quarter for row in results if row.quarter]

        if quarters:
            return sorted(quarters, key=lambda x: (x[:4], x[4:]))

    except Exception as _e:
        print(f"[UI] get_available_quarters 意外失敗: {type(_e).__name__}: {_e}")

    return list(_FALLBACK_QUARTERS)


@st.cache_data(ttl=300, show_spinner=False)
def get_available_quarters_union(companies: tuple[str, ...]) -> list[str]:
    """
    多公司模式：回傳所選公司「已匯入季度」的聯集。
    至少一家公司有資料的季度才會出現在下拉，避免下拉混入未選公司獨有的季度。

    Args:
        companies: 公司名稱 tuple（用 tuple 才能參與 @st.cache_data 的 hash）
    """
    if not companies:
        return get_available_quarters()
    seen: set[str] = set()
    for c in companies:
        for q in get_available_quarters(c):
            seen.add(q)
    return sorted(seen, key=lambda x: (x[:4], x[4:]))
