"""
src/core/retriever.py
混合檢索與 Rerank 三階段 (基於 BigQuery Vector Search 重構)。
"""

import os
from functools import lru_cache
from typing import Any
import cohere
from google.cloud import bigquery

from src.core.bq_client import get_bq_client, get_table_path
from src.core.secrets import get_secret
from src.ingestion.embedder import embed_texts, EMBEDDING_MODEL

TOP_K_RETRIEVAL = 20
TOP_K_RERANK = 5

_HYDE_ENABLED = os.getenv("LLM_HYDE_ENABLED", "false").strip().lower() in ("true", "1", "yes")
_HYDE_MIN_QUERY_LEN = 6

@lru_cache(maxsize=128)
def _hyde_expand(query: str) -> str:
    from src.core.llm_client import chat as _llm_chat
    prompt = (
        "你是台灣半導體分析師。針對以下問題，請用法說會逐字稿的口吻寫一段"
        "假設性回答（80~150 字繁體中文）。"
        "盡量使用法說會常見詞彙（如「需求強勁」「庫存調整」「毛利率指引」"
        "「先進製程」「美國廠成本」）。只回傳回答內文，不要有引言或標題。\n\n"
        f"問題：{query}"
    )
    try:
        return _llm_chat(prompt, max_tokens=200, mode="dev").strip() or query
    except Exception as e:
        print(f"[Retriever] HyDE 生成失敗（{type(e).__name__}），降級為原 query")
        return query

def _maybe_expand(query: str) -> str:
    if not _HYDE_ENABLED or len(query.strip()) < _HYDE_MIN_QUERY_LEN:
        return query
    return _hyde_expand(query)

@lru_cache(maxsize=1)
def _get_cohere_client() -> cohere.Client | None:
    key = get_secret("COHERE_API_KEY")
    if not key:
        return None
    return cohere.Client(key)

def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]

def clear_retriever_cache() -> None:
    pass  # BigQuery 不需要清空本地 BM25 索引快取

def _build_where_clause(company: str | None, quarters: list[str] | None, section: str | None) -> tuple[str, dict]:
    conditions = []
    params = {}
    if company:
        conditions.append("company = @company")
        params["company"] = company
    if quarters:
        conditions.append("quarter IN UNNEST(@quarters)")
        params["quarters"] = quarters
    if section:
        conditions.append("section = @section")
        params["section"] = section
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    return where_clause, params

def vector_search(
    query: str,
    company: str | None = None,
    quarters: list[str] | None = None,
    section: str | None = None,
    top_k: int = TOP_K_RETRIEVAL,
) -> list[dict]:
    """
    一階段：BigQuery 向量搜尋。
    回傳 [{score, payload, id}, ...]
    """
    client = get_bq_client()
    table_path = get_table_path()
    vector = embed_query(_maybe_expand(query))
    
    where_clause, params_dict = _build_where_clause(company, quarters, section)
    
    # query_parameters
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("vector", "FLOAT64", vector),
        ]
    )
    if "company" in params_dict:
        job_config.query_parameters.append(bigquery.ScalarQueryParameter("company", "STRING", params_dict["company"]))
    if "quarters" in params_dict:
        job_config.query_parameters.append(bigquery.ArrayQueryParameter("quarters", "STRING", params_dict["quarters"]))
    if "section" in params_dict:
        job_config.query_parameters.append(bigquery.ScalarQueryParameter("section", "STRING", params_dict["section"]))

    # [b] 把 company / quarter / section 篩選 push down 到 VECTOR_SEARCH 的 base table 子查詢，
    # 而不是在外層 WHERE 過濾。原本外層 WHERE 用裸欄位名（company / quarter / section）會 BadRequest，
    # 因為 VECTOR_SEARCH 結果把原始欄位包進 struct 叫 base，欄位需以 base.X 引用才存在。
    # 改成 pre-filter 同時順手修掉「全表 top_k=20 → 篩過後常剩 0 筆」的 recall 漏洞。
    sql = f"""
    SELECT
        base.id, base.company, base.quarter, base.section, base.content,
        base.source_file, base.source_page, base.chunk_index,
        distance
    FROM VECTOR_SEARCH(
        (SELECT * FROM `{table_path}` WHERE {where_clause}),
        'embedding',
        (SELECT @vector AS embedding),
        top_k => @top_k,
        distance_type => 'COSINE'
    )
    """
    job_config.query_parameters.append(bigquery.ScalarQueryParameter("top_k", "INT64", top_k))
    
    query_job = client.query(sql, job_config=job_config)
    results = query_job.result()
    
    hits = []
    for row in results:
        # Cosine distance in BQ -> Cosine similarity = 1 - distance
        score = 1.0 - row.distance
        payload = {
            "company": row.company,
            "quarter": row.quarter,
            "section": row.section,
            "content": row.content,
            "source_file": row.source_file,
            "source_page": row.source_page,
            "chunk_index": row.chunk_index,
        }
        hits.append({"id": row.id, "score": score, "payload": payload})
        
    return hits

def rerank(query: str, candidates: list[dict], top_n: int = TOP_K_RERANK) -> list[dict]:
    client = _get_cohere_client()
    if not client or not candidates:
        return candidates[:top_n]

    documents = [c["payload"].get("content", "") for c in candidates]
    resp = client.rerank(
        model="rerank-v3.5",
        query=query,
        documents=documents,
        top_n=top_n,
    )
    reranked = []
    for r in resp.results:
        item = candidates[r.index].copy()
        item["rerank_score"] = r.relevance_score
        reranked.append(item)
    return reranked

def retrieve(
    query: str,
    company: str | None = None,
    quarters: list[str] | None = None,
    section: str | None = None,
    top_k: int = TOP_K_RERANK,
) -> list[dict]:
    # 直接使用 vector search，取代先前的 hybrid (BM25 + Vector + RRF)
    candidates = vector_search(query, company, quarters, section, top_k=TOP_K_RETRIEVAL)
    return rerank(query, candidates, top_n=top_k)

def get_company_quarters(company: str) -> list[str]:
    """從 BigQuery 取得該公司所有已存在的季度列表（排序好）。"""
    client = get_bq_client()
    table_path = get_table_path()
    
    sql = f"""
    SELECT DISTINCT quarter 
    FROM `{table_path}`
    WHERE company = @company AND quarter IS NOT NULL
    ORDER BY quarter
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("company", "STRING", company),
        ]
    )
    query_job = client.query(sql, job_config=job_config)
    results = query_job.result()
    
    quarters = [row.quarter for row in results if row.quarter]
    return sorted(quarters, key=lambda x: (x[:4], x[4:]))

def retrieve_coverage(
    query: str,
    company: str,
    missing_quarters: list[str],
    top_k_per_quarter: int = 2,
    min_score: float = 0.25,
    max_quarters: int = 8,
    use_rerank: bool = True,
) -> dict[str, list[dict]]:
    """
    利用 BigQuery SQL 的 PARTITION BY 一次撈取所有 missing_quarters 的 Top-K，
    簡化過去迴圈多次呼叫 SQL 的邏輯。
    """
    if not missing_quarters:
        return {}

    if len(missing_quarters) > max_quarters:
        missing_quarters = sorted(missing_quarters)[-max_quarters:]
        print(f"[Retriever] coverage sweep 超過 {max_quarters} 季，取最新 {max_quarters} 季")

    client = get_bq_client()
    table_path = get_table_path()
    vector = embed_query(_maybe_expand(query))
    
    fetch_k = top_k_per_quarter * 3 if use_rerank else top_k_per_quarter
    # BQ distance_type=COSINE, similarity = 1 - distance, thus max_distance = 1 - min_score
    max_distance = 1.0 - min_score
    
    sql = f"""
    SELECT * FROM (
        SELECT 
            base.id, base.company, base.quarter, base.section, base.content, 
            base.source_file, base.source_page, base.chunk_index,
            distance,
            ROW_NUMBER() OVER(PARTITION BY base.quarter ORDER BY distance) as rn
        FROM VECTOR_SEARCH(
            TABLE `{table_path}`,
            'embedding',
            (SELECT @vector AS embedding),
            top_k => 200, -- 放寬整體檢索量，由後續視窗函數篩選
            distance_type => 'COSINE'
        )
        WHERE base.company = @company 
          AND base.quarter IN UNNEST(@quarters) 
          AND distance <= @max_distance
    )
    WHERE rn <= @fetch_k
    """
    
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("vector", "FLOAT64", vector),
            bigquery.ScalarQueryParameter("company", "STRING", company),
            bigquery.ArrayQueryParameter("quarters", "STRING", missing_quarters),
            bigquery.ScalarQueryParameter("max_distance", "FLOAT64", max_distance),
            bigquery.ScalarQueryParameter("fetch_k", "INT64", fetch_k),
        ]
    )
    
    query_job = client.query(sql, job_config=job_config)
    results = query_job.result()
    
    # 組合各季度的 chunks
    raw_results = {}
    for row in results:
        q = row.quarter
        score = 1.0 - row.distance
        payload = {
            "company": row.company,
            "quarter": row.quarter,
            "section": row.section,
            "content": row.content,
            "source_file": row.source_file,
            "source_page": row.source_page,
            "chunk_index": row.chunk_index,
        }
        item = {"id": row.id, "score": score, "payload": payload}
        if q not in raw_results:
            raw_results[q] = []
        raw_results[q].append(item)
        
    final_result = {}
    for q, candidates in raw_results.items():
        if use_rerank and len(candidates) > top_k_per_quarter:
            final_result[q] = rerank(query, candidates, top_n=top_k_per_quarter)
        else:
            final_result[q] = candidates[:top_k_per_quarter]
            
    # 對沒有找到結果的季度，印出警告或略過 (維持和之前相同的邏輯)
    for q in missing_quarters:
        if q not in final_result:
            print(f"[Retriever] {q} 無任何符合 chunk (分數不足 {min_score})，略過不補充")
            
    return final_result
