"""
src/core/retriever.py
Qdrant 向量搜尋 + Cohere Rerank 兩階段檢索。
"""

import os
from functools import lru_cache
from typing import Any
import cohere
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

from src.core.qdrant_client import get_qdrant_client, COLLECTION_NAME
from src.ingestion.embedder import embed_texts, EMBEDDING_MODEL

TOP_K_RETRIEVAL = 20   # 初步向量搜尋取 K 筆
TOP_K_RERANK = 5       # Rerank 後保留筆數


@lru_cache(maxsize=1)
def _get_cohere_client() -> cohere.Client | None:
    """
    Lazy 初始化 Cohere client。
    在第一次實際使用時才讀取 API Key，確保 load_dotenv() 已執行。
    若未設定 COHERE_API_KEY 則回傳 None（rerank 功能會自動降級跳過）。
    """
    key = os.getenv("COHERE_API_KEY", "").strip()
    if not key:
        return None
    return cohere.Client(key)


def embed_query(text: str) -> list[float]:
    """將查詢文字轉為向量。"""
    return embed_texts([text])[0]


def _build_filter(
    company: str | None = None,
    quarters: list[str] | None = None,
    section: str | None = None,
) -> Filter | None:
    """依條件建立 Qdrant 過濾器（全為 None 時回傳 None 代表不過濾）。"""
    conditions = []
    if company:
        conditions.append(FieldCondition(key="company", match=MatchValue(value=company)))
    if quarters:
        conditions.append(FieldCondition(key="quarter", match=MatchAny(any=quarters)))
    if section:
        conditions.append(FieldCondition(key="section", match=MatchValue(value=section)))
    if not conditions:
        return None
    # Filter 已在頂部 import，直接使用，不需重複 import
    return Filter(must=conditions)


def vector_search(
    query: str,
    company: str | None = None,
    quarters: list[str] | None = None,
    section: str | None = None,
    top_k: int = TOP_K_RETRIEVAL,
) -> list[dict]:
    """
    一階段：Qdrant 向量搜尋。
    回傳 [{score, payload, id}, ...]
    相容新版 qdrant-client（>=1.7）使用 query_points；舊版 fallback 到 search。
    """
    client = get_qdrant_client()
    vector = embed_query(query)
    qfilter = _build_filter(company, quarters, section)

    # qdrant-client >= 1.7 使用 query_points；舊版用 search
    if hasattr(client, "query_points"):
        from qdrant_client.models import SearchRequest
        response = client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            query_filter=qfilter,
            limit=top_k,
            with_payload=True,
        )
        hits = response.points
    else:
        hits = client.search(
            collection_name=COLLECTION_NAME,
            query_vector=vector,
            query_filter=qfilter,
            limit=top_k,
            with_payload=True,
        )

    return [
        {"id": r.id, "score": r.score, "payload": r.payload}
        for r in hits
    ]


def rerank(query: str, candidates: list[dict], top_n: int = TOP_K_RERANK) -> list[dict]:
    """
    二階段：Cohere Rerank 精排。
    若未設定 COHERE_API_KEY，直接回傳前 top_n 筆（跳過 rerank）。
    """
    client = _get_cohere_client()
    if not client or not candidates:
        return candidates[:top_n]

    documents = [c["payload"].get("content", "") for c in candidates]
    resp = client.rerank(
        model="rerank-multilingual-v3.0",
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
    """
    完整兩階段檢索：向量搜尋 → Cohere Rerank。
    這是外部模組呼叫的主入口。
    """
    candidates = vector_search(query, company, quarters, section)
    return rerank(query, candidates, top_n=top_k)


def get_company_quarters(company: str) -> list[str]:
    """
    從 Qdrant 取得該公司所有已存在的季度列表（排序好）。
    優先使用 facet API；若不支援則 fallback 到 scroll 掃描。
    """
    client = get_qdrant_client()
    qfilter = _build_filter(company=company)
    try:
        resp = client.facet(
            collection_name=COLLECTION_NAME,
            key="quarter",
            filter=qfilter,
            limit=50,
        )
        quarters = [hit.value for hit in resp.hits if hit.value]
        if quarters:
            return sorted(quarters, key=lambda x: (x[:4], x[4:]))
    except Exception:
        pass
    # fallback: scroll scan（facet API 不支援時使用）
    quarters: set[str] = set()
    offset = None
    # [b] 加入最大掃描上限，與 app.py 保持一致，防止超大 collection 無限迴圈
    MAX_SCROLL_SCAN = 3000
    total_scanned = 0
    while True:
        result, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qfilter,
            limit=300,
            offset=offset,
            with_payload=["quarter"],
            with_vectors=False,
        )
        for r in result:
            q = r.payload.get("quarter", "")
            if q:
                quarters.add(q)
        total_scanned += len(result)
        if offset is None or total_scanned >= MAX_SCROLL_SCAN:
            break
    return sorted(quarters, key=lambda x: (x[:4], x[4:]))


def retrieve_coverage(
    query: str,
    company: str,
    missing_quarters: list[str],
    top_k_per_quarter: int = 2,
    min_score: float = 0.25,
) -> dict[str, list[dict]]:
    """
    補充覆蓋：對每個未出現在 retrieved 的季度，各取最相關的 top_k_per_quarter 筆。
    共用同一個 query embedding（避免重複計算），不做 rerank（速度優先）。

    min_score: 低於此 cosine similarity 的季度視為「無相關內容」，略過不補充，
               避免把 boilerplate / 表格資料誤送進矛盾比對。
    """
    if not missing_quarters:
        return {}

    client = get_qdrant_client()
    vector = embed_query(query)   # 只算一次 embedding，所有季度共用
    result: dict[str, list[dict]] = {}

    for q in missing_quarters:
        qfilter = _build_filter(company=company, quarters=[q])
        try:
            if hasattr(client, "query_points"):
                response = client.query_points(
                    collection_name=COLLECTION_NAME,
                    query=vector,
                    query_filter=qfilter,
                    limit=top_k_per_quarter,
                    with_payload=True,
                )
                hits = response.points
            else:
                hits = client.search(
                    collection_name=COLLECTION_NAME,
                    query_vector=vector,
                    query_filter=qfilter,
                    limit=top_k_per_quarter,
                    with_payload=True,
                )
            # 只保留相關度夠高的 chunk；若最佳 chunk 分數不足，跳過整個季度
            chunks = [
                {"id": r.id, "score": r.score, "payload": r.payload}
                for r in hits
                if r.score >= min_score
            ]
            if chunks:
                result[q] = chunks
            else:
                # [b] 先確認 hits 非空再存取 [0]，避免 IndexError
                if hits:
                    print(f"[Retriever] {q} 最高分 {hits[0].score:.3f} < {min_score}，略過不補充")
                else:
                    print(f"[Retriever] {q} 無任何 chunk，略過不補充")
        except Exception as e:
            print(f"[Retriever] retrieve_coverage 季度 {q} 失敗: {e}")

    return result
