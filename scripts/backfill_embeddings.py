"""
補生 BigQuery 中缺失的 embedding。

針對 row 已存在但 `embedding IS NULL OR ARRAY_LENGTH(embedding) = 0` 的情況
重新跑 Vertex AI embedding 並回填。採 DELETE + INSERT 模式（id 由
source_file + page + chunk_index 決定，re-insert 不會產生重複）。

執行：
    # 預覽（不寫入）
    python scripts/backfill_embeddings.py --dry-run

    # 限定公司 / 季度
    python scripts/backfill_embeddings.py --company 台積電 --quarters 2025Q4 2026Q1

    # 全部補生
    python scripts/backfill_embeddings.py
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from google.cloud import bigquery

from src.core.bq_client import get_bq_client, get_table_path
from src.ingestion.embedder import embed_texts

BATCH = 50  # 一批多少筆 row 一次 embed + write


def fetch_missing(
    client: bigquery.Client,
    table_path: str,
    company: str | None,
    quarters: list[str] | None,
) -> list[dict[str, Any]]:
    where = ["(embedding IS NULL OR ARRAY_LENGTH(embedding) = 0)"]
    params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter] = []
    if company:
        where.append("company = @company")
        params.append(bigquery.ScalarQueryParameter("company", "STRING", company))
    if quarters:
        where.append("quarter IN UNNEST(@quarters)")
        params.append(bigquery.ArrayQueryParameter("quarters", "STRING", quarters))

    sql = f"""
    SELECT id, company, quarter, section, content, source_file, source_page, chunk_index
    FROM `{table_path}`
    WHERE {' AND '.join(where)}
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = []
    for r in job.result():
        rows.append({
            "id": r.id,
            "company": r.company,
            "quarter": r.quarter,
            "section": r.section,
            "content": r.content,
            "source_file": r.source_file,
            "source_page": r.source_page,
            "chunk_index": r.chunk_index,
        })
    return rows


def delete_ids(client: bigquery.Client, table_path: str, ids: list[str]) -> None:
    sql = f"DELETE FROM `{table_path}` WHERE id IN UNNEST(@ids)"
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("ids", "STRING", ids),
    ])
    client.query(sql, job_config=job_config).result()


def insert_rows(client: bigquery.Client, table_path: str, rows: list[dict[str, Any]]) -> None:
    errors = client.insert_rows_json(table_path, rows)
    if errors:
        raise RuntimeError(f"BigQuery 寫入錯誤: {errors}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", default=None)
    parser.add_argument("--quarters", nargs="*", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = get_bq_client()
    table_path = get_table_path()

    print(f"[Backfill] 查詢缺失 embedding 的 row...")
    rows = fetch_missing(client, table_path, args.company, args.quarters)
    print(f"[Backfill] 找到 {len(rows)} 筆缺失 embedding 的 row")

    if not rows:
        print("[Backfill] 沒有需要補生的 row。")
        return 0

    # 分布摘要
    by_q: dict[str, int] = {}
    skipped_empty = 0
    for r in rows:
        if not (r.get("content") or "").strip():
            skipped_empty += 1
            continue
        key = f"{r.get('company')}/{r.get('quarter')}"
        by_q[key] = by_q.get(key, 0) + 1
    print("[Backfill] 分布（不含 content 為空者）：")
    for k, v in sorted(by_q.items()):
        print(f"  {k}: {v}")
    if skipped_empty:
        print(f"[Backfill] content 為空，將略過：{skipped_empty} 筆")

    if args.dry_run:
        print("[Backfill] --dry-run 模式，未實際寫入。")
        return 0

    # 過濾出有 content 的 row
    work = [r for r in rows if (r.get("content") or "").strip()]
    written = 0

    for i in range(0, len(work), BATCH):
        batch = work[i:i + BATCH]
        texts = [r["content"] for r in batch]
        print(f"[Backfill] batch {i // BATCH + 1}/{(len(work) + BATCH - 1) // BATCH}：embedding {len(texts)} 筆...")
        try:
            vectors = embed_texts(texts)
        except Exception as e:
            print(f"[Backfill] embedding 失敗 ({type(e).__name__}: {e})，略過此 batch")
            continue

        new_rows = []
        for r, v in zip(batch, vectors):
            new_rows.append({**r, "embedding": v})

        ids = [r["id"] for r in batch]
        try:
            delete_ids(client, table_path, ids)
            insert_rows(client, table_path, new_rows)
            written += len(new_rows)
            print(f"[Backfill]   → 已補生並寫回 {len(new_rows)} 筆")
        except Exception as e:
            print(f"[Backfill]   → 寫回失敗 ({type(e).__name__}: {e})，本 batch 跳過")

    print(f"[Backfill] 完成：補生 {written} / {len(work)} 筆")
    return 0 if written == len(work) else 1


if __name__ == "__main__":
    sys.exit(main())
