"""
src/core/ragas_eval.py
RAGAS 評估的最小封裝：把 Agent 跑出來的 (question, contexts, answer) 餵給 RAGAS。

設計原則：
  - 可選依賴：未安裝 ragas / langchain-openai 時，is_available() 回 False，
    benchmark 自動跳過此 metric，不影響其他測試。
  - 不污染 chat() 流程：RAGAS 自己的 LLM 呼叫走 langchain-openai，與本專案
    的 llm_client cascade 是兩條獨立軌道。原因是 RAGAS 內部已綁 langchain
    BaseChatModel 介面，重新實作成本不划算。
  - 預設使用 OpenAI（OPENAI_API_KEY），因為 RAGAS 對 GPT-4o 級模型的 prompt
    經過調校，換 Gemini/Cohere 評分穩定性會明顯下降。

評估指標說明：
  - faithfulness:        答案是否忠於 retrieved contexts（LLM 幻覺率反指標）
  - answer_relevancy:    答案是否切題（語意相關性）
  - context_precision:   retrieved chunks 對問題的精準度（雜訊比例反指標）
  - context_recall:      retrieved chunks 是否涵蓋 ground truth（需提供答案）

對應到本專案的指標目標：
  - faithfulness ≥ 0.85  → 對應 benchmark 「幻覺率 ≤ 5%」
  - answer_relevancy ≥ 0.80
  - context_precision ≥ 0.70
"""

from __future__ import annotations

import os
from typing import Any, Optional


def is_available() -> bool:
    """[b] 偵測 ragas 與 langchain-openai 是否都已安裝。"""
    try:
        import ragas  # noqa: F401
        import langchain_openai  # noqa: F401
        return True
    except ImportError:
        return False


def _env_int(name: str, default: int) -> int:
    """[b] 讀正整數環境變數；非數字 / 非正數一律回 default 並警告，不讓設定錯誤炸掉評估。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        print(f"[RAGAS] ⚠ {name}={raw!r} 不是整數，改用預設值 {default}")
        return default
    if val <= 0:
        print(f"[RAGAS] ⚠ {name}={val} 必須為正整數，改用預設值 {default}")
        return default
    return val


def _ensure_openai_key() -> bool:
    """
    [b] 確保 os.environ["OPENAI_API_KEY"] 是「真的能用」的金鑰。

    RAGAS 直接讀 os.environ，不走本專案的 get_secret() cascade。當金鑰只存在
    GCP Secret Manager、而 .env 把該行寫成註解時（例如
    `OPENAI_API_KEY=# → GCP Secret Manager 管理`），dotenv 會把註解字串當成值
    載入。那個值非空、能通過 `if not os.getenv(...)` 檢查，卻含非 ASCII 字元，
    最後在 httpx 組 Authorization header 時炸成
    `UnicodeEncodeError: 'ascii' codec can't encode character '\\u2192'`——
    每一次評分呼叫都失敗、metric 全部回 nan，但表面上看起來只是分數難看。

    因此這裡不只檢查「有沒有值」，而是檢查「像不像金鑰」（ASCII + sk- 前綴）；
    不合格就回頭跟 Secret Manager 要，並覆寫 os.environ。注意不能用
    bridge_to_env()——它只在變數為空時才寫入，救不了「被註解字串佔位」這種情況。
    """
    def _usable(value: str) -> bool:
        # 不檢查 `sk-` 前綴：OpenAI 金鑰格式會變，寫死前綴日後會誤殺合法金鑰。
        # 這裡只擋兩種「一定不能送進 HTTP header」的值：非 ASCII（httpx 會炸
        # UnicodeEncodeError）、以 `#` 開頭（.env 註解被當成值載入）。
        return bool(value) and value.isascii() and not value.startswith("#")

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if _usable(key):
        return True

    try:
        from src.core.secrets import get_secret
        fetched = (get_secret("OPENAI_API_KEY") or "").strip()
    except Exception:
        fetched = ""

    if _usable(fetched):
        os.environ["OPENAI_API_KEY"] = fetched
        return True

    return False


def _build_judge():
    """
    [b] 建立 RAGAS 的評審 LLM 與 embedding，兩者都不能依賴 RAGAS 的預設值。

    RAGAS 預設評審是 `gpt-4o-mini`、預設 embedding 是 OpenAI text-embedding-*。
    本專案的 OpenAI 專案金鑰兩者都沒有存取權（403 model_not_found），而 RAGAS
    只會把 403 記成「該題評分失敗」→ 回填 NaN，不會中止流程；表面上像是模型
    表現差，實際上一次評分都沒發生。因此這裡明確指定可用的模型：

      - 評審 LLM：`RAGAS_JUDGE_MODEL`（預設 gpt-4.1）。本帳號無 gpt-4o 系列，
        gpt-4.1 是同級距且非 reasoning 的通用模型，最接近 RAGAS prompt 的調校前提。
      - Embedding：沿用本專案既有的 Vertex `gemini-embedding-2`（answer_relevancy
        需要 embedding 算語意相似度）。注意它只用於「原問題 ↔ 反推問題」的相似度，
        不參與檢索評分，所以不會造成用自己的檢索器評自己的檢索結果。

    回傳 (llm, embeddings)，交給 evaluate() 覆寫預設值。
    """
    from langchain_core.embeddings import Embeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    provider = (os.getenv("RAGAS_JUDGE_PROVIDER", "openai").strip().lower()
                or "openai")
    _DEFAULT_MODEL = {
        "openai":    "gpt-4.1",
        "anthropic": "claude-sonnet-4-6",
    }
    judge_model = (os.getenv("RAGAS_JUDGE_MODEL", "").strip()
                   or _DEFAULT_MODEL.get(provider, "gpt-4.1"))

    class _ProjectEmbeddings(Embeddings):
        """把本專案的 embed_query_texts 包成 LangChain Embeddings 介面。"""

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            from src.ingestion.embedder import embed_query_texts
            return embed_query_texts(texts)

        def embed_query(self, text: str) -> list[float]:
            return self.embed_documents([text])[0]

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        from src.core.secrets import get_secret
        api_key = (get_secret("ANTHROPIC_API_KEY")
                   or os.getenv("ANTHROPIC_API_KEY", "")).strip()
        # [b] max_tokens 太小會讓 faithfulness 的 statement 拆解被截斷 → 解析失敗
        # → NaN。這裡給足空間，成本由 RAGAS_MAX_CONTEXTS 控制而非靠截斷輸出。
        chat_model = ChatAnthropic(
            model=judge_model, temperature=0, max_tokens=4096, api_key=api_key,
        )
    else:
        from langchain_openai import ChatOpenAI
        chat_model = ChatOpenAI(model=judge_model, temperature=0)

    llm = LangchainLLMWrapper(chat_model)
    embeddings = LangchainEmbeddingsWrapper(_ProjectEmbeddings())
    return llm, embeddings


def evaluate_query(
    question: str,
    answer: str,
    contexts: list[str],
    ground_truth: Optional[str] = None,
    metrics: Optional[list[str]] = None,
) -> dict[str, float]:
    """
    對單一 (question, answer, contexts) 三元組跑 RAGAS。

    Args:
        question:    使用者原始問題
        answer:      Agent 最終回答（final_report）
        contexts:    retrieved chunks 的內容字串列表
        ground_truth: 標準答案；提供時可計算 context_recall
        metrics:     要跑的 metric 名稱清單；None = 全部可用的

    Returns:
        {metric_name: float, ...}；若 ragas 未安裝或評估失敗，回 {}（不 raise）。

    執行成本提醒：
        每個 metric ≈ 1 次 LLM 呼叫（GPT-4o）。30 題 × 4 metric ≈ 120 次呼叫，
        以 GPT-4o 定價估約 $0.50–1.50 USD。建議先用小樣本 (--ragas-sample 5)
        驗證流程，再跑全集。
    """
    if not is_available():
        return {}

    if not contexts:
        # 沒有 retrieved 內容時跳過：所有 metric 都會失敗或回 0
        return {}

    # [b] 只有走 OpenAI 評審時才需要 OPENAI_API_KEY；改用其他 provider 時
    # 硬性要求 OpenAI 金鑰會把本來可以跑的評估擋掉。
    if os.getenv("RAGAS_JUDGE_PROVIDER", "openai").strip().lower() == "openai":
        if not _ensure_openai_key():
            print("[RAGAS] ⚠ 找不到可用的 OPENAI_API_KEY，跳過 RAGAS 評估")
            return {}

    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import (
            faithfulness,
            answer_relevancy,
            LLMContextPrecisionWithoutReference,
        )

        # [b] RAGAS 0.4 起 `context_precision` 預設為 with-reference 變體，缺
        # `reference` 欄位會讓整批 evaluate() 直接 raise ValueError —— 連
        # faithfulness / answer_relevancy 一起賠掉。本專案沒有人工標註的黃金答案
        # （benchmark 傳入的 ground_truth 只是測項描述，不是參考答案），拿它當
        # reference 會產生沒有意義的分數。改用 without-reference 變體：以 answer
        # 為基準判斷每個 retrieved chunk 是否有貢獻，不需要 ground truth。
        available_metrics = {
            "faithfulness": faithfulness,
            "answer_relevancy": answer_relevancy,
            "context_precision": LLMContextPrecisionWithoutReference(),
        }

        # [b] context_recall 需要真正的參考答案。測項描述不足以充當 reference，
        # 分數不可信，故不納入；要啟用需先建立人工標註的黃金答案集。

        wanted = metrics or list(available_metrics.keys())
        selected = [available_metrics[m] for m in wanted if m in available_metrics]
        if not selected:
            return {}

        # [c] context_precision 會對「每個 context」各發一次 LLM 呼叫，成本與
        # 延遲和 context 數成正比。實測一題約 30 條；這裡設上限避免偶發的超大
        # 檢索結果讓單題成本失控。取前 N 條（檢索已按分數排序），等同 precision@N，
        # 回報分數時需一併說明 N。
        max_ctx = _env_int("RAGAS_MAX_CONTEXTS", 30)
        contexts = contexts[:max_ctx]

        row: dict[str, Any] = {
            "question":    [question],
            "answer":      [answer],
            "contexts":    [contexts],
        }

        dataset = Dataset.from_dict(row)
        judge_llm, judge_embeddings = _build_judge()

        # [b] RAGAS 預設單一 job timeout 是 180s。實測一題約 30 個 context，
        # context_precision 會對「每個 context」各發一次 LLM 呼叫，faithfulness
        # 還要先把答案拆成 statement 再逐條比對——Sonnet 等級的評審跑不完 180s，
        # 會拋 TimeoutError 並把該 metric 填成 NaN。結果是分數欄位悄悄消失，
        # 看起來像「模型沒拿到分」而不是「評分沒跑完」。放寬到可設定的上限。
        from ragas.run_config import RunConfig
        run_config = RunConfig(
            timeout=_env_int("RAGAS_TIMEOUT_SEC", 900),
            max_workers=_env_int("RAGAS_MAX_WORKERS", 4),
        )

        result = evaluate(
            dataset,
            metrics=selected,
            llm=judge_llm,
            embeddings=judge_embeddings,
            run_config=run_config,
        )

        # RAGAS 回傳 EvaluationResult；以 to_pandas 取單列 dict 最穩
        df = result.to_pandas()
        if df.empty:
            return {}

        # [b] RAGAS 輸出的欄位名是 metric 類別名，不等於我們要的對外名稱：
        # LLMContextPrecisionWithoutReference 會輸出
        # `llm_context_precision_without_reference`。若沿用「欄名必須等於或
        # 前綴符合 available_metrics key」的過濾，這個欄位會被整個丟掉，
        # 結果是 context_precision 永遠缺席卻不報錯。這裡明確做一次正規化。
        alias = {
            "llm_context_precision_without_reference": "context_precision",
            "llm_context_precision_with_reference":    "context_precision",
        }
        wanted_names = set(available_metrics.keys())

        scores: dict[str, float] = {}
        for col in df.columns:
            name = alias.get(col, col)
            if name not in wanted_names:
                continue
            try:
                val = float(df[col].iloc[0])
            except (TypeError, ValueError):
                continue
            # [b] 呼叫失敗時 RAGAS 以 NaN 填值。NaN 混進平均會污染整份報告，
            # 且 NaN != NaN 不會被任何比較擋下，必須在這裡剔除。
            if val != val:
                continue
            scores[name] = val

        return scores

    except Exception as e:
        # [b] RAGAS 內部任何錯誤（API 失敗、解析失敗）都不該炸掉 benchmark
        print(f"[RAGAS] ⚠ 評估失敗：{type(e).__name__}: {str(e)[:120]}")
        return {}


def aggregate(per_query_scores: list[dict[str, float]]) -> dict[str, float]:
    """把多個 per-query metric dict 平均，回傳 {metric: mean_score}。"""
    if not per_query_scores:
        return {}

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for scores in per_query_scores:
        for k, v in scores.items():
            sums[k] = sums.get(k, 0.0) + v
            counts[k] = counts.get(k, 0) + 1

    return {k: round(sums[k] / counts[k], 4) for k in sums}


def state_to_contexts(state: dict) -> list[str]:
    """從 AgentState.retrieved 抽出 chunk content list，供 evaluate_query 使用。

    [b] retriever 產出的 chunk 形狀是 {"id", "score", "payload": {"content", ...}}
    ——內容在 payload 底下，不在頂層。原本只讀 `c["content"]` 會永遠取到 None，
    於是 contexts 恆為空；benchmark 的 `if ctxs and report:` 直接跳過評分，
    整份 RAGAS 靜默不執行（ragas_aggregate = None），而且不會有任何錯誤訊息。
    這裡以 payload 為主、頂層為備援，兩種形狀都能吃。
    """
    contexts: list[str] = []
    for _, chunks in (state.get("retrieved") or {}).items():
        for c in chunks or []:
            if not isinstance(c, dict):
                continue
            payload = c.get("payload")
            content = ""
            if isinstance(payload, dict):
                content = (payload.get("content") or "").strip()
            if not content:
                content = (c.get("content") or "").strip()
            if content:
                contexts.append(content)
    return contexts
