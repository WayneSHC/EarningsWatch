"""
診斷 BigQuery 檢索為何回傳 0 chunk。

執行：
    python scripts/probe_retrieval.py --company 台積電 --quarters 2025Q4 2026Q1 --query "AI需求"

會檢查：
  1. 該公司 / 季度組合在 BQ 中有多少 row
  2. 其中 embedding 為 NULL / 空陣列的 row 數
  3. content 為空的 row 數
  4. section 的分布
  5. 直接呼叫 vector_search 看實際回傳幾筆
"""

import argparse
from src.core.bq_client import get_bq_client, get_table_path
from src.core.retriever import vector_search
from google.cloud import bigquery


def probe(company: str, quarters: list[str], query: str) -> None:
    client = get_bq_client()
    table_path = get_table_path()

    print(f"\n=== 1. 基本 row 計數 ===")
    sql = f"""
    SELECT
        quarter,
        COUNT(*) as total_rows,
        COUNTIF(embedding IS NULL OR ARRAY_LENGTH(embedding) = 0) as empty_embedding_rows,
        COUNTIF(content IS NULL OR content = '') as empty_content_rows,
        COUNT(DISTINCT section) as section_count
    FROM `{table_path}`
    WHERE company = @company AND quarter IN UNNEST(@quarters)
    GROUP BY quarter
    ORDER BY quarter
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("company", "STRING", company),
        bigquery.ArrayQueryParameter("quarters", "STRING", quarters),
    ])
    for row in client.query(sql, job_config=job_config).result():
        print(f"  {row.quarter}: rows={row.total_rows}  empty_emb={row.empty_embedding_rows}  empty_content={row.empty_content_rows}  sections={row.section_count}")

    print(f"\n=== 2. Section 分布 ===")
    sql = f"""
    SELECT quarter, section, COUNT(*) as n
    FROM `{table_path}`
    WHERE company = @company AND quarter IN UNNEST(@quarters)
    GROUP BY quarter, section
    ORDER BY quarter, n DESC
    """
    for row in client.query(sql, job_config=job_config).result():
        print(f"  {row.quarter} / {row.section!r}: {row.n}")

    print(f"\n=== 3. 實際呼叫 vector_search（無 section 過濾）===")
    hits = vector_search(query=query, company=company, quarters=quarters, top_k=20)
    print(f"  回傳 {len(hits)} 筆")
    for h in hits[:5]:
        p = h.get("payload", {})
        print(f"    score={h.get('score'):.3f}  q={p.get('quarter')}  sec={p.get('section')!r}  content={p.get('content','')[:80]!r}")

    print(f"\n=== 4. 同 query 但不限季度 ===")
    hits2 = vector_search(query=query, company=company, quarters=None, top_k=10)
    print(f"  回傳 {len(hits2)} 筆")
    for h in hits2[:5]:
        p = h.get("payload", {})
        print(f"    score={h.get('score'):.3f}  q={p.get('quarter')}  content={p.get('content','')[:80]!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", default="台積電")
    parser.add_argument("--quarters", nargs="+", default=["2025Q4", "2026Q1"])
    parser.add_argument("--query", default="AI需求")
    args = parser.parse_args()
    probe(args.company, args.quarters, args.query)


if __name__ == "__main__":
    main()
