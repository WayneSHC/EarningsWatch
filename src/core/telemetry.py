"""
src/core/telemetry.py
Token / cost / latency 觀測。

設計目標：
  - 不改 chat() 對外介面：呼叫端不需要知道 telemetry 存在
  - Thread-safe：多公司並行（ThreadPoolExecutor max_workers=2）會同時寫入
  - 零依賴：只用 stdlib，不引入 prometheus / opentelemetry 等大型套件
  - 成本估算：用各家 2026-05 公開定價，缺定價的 model 回 0（不阻擋寫入）

UI 顯示：app.py sidebar 呼叫 summary() 取得本次 session 累積數據。
RAGAS 整合：benchmark.py 在每組題目跑完後 reset() 並 summary()，得到
            per-question token cost。
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Optional


# ── [c] 各家公開定價（USD per 1M tokens；2026-05 參考值）─────────────────
# 來源：OpenAI Pricing、Google AI Studio、Cohere Pricing 公開頁面。
# 價格會變動，這只是估算值；缺項時 estimate_cost() 回 0（graceful degrade）。
#
# [b] 2026-05-08 修正：先前用 `gpt-5o` / `gemini-3.0-flash` 是錯誤模型名（API 回 404），
#     已校正成 `gpt-5` / `gpt-5-mini` / `gemini-2.5-flash`。
#     舊鍵保留無害（缺項會被忽略），但若 BACKEND_MODELS 仍意外吐回舊名也不會匹配。
_PRICING: dict[tuple[str, str], tuple[float, float]] = {
    # (backend, model): (input_per_1m_usd, output_per_1m_usd)
    # ── 現役主力（2026-05 校正後）──
    ("openai", "gpt-5"):                    (5.00, 15.00),
    ("openai", "gpt-5-mini"):               (0.25,  1.25),
    ("openai", "gpt-4.1"):                  (2.00,  8.00),
    ("openai", "gpt-4.1-mini"):             (0.40,  1.60),
    ("openai", "gpt-4o"):                   (2.50, 10.00),
    ("openai", "gpt-4o-mini"):              (0.15,  0.60),
    ("gemini", "gemini-2.5-flash"):         (0.075, 0.30),
    ("gemini", "gemini-2.5-pro"):           (1.25,  5.00),
    ("gemini", "gemini-2.0-flash"):         (0.075, 0.30),
    ("cohere", "command-r-plus-08-2024"):   (2.50, 10.00),
    ("cohere", "command-r7b-12-2024"):      (0.0375, 0.15),
}


@dataclass(slots=True)
class LLMCall:
    """單次 LLM 呼叫的觀測記錄。"""
    backend: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: float = 0.0
    cost_usd: float = 0.0
    error: Optional[str] = None


def estimate_cost(
    backend: str, model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    """依 _PRICING 表估算 USD 成本，缺定價時回 0。"""
    rate = _PRICING.get((backend, model))
    if not rate:
        return 0.0
    inp_rate, out_rate = rate
    return (prompt_tokens / 1_000_000) * inp_rate + (completion_tokens / 1_000_000) * out_rate


class _Registry:
    """[c] thread-safe singleton：所有 chat() 呼叫共用同一份累積資料。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._calls: list[LLMCall] = []

    def record(self, call: LLMCall) -> None:
        with self._lock:
            self._calls.append(call)

    def summary(self) -> dict:
        """彙整目前所有記錄，回傳給 UI / benchmark 使用。"""
        with self._lock:
            calls = list(self._calls)

        total_calls = len(calls)
        ok = sum(1 for c in calls if c.error is None)
        prompt = sum(c.prompt_tokens for c in calls)
        completion = sum(c.completion_tokens for c in calls)
        cost = sum(c.cost_usd for c in calls)
        duration = sum(c.duration_ms for c in calls)

        by_backend: dict[str, dict] = {}
        for c in calls:
            d = by_backend.setdefault(
                c.backend, {"calls": 0, "tokens": 0, "cost_usd": 0.0}
            )
            d["calls"] += 1
            d["tokens"] += c.prompt_tokens + c.completion_tokens
            d["cost_usd"] += c.cost_usd

        return {
            "total_calls": total_calls,
            "successful_calls": ok,
            "failed_calls": total_calls - ok,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "estimated_cost_usd": round(cost, 6),
            "total_duration_ms": round(duration, 1),
            "by_backend": by_backend,
        }

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()

    def calls(self) -> list[LLMCall]:
        with self._lock:
            return list(self._calls)


_registry = _Registry()


def record(call: LLMCall) -> None:
    """記錄一次 LLM 呼叫；llm_client.chat() 內部使用。"""
    _registry.record(call)


def summary() -> dict:
    """取得目前累積的 telemetry 數據；UI / benchmark 使用。"""
    return _registry.summary()


def reset() -> None:
    """清空累積資料；通常在每個新 query 開始前呼叫，避免跨 query 累加。"""
    _registry.reset()


def calls() -> list[LLMCall]:
    """取得所有原始記錄；debug / RAGAS per-question 分析用。"""
    return _registry.calls()
