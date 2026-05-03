"""
src/core/retriever.py
混合檢索（Vector + BM25 + RRF 融合）→ Cohere Rerank 三階段。

[R1] 混合檢索：
  - 向量檢索（語意相近）：捕捉「需求強勁」≈「訂單暢旺」這類同義改寫
  - BM25 稀疏檢索（關鍵字精確）：補強型號/數字（CoWoS, 3nm, 53%）等命名實體
  - RRF（Reciprocal Rank Fusion）將兩路排名平均，避免單一方法的盲點
"""

import os
import re
from functools import lru_cache
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import cohere
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

from src.core.qdrant_client import get_qdrant_client, COLLECTION_NAME
from src.ingestion.embedder import embed_texts, EMBEDDING_MODEL

TOP_K_RETRIEVAL = 20   # 初步檢索（向量 + BM25 各取 K）
TOP_K_RERANK = 5       # Rerank 後保留筆數
RRF_K = 60             # [R1] RRF 平滑常數，60 為文獻常用預設值


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


# ══════════════════════════════════════════════════════════════════════════════
# [R1] BM25 稀疏檢索：lazy 建索引，依 (company, section) 快取
# ══════════════════════════════════════════════════════════════════════════════
def _tokenize_zh(text: str) -> list[str]:
    """中文 + 英文混合分詞。jieba 處理中文，正則保留英數型號（如 CoWoS, 3nm）。"""
    import jieba
    text = text or ""
    # [c] cut_for_search 對短查詢效果較佳；長文用 cut() 也可
    tokens = [t.strip().lower() for t in jieba.cut_for_search(text) if t.strip()]
    # 額外抓出純英數 token（jieba 對純英文有時切不乾淨）
    tokens += [m.group(0).lower() for m in re.finditer(r"[A-Za-z0-9]+", text)]
    # 過濾單字符雜訊（保留中文單字，過濾空白/標點）
    return [t for t in tokens if t and not t.isspace() and len(t) >= 1]


@lru_cache(maxsize=8)
def _get_bm25_index(company: str | None, section: str | None) -> tuple[Any, list[dict]] | None:
    """
    依 (company, section) 建立 BM25 索引。lru_cache 確保整個 process 只建一次。

    回傳 (bm25_okapi, chunks)；chunks 為 {id, payload, tokens} 列表。
    若該 collection 為空或 rank_bm25 未安裝，回傳 None（hybrid 自動降級為純向量）。
    """
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        print("[Retriever] rank_bm25 未安裝，BM25 檢索跳過")
        return None

    client = get_qdrant_client()
    qfilter = _build_filter(company=company, section=section)

    chunks: list[dict] = []
    offset = None
    MAX_DOCS = 5000  # [b] 防止超大 collection 一次性載入 OOM
    while True:
        try:
            result, offset = client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=qfilter,
                limit=500,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            print(f"[Retriever] BM25 索引 scroll 失敗：{e}")
            return None
        for r in result:
            content = r.payload.get("content", "") if r.payload else ""
            if not content:
                continue
            chunks.append({
                "id": r.id,
                "payload": r.payload,
                "tokens": _tokenize_zh(content),
            })
        if offset is None or len(chunks) >= MAX_DOCS:
            break

    if not chunks:
        return None

    corpus = [c["tokens"] for c in chunks]
    bm25 = BM25Okapi(corpus)
    print(f"[Retriever] BM25 索引建立完成：company={company} section={section} docs={len(chunks)}")
    return bm25, chunks


def clear_retriever_cache() -> None:
    """
    BM25 索引失效：新 PDF 匯入後呼叫，確保下次查詢重建索引。

    注意：Streamlit 與 ingestion script 是不同 process，
    此函數只能清除當前 process 的快取。
    若 ingestion 在獨立腳本中執行，Streamlit 服務需重啟才能完整反映新資料。
    """
    _get_bm25_index.cache_clear()
    print("[Retriever] BM25 索引快取已清除，下次查詢將重建")


def bm25_search(
    query: str,
    company: str | None = None,
    quarters: list[str] | None = None,
    section: str | None = None,
    top_k: int = TOP_K_RETRIEVAL,
) -> list[dict]:
    """
    BM25 稀疏檢索。quarters 過濾在記憶體中後處理（BM25 corpus 已依 company/section 切片）。
    """
    idx = _get_bm25_index(company, section)
    if idx is None:
        return []
    bm25, chunks = idx
    q_tokens = _tokenize_zh(query)
    if not q_tokens:
        return []
    scores = bm25.get_scores(q_tokens)
    # 排序取 top
    paired = sorted(
        zip(scores, chunks), key=lambda x: x[0], reverse=True
    )
    out: list[dict] = []
    for score, chunk in paired:
        if score <= 0:
            break
        # quarters filter（後處理）
        if quarters and chunk["payload"].get("quarter") not in quarters:
            continue
        out.append({
            "id": chunk["id"],
            "score": float(score),
            "payload": chunk["payload"],
        })
        if len(out) >= top_k:
            break
    return out


def _rrf_fuse(
    *ranked_lists: list[dict],
    k: int = RRF_K,
    top_k: int = TOP_K_RETRIEVAL,
) -> list[dict]:
    """
    Reciprocal Rank Fusion：對多路排名結果做平均，輸出按融合分數降序的列表。
    rrf_score = sum(1 / (k + rank_in_list))，rank 從 1 起算。
    """
    fused: dict[Any, dict] = {}  # id → {item, score}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            rid = item["id"]
            if rid not in fused:
                fused[rid] = {"item": item.copy(), "score": 0.0}
            fused[rid]["score"] += 1.0 / (k + rank)
    merged = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
    out = []
    for entry in merged[:top_k]:
        item = entry["item"]
        item["rrf_score"] = round(entry["score"], 5)
        out.append(item)
    return out


def hybrid_search(
    query: str,
    company: str | None = None,
    quarters: list[str] | None = None,
    section: str | None = None,
    top_k: int = TOP_K_RETRIEVAL,
) -> list[dict]:
    """
    [R1] 混合檢索：向量 + BM25 並行 → RRF 融合。
    若 BM25 索引建立失敗或結果為空，自動降級為純向量。
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_vec = pool.submit(vector_search, query, company, quarters, section, top_k)
        f_bm  = pool.submit(bm25_search,   query, company, quarters, section, top_k)
        try:
            vec_hits = f_vec.result()
        except Exception as e:
            print(f"[Retriever] vector_search 失敗：{e}")
            vec_hits = []
        try:
            bm_hits = f_bm.result()
        except Exception as e:
            print(f"[Retriever] bm25_search 失敗：{e}")
            bm_hits = []

    if not bm_hits:
        return vec_hits[:top_k]
    if not vec_hits:
        return bm_hits[:top_k]
    return _rrf_fuse(vec_hits, bm_hits, top_k=top_k)


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
        model="rerank-v3.5",   # 2024-12 最新 Cohere Rerank，精度↑速度持平
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
    完整三階段檢索：[R1] 向量+BM25 RRF 融合 → Cohere Rerank。
    這是外部模組呼叫的主入口。
    """
    candidates = hybrid_search(query, company, quarters, section)
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
    max_quarters: int = 8,
    use_rerank: bool = True,
) -> dict[str, list[dict]]:
    """
    補充覆蓋：對每個未出現在 retrieved 的季度，各取最相關的 top_k_per_quarter 筆。
    共用同一個 query embedding（避免重複計算）。

    [R8] use_rerank=True：每季 over-fetch（top_k_per_quarter * 3）後做 Cohere rerank
         精排，避免 boilerplate（如「以下言論不構成投資建議」）混進跨季比對。
         Cohere 未設定時自動降級為純向量結果。

    min_score: 低於此 cosine similarity 的季度視為「無相關內容」，略過不補充。
    max_quarters: 最多補充季度上限（[c] 防止 10+ 季度串行查詢拖慢整體）
    """
    if not missing_quarters:
        return {}

    # [c] 限制補充季度數：超過上限時取最新的 max_quarters 個（時效性優先）
    if len(missing_quarters) > max_quarters:
        missing_quarters = sorted(missing_quarters)[-max_quarters:]
        print(f"[Retriever] coverage sweep 超過 {max_quarters} 季，取最新 {max_quarters} 季")

    client = get_qdrant_client()
    vector = embed_query(query)   # 只算一次 embedding，所有季度共用
    result: dict[str, list[dict]] = {}

    # [R8] over-fetch 倍數：rerank 啟用時抓 3 倍候選，給精排篩選空間
    fetch_k = top_k_per_quarter * 3 if use_rerank else top_k_per_quarter

    def _fetch_one(q: str) -> tuple[str, list[dict]]:
        """單一季度 Qdrant 查詢 + 可選 rerank，供 ThreadPoolExecutor 並行呼叫。"""
        qfilter = _build_filter(company=company, quarters=[q])
        try:
            if hasattr(client, "query_points"):
                response = client.query_points(
                    collection_name=COLLECTION_NAME,
                    query=vector,
                    query_filter=qfilter,
                    limit=fetch_k,
                    with_payload=True,
                )
                hits = response.points
            else:
                hits = client.search(
                    collection_name=COLLECTION_NAME,
                    query_vector=vector,
                    query_filter=qfilter,
                    limit=fetch_k,
                    with_payload=True,
                )
            # 只保留相關度夠高的 chunk；若最佳 chunk 分數不足，跳過整個季度
            candidates = [
                {"id": r.id, "score": r.score, "payload": r.payload}
                for r in hits
                if r.score >= min_score
            ]
            if not candidates:
                # [b] 先確認 hits 非空再存取 [0]，避免 IndexError
                if hits:
                    print(f"[Retriever] {q} 最高分 {hits[0].score:.3f} < {min_score}，略過不補充")
                else:
                    print(f"[Retriever] {q} 無任何 chunk，略過不補充")
                return q, []

            # [R8] Cohere rerank 精排（rerank() 內部處理 Cohere 不可用的降級）
            if use_rerank and len(candidates) > top_k_per_quarter:
                final = rerank(query, candidates, top_n=top_k_per_quarter)
            else:
                final = candidates[:top_k_per_quarter]
            return q, final
        except Exception as e:
            print(f"[Retriever] retrieve_coverage 季度 {q} 失敗: {e}")
            return q, []

    # [c] 並行查詢所有缺漏季度：Qdrant 為 I/O bound，ThreadPoolExecutor 效果顯著
    _workers = min(len(missing_quarters), 8)
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futures = {pool.submit(_fetch_one, q): q for q in missing_quarters}
        for fut in as_completed(futures):
            q, chunks = fut.result()
            if chunks:
                result[q] = chunks

    return result
