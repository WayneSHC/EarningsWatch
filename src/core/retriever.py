"""
src/core/retriever.py
混合檢索與 Rerank 三階段 (基於 BigQuery Vector Search 重構)。
"""

import os
import time
from collections import deque
from functools import lru_cache
from threading import Lock
from typing import Any
import cohere
from google.cloud import bigquery

from src.core.bq_client import get_bq_client, get_table_path
from src.core.secrets import get_secret
from src.ingestion.embedder import embed_query_texts, EMBEDDING_MODEL

TOP_K_RETRIEVAL = 20
TOP_K_RERANK = 5

_HYDE_ENABLED = os.getenv("LLM_HYDE_ENABLED", "false").strip().lower() in ("true", "1", "yes")
_HYDE_MIN_QUERY_LEN = 6

# coverage sweep 相似度門檻：低於此分的 chunk 不會被補回缺漏季度。
# 可由環境變數 COVERAGE_MIN_SCORE 覆寫；非法值會 fallback 至預設並印警告（不 raise）。
_DEFAULT_MIN_SCORE = 0.25


def _load_min_score_from_env() -> float:
    """讀取 COVERAGE_MIN_SCORE 環境變數；缺漏或非法時回傳 _DEFAULT_MIN_SCORE。

    Returns:
        float in [0.0, 1.0]. 永不 raise — retrieval 是核心讀路徑，
        環境變數打錯不該整個服務掛掉，給安全預設並印警告即可。
    """
    raw = os.getenv("COVERAGE_MIN_SCORE")
    if raw is None or raw.strip() == "":
        return _DEFAULT_MIN_SCORE
    try:
        val = float(raw)
    except (TypeError, ValueError):
        print(
            f"[Retriever] ⚠ COVERAGE_MIN_SCORE={raw!r} 不是合法 float，"
            f"fallback 至預設 {_DEFAULT_MIN_SCORE}"
        )
        return _DEFAULT_MIN_SCORE
    if not (0.0 <= val <= 1.0):
        print(
            f"[Retriever] ⚠ COVERAGE_MIN_SCORE={val} 超出 [0.0, 1.0] 區間，"
            f"fallback 至預設 {_DEFAULT_MIN_SCORE}"
        )
        return _DEFAULT_MIN_SCORE
    return val

def _log_bq_error(where: str, exc: BaseException) -> None:
    """[b][f] 統一 BigQuery 錯誤記錄：截斷訊息至 120 字，僅記錄類型 + 短訊息。

    BigQuery 例外訊息可能含 project ID / table path / credentials 路徑等敏感資訊；
    將其原樣印到 UI 或 log 違反 Constitution Principle VI（不洩漏端點 / 憑證）。
    此 helper 統一截斷以符合 contradiction.py 中 _unwrap + str(...)[:120] 的成例。
    """
    msg = str(exc)[:120]
    print(f"[Retriever] ⚠ {where} BigQuery 失敗（{type(exc).__name__}: {msg}）")


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


# ── [b] Cohere rerank 速率節流 ────────────────────────────────────────────────
# Cohere Trial key 限 10 calls/min。互動式單次查詢只發少數 rerank 呼叫，遠低於
# 上限、零延遲；但爆量負載（benchmark、多公司並行）會超限觸發 429，使 rerank
# 整批失效、檢索品質下降。本節流器用 sliding-window 計數：每 60 秒視窗內前
# COHERE_MAX_RPM 次呼叫無延遲通過，超出的呼叫 block 到有空位才放行 —— 讓免費
# key 在所有情境都正確運作（互動零感、爆量自動排隊）。COHERE_MAX_RPM=0 關閉
# 節流（production key 上限較高時用）。
def _load_cohere_max_rpm() -> int:
    raw = os.getenv("COHERE_MAX_RPM", "10")
    try:
        val = int(raw)
        return val if val >= 0 else 10
    except (TypeError, ValueError):
        print(f"[Retriever] ⚠ COHERE_MAX_RPM={raw!r} 非整數，使用預設 10")
        return 10


class _CohereThrottle:
    """[c] thread-safe sliding-window rate limiter for Cohere rerank calls."""

    _WINDOW_SEC = 60.0

    def __init__(self, max_rpm: int) -> None:
        self._max_rpm = max_rpm
        self._lock = Lock()
        self._calls: deque[float] = deque()

    def _purge(self, now: float) -> None:
        """移除滑動視窗外的呼叫記錄（呼叫時須持有 _lock）。"""
        while self._calls and now - self._calls[0] >= self._WINDOW_SEC:
            self._calls.popleft()

    def acquire(self) -> None:
        """取得一個呼叫額度；視窗已滿時 block 到有空位。max_rpm<=0 時直接放行。"""
        if self._max_rpm <= 0:
            return
        with self._lock:
            now = time.time()
            self._purge(now)
            if len(self._calls) >= self._max_rpm:
                # 視窗已滿 → 等到最舊一筆滿 60s 才有空位
                wait = self._WINDOW_SEC - (now - self._calls[0])
                if wait > 0:
                    print(f"[Retriever] Cohere 節流：{wait:.1f}s 後再發 rerank")
                    time.sleep(wait)
                now = time.time()
                self._purge(now)
            self._calls.append(time.time())


_cohere_throttle = _CohereThrottle(_load_cohere_max_rpm())

def embed_query(text: str) -> list[float]:
    return embed_query_texts([text])[0]

def clear_retriever_cache() -> None:
    pass  # BigQuery 為 serverless，沒有本地索引需要清空（保留 API 給 ingestion 呼叫）

def _collect_filter_params(
    company: str | None,
    quarters: list[str] | None,
    section: str | None,
) -> dict:
    """Collect non-empty filter values into a dict for BigQuery param binding.

    Returns only the params dict — the actual WHERE clause is built inline
    in `vector_search` using `base.<column>` references inside VECTOR_SEARCH
    (filter pushdown — see comment in vector_search).
    """
    params: dict = {}
    if company:
        params["company"] = company
    if quarters:
        params["quarters"] = quarters
    if section:
        params["section"] = section
    return params

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
    
    params_dict = _collect_filter_params(company, quarters, section)

    # [b] 在 QueryJobConfig 建構時就一次性傳入完整 params list。
    # 新版 google-cloud-bigquery 的 query_parameters property 回傳的是 list 的副本，
    # 對它 .append() 不會反映到實際 job_config，會導致 BadRequest('parameter not found')。
    params: list = [bigquery.ArrayQueryParameter("vector", "FLOAT64", vector)]
    if "company" in params_dict:
        params.append(bigquery.ScalarQueryParameter("company", "STRING", params_dict["company"]))
    if "quarters" in params_dict:
        params.append(bigquery.ArrayQueryParameter("quarters", "STRING", params_dict["quarters"]))
    if "section" in params_dict:
        params.append(bigquery.ScalarQueryParameter("section", "STRING", params_dict["section"]))
    job_config = bigquery.QueryJobConfig(query_parameters=params)

    # [b] 把 company / quarter / section 篩選 push down 到 VECTOR_SEARCH 的 base table 子查詢，
    # 而不是在外層 WHERE 過濾。原本外層 WHERE 用裸欄位名（company / quarter / section）會 BadRequest，
    # 因為 VECTOR_SEARCH 結果把原始欄位包進 struct 叫 base，欄位需以 base.X 引用才存在。
    # 改成 pre-filter 同時順手修掉「全表 top_k=20 → 篩過後常剩 0 筆」的 recall 漏洞。
    # [b] VECTOR_SEARCH 的限制：
    #   1) base_table_query 不可使用 query parameter（BQ 限制） → 篩選必須放外層 WHERE
    #   2) 外層 WHERE 須用 base.X 引用（VECTOR_SEARCH 把原始列包進 struct）
    #   3) 為避免「全表 top_k=20 → 篩過後 0 筆」的 recall 漏洞，內層 top_k 放寬，外層再截 top_k
    #   top_k 必須是 literal（VECTOR_SEARCH named arg 不接受 query parameter）
    inner_k = max(int(top_k) * 20, 200)
    base_where_parts = []
    if company:
        base_where_parts.append("base.company = @company")
    if quarters:
        base_where_parts.append("base.quarter IN UNNEST(@quarters)")
    if section:
        base_where_parts.append("base.section = @section")
    base_where = " AND ".join(base_where_parts) if base_where_parts else "TRUE"
    sql = f"""
    SELECT
        base.id, base.company, base.quarter, base.section, base.content,
        base.source_file, base.source_page, base.chunk_index,
        distance
    FROM VECTOR_SEARCH(
        TABLE `{table_path}`,
        'embedding',
        (SELECT @vector AS embedding),
        top_k => {inner_k},
        distance_type => 'COSINE'
    )
    WHERE {base_where}
    ORDER BY distance
    LIMIT {int(top_k)}
    """
    
    # [b] BQ 失敗時降級回傳 [] 而非 raise：retrieval 為 agent 上游節點，
    #     若 raise 會炸到 UI；返回 [] 讓 self_reflect 的 coverage-driven
    #     retry plan 自然偵測 weak quarters 並重試。
    try:
        query_job = client.query(sql, job_config=job_config)
        results = query_job.result()
    except Exception as e:
        _log_bq_error("vector_search", e)
        return []

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
    # [b] 先過節流器：讓免費 Cohere key 的爆量呼叫排隊而非觸發 429（互動式
    #     少量呼叫不會 block）。節流後 rerank 才能真正運作而非被迫降級。
    _cohere_throttle.acquire()
    # [b] Cohere rerank 是「精修」步驟，非必要步驟：vector_search 已回傳按
    #     cosine 相似度排序的候選。若 Cohere 呼叫失敗（429 Trial-key 速率限制、
    #     金鑰失效、服務中斷…），絕不該讓整個 retrieve() 連帶崩潰 —— 那會使
    #     parallel_retrieval 的該子查詢結果整批丟失、季度覆蓋不足、矛盾偵測
    #     被跳過。降級為直接回傳 vector-search 原始排序的前 top_n 筆。
    try:
        resp = client.rerank(
            model="rerank-v3.5",
            query=query,
            documents=documents,
            top_n=top_n,
        )
    except Exception as e:
        # [f] 只印 type 名稱與截斷訊息，避免 Cohere 回應 body（含 trace-id）刷版
        print(
            f"[Retriever] Cohere rerank 失敗（{type(e).__name__}），"
            f"降級為 vector-search 排序：{str(e)[:120]}"
        )
        return candidates[:top_n]

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
    # [b] BQ 失敗時降級回傳 []：UI 的季度下拉選單會空，但服務不會掛。
    try:
        query_job = client.query(sql, job_config=job_config)
        results = query_job.result()
    except Exception as e:
        _log_bq_error("get_company_quarters", e)
        return []

    quarters = [row.quarter for row in results if row.quarter]
    return sorted(quarters, key=lambda x: (x[:4], x[4:]))

def retrieve_coverage(
    query: str,
    company: str,
    missing_quarters: list[str],
    top_k_per_quarter: int = 2,
    min_score: float | None = None,
    max_quarters: int = 8,
    use_rerank: bool = True,
) -> dict[str, list[dict]]:
    """
    利用 BigQuery SQL 的 PARTITION BY 一次撈取所有 missing_quarters 的 Top-K，
    簡化過去迴圈多次呼叫 SQL 的邏輯。

    Args:
        min_score: 相似度下限（cosine similarity）。
            預設 None → 讀環境變數 COVERAGE_MIN_SCORE，缺漏或非法則用
            _DEFAULT_MIN_SCORE (0.25)。顯式傳入時無視環境變數。
    """
    if not missing_quarters:
        return {}

    if min_score is None:
        min_score = _load_min_score_from_env()

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
    
    # [b] coverage sweep 失敗時降級回傳 {}：呼叫端 parallel_retrieval 會
    #     視為「補不到任何缺漏」，把已有的 main retrieval 結果交給下游 LLM。
    try:
        query_job = client.query(sql, job_config=job_config)
        results = query_job.result()
    except Exception as e:
        _log_bq_error("retrieve_coverage", e)
        return {}

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
