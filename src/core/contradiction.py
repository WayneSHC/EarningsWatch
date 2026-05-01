"""
src/core/contradiction.py
Contradiction Detector — 整個系統最關鍵的模組。

選用 LLM-based 語意比對而非規則判斷：
  - 「需求強勁」vs「庫存調整」需要語境理解，if/else 無法處理
  - 「維持審慎樂觀」→「保持觀望」的微妙語氣轉變，規則抓不到
  - 回傳結構化 JSON，confidence 分數支援 Self-Reflection 評估
"""

import os
import json
import re
from typing import Any
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.llm_client import chat as llm_chat


def _extract_json(text: str) -> dict:
    """從 LLM 回應中安全萃取 JSON。"""
    # 嘗試直接 parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # 嘗試從 ```json ... ``` 區塊萃取
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # 掃描第一個 '{' 到最後一個 '}'，支援巢狀結構
    # [b] r'\{[^{}]*\}' 只能匹配非巢狀 JSON，改用首尾掃描
    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    # 全部失敗，記錄警告並回傳預設值（confidence=0 讓 self_reflect 可偵測）
    print(f"[Contradiction] ⚠ JSON 解析失敗，LLM 原始回應（前200字）：{text[:200]!r}")
    return {
        "same_topic": False,
        "stance_change": "無關",
        "has_contradiction": False,
        "change_detail": "LLM 回應解析失敗，請重試",
        "evidence_early": "",
        "evidence_later": "",
        "follow_up_question": "",
        "confidence": 0.0,
    }


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def detect_contradiction(
    stmt_a: dict,
    stmt_b: dict,
    topic: str,
) -> dict:
    """
    核心函數：比對兩季發言的語意一致性。

    Args:
        stmt_a: {"quarter": "2024Q1", "date": "2024-01-18", "content": "..."}
        stmt_b: {"quarter": "2024Q3", "date": "2024-10-17", "content": "..."}
        topic: 比對主題（如 "AI需求"、"毛利率"）

    Returns:
        {
          "same_topic": bool,           # 兩段是否真的在討論同一主題
          "stance_change": str,         # 更樂觀/更保守/維持不變/無關
          "has_contradiction": bool,    # 是否存在矛盾
          "change_detail": str,         # 具體改變說明（50字以內）
          "evidence_early": str,        # 較早季度關鍵語句（原文引用）
          "evidence_later": str,        # 較晚季度關鍵語句（原文引用）
          "follow_up_question": str,    # 建議投資人追問的問題
          "confidence": float,          # 0.0 ~ 1.0
        }
    """
    # [b] 防呆：確保傳入值為 dict，避免呼叫方誤傳 None
    if not isinstance(stmt_a, dict) or not isinstance(stmt_b, dict):
        raise ValueError(f"stmt_a/stmt_b 必須為 dict，收到 {type(stmt_a)}/{type(stmt_b)}")

    # 確保 stmt_a 是較早的季度（字典序排序：2024Q1 < 2024Q3）
    if stmt_a.get("quarter", "") > stmt_b.get("quarter", ""):
        stmt_a, stmt_b = stmt_b, stmt_a

    prompt = f"""你是一位嚴謹的財務分析師，專門追蹤上市公司管理層的發言一致性。

以下是同一公司針對「{topic}」的兩個不同季度發言：

【{stmt_a["quarter"]}】（{stmt_a.get("date", "")}）：
{stmt_a["content"]}

【{stmt_b["quarter"]}】（{stmt_b.get("date", "")}）：
{stmt_b["content"]}

請分析並只回傳以下 JSON（不要有任何其他文字）：
{{
  "same_topic": true或false（兩段是否真的在討論「{topic}」這個主題）,
  "stance_change": "更樂觀"或"更保守"或"維持不變"或"無關",
  "has_contradiction": true或false（立場是否有實質矛盾或前後不一致）,
  "change_detail": "具體說明改變了什麼，50字以內",
  "evidence_early": "直接引用較早季度的關鍵語句",
  "evidence_later": "直接引用較晚季度的關鍵語句",
  "follow_up_question": "建議散戶投資人或分析師追問管理層的一個具體問題",
  "confidence": 0到1之間的數字（你對本次分析結論的信心度）
}}"""

    raw = llm_chat(prompt, max_tokens=600)
    return _extract_json(raw)


def batch_detect(
    statements_by_quarter: dict[str, list[dict]],
    topic: str,
) -> list[dict]:
    """
    對多季發言做全組合比對。

    Args:
        statements_by_quarter: {"2024Q1": [chunk1, chunk2], "2024Q3": [...], ...}
        topic: 比對主題（空字串時仍可執行，LLM 會做通用比對）

    Returns:
        [{quarter_a, quarter_b, analysis (detect_contradiction 結果)}, ...]

    容錯設計：
        - 每個季度對獨立 try/except，單一失敗不影響其餘比對
        - content 截斷至 _MAX_CONTENT 字，防止 token 超限
        - payload 存取使用 .get() 防呆，避免 KeyError
    """
    # [b] 防呆：topic 非字串時強制轉換
    topic = str(topic).strip() if topic else ""

    quarters = sorted(statements_by_quarter.keys())
    results = []

    # 單次 LLM 呼叫的內容上限（約 500 tokens）；超過截斷，不影響語意核心
    _MAX_CONTENT = 2000

    # 取相鄰季度比對（避免 N² 組合爆炸）
    for i in range(len(quarters) - 1):
        q_a, q_b = quarters[i], quarters[i + 1]
        chunks_a = statements_by_quarter.get(q_a, [])
        chunks_b = statements_by_quarter.get(q_b, [])

        # [b] 防呆：任一季無資料則跳過，避免 IndexError
        if not chunks_a or not chunks_b:
            continue

        # 各季取最相關的前 2 個 chunk 合併（已由 retriever 依分數排序）
        # [b] .get("payload", {}) 防呆：chunk 結構異常時不 crash
        content_a = "\n".join(
            c.get("payload", {}).get("content", "") for c in chunks_a[:2]
        )
        content_b = "\n".join(
            c.get("payload", {}).get("content", "") for c in chunks_b[:2]
        )

        # [b] 空內容防呆：兩季都無內容則無法比對，記錄後跳過
        if not content_a.strip() or not content_b.strip():
            print(f"[Contradiction] ⚠ {q_a} 或 {q_b} 內容為空，跳過比對")
            continue

        stmt_a = {
            "quarter": q_a,
            "date":    chunks_a[0].get("payload", {}).get("date", ""),
            "content": content_a[:_MAX_CONTENT],   # [c] 截斷，防 token 超限
        }
        stmt_b = {
            "quarter": q_b,
            "date":    chunks_b[0].get("payload", {}).get("date", ""),
            "content": content_b[:_MAX_CONTENT],
        }

        # [b] 獨立 try/except：單一季度對失敗不中止整批偵測
        try:
            analysis = detect_contradiction(stmt_a, stmt_b, topic)
        except Exception as e:
            print(f"[Contradiction] ⚠ {q_a} vs {q_b} 偵測失敗（{type(e).__name__}: {e}）")
            # 回傳「信心度 0」的降級結果，讓 self_reflect 能偵測到品質問題
            analysis = {
                "same_topic":         False,
                "stance_change":      "無關",
                "has_contradiction":  False,
                "change_detail":      f"偵測失敗：{type(e).__name__}",
                "evidence_early":     "",
                "evidence_later":     "",
                "follow_up_question": "",
                "confidence":         0.0,
            }

        results.append({
            "quarter_a": q_a,
            "quarter_b": q_b,
            "analysis":  analysis,
        })

    return results


def detect_promises(chunks_by_quarter: dict[str, list[dict]], topic: str) -> list[dict]:
    """
    承諾兌現追蹤：找出前一季的「承諾」，判斷後一季是否兌現。

    Returns:
        [{"promise_quarter", "content", "status": "✅達標/❌未兌現/⚠不明", "detail"}, ...]
    """
    quarters = sorted(chunks_by_quarter.keys())
    promises = []

    for i in range(len(quarters) - 1):
        q_prev, q_next = quarters[i], quarters[i + 1]
        chunks_prev = chunks_by_quarter.get(q_prev, [])
        chunks_next = chunks_by_quarter.get(q_next, [])

        # 取前 3 個最相關 chunk 合併成一段，讓 LLM 判斷是否含前瞻承諾
        # 不再依賴 contains_guidance flag（英文 transcript 常被漏標）
        if not chunks_prev:
            continue
        combined_prev = "\n\n".join(
            c.get("payload", {}).get("content", "")
            for c in chunks_prev[:3]
        )
        combined_next = "\n\n".join(
            c.get("payload", {}).get("content", "")
            for c in chunks_next[:2]
        )
        # [c] 截斷：與 batch_detect 保持一致，防止 token 超限
        combined_prev = combined_prev[:2000]
        combined_next = combined_next[:1500]

        # 用 LLM 判斷承諾兌現
        prompt = f"""以下是 {q_prev} 季度的管理層發言（可能含前瞻指引）：
{combined_prev}

以下是 {q_next} 季度的後續說明：
{combined_next}

請只回傳 JSON：
{{
  "has_promise": true或false（{q_prev} 是否有具體可追蹤的前瞻承諾）,
  "promise_summary": "承諾摘要（20字以內）",
  "status": "達標"或"未兌現"或"不明"（根據 {q_next} 季度的資訊判斷）,
  "detail": "判斷說明（30字以內）",
  "confidence": 0到1
}}"""

        try:
            raw = llm_chat(prompt, max_tokens=300)
            result = _extract_json(raw)
            if result.get("has_promise"):
                # [b] 用 .get() 防呆：LLM 有時不回傳 status 欄位
                status_emoji = {"達標": "✅", "未兌現": "❌", "不明": "⚠"}.get(
                    result.get("status", "不明"), "⚠"
                )
                promises.append({
                    "promise_quarter": q_prev,
                    "followup_quarter": q_next,
                    "content": result.get("promise_summary", ""),
                    "status": f"{status_emoji} {result['status']}",
                    "detail": result.get("detail", ""),
                    "confidence": result.get("confidence", 0.5),
                })
        except Exception as e:
            print(f"[Contradiction] 承諾分析失敗: {e}")
            continue

    return promises
