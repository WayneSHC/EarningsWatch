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

    if not os.getenv("OPENAI_API_KEY", "").strip():
        # RAGAS 預設用 OpenAI；缺 key 直接跳過比讓 RAGAS 內部報錯清晰
        print("[RAGAS] ⚠ 找不到 OPENAI_API_KEY，跳過 RAGAS 評估")
        return {}

    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import (
            faithfulness,
            answer_relevancy,
            context_precision,
        )

        available_metrics = {
            "faithfulness": faithfulness,
            "answer_relevancy": answer_relevancy,
            "context_precision": context_precision,
        }

        # context_recall 需要 ground_truth；按需匯入避免 ImportError
        if ground_truth:
            try:
                from ragas.metrics import context_recall
                available_metrics["context_recall"] = context_recall
            except ImportError:
                pass

        wanted = metrics or list(available_metrics.keys())
        selected = [available_metrics[m] for m in wanted if m in available_metrics]
        if not selected:
            return {}

        row: dict[str, Any] = {
            "question":    [question],
            "answer":      [answer],
            "contexts":    [contexts],
        }
        if ground_truth:
            row["ground_truth"] = [ground_truth]

        dataset = Dataset.from_dict(row)
        result = evaluate(dataset, metrics=selected)

        # RAGAS 回傳 EvaluationResult；以 to_pandas 取單列 dict 最穩
        df = result.to_pandas()
        if df.empty:
            return {}
        scores = {}
        for col in df.columns:
            val = df[col].iloc[0]
            # 只保留有意義的 numeric metric 欄位
            try:
                scores[col] = float(val)
            except (TypeError, ValueError):
                continue
        # 過濾掉非 metric 欄位（question / answer / contexts 等）
        return {
            k: v for k, v in scores.items()
            if k in available_metrics or k.startswith(tuple(available_metrics.keys()))
        }

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
    """從 AgentState.retrieved 抽出 chunk content list，供 evaluate_query 使用。"""
    contexts: list[str] = []
    for _, chunks in (state.get("retrieved") or {}).items():
        for c in chunks:
            content = c.get("content") if isinstance(c, dict) else None
            if content:
                contexts.append(content)
    return contexts
