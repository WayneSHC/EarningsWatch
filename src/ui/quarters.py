"""
src/ui/quarters.py

從 BigQuery 動態讀取實際存在的季度列表（給 sidebar 下拉用）。
"""

from __future__ import annotations

import streamlit as st

@st.cache_data(ttl=300, show_spinner=False)
def get_available_quarters() -> list[str]:
    """
    從 BigQuery 動態讀取實際存在的季度列表，按時間排序。
    """
    try:
        from src.core.bq_client import get_bq_client, get_table_path
        client = get_bq_client()
        table_path = get_table_path()
        
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
        
    return ["2022Q4", "2023Q1", "2023Q2", "2023Q3",
            "2024Q2", "2024Q3", "2024Q4",
            "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1"]
