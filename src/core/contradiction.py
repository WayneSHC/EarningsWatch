"""
src/core/contradiction.py
Contradiction Detector — 整個系統最關鍵的模組。

選用 LLM-based 語意比對而非規則判斷：
  - 「需求強勁」vs「庫存調整」需要語境理解，if/else 無法處理
  - 「維持審慎樂觀」→「保持觀望」的微妙語氣轉變，規則抓不到
  - 回傳結構化 JSON，confidence 分數支援 Self-Reflection 評估
"""

import difflib
import os
import json
import re
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError

from src.core.llm_client import chat as llm_chat

# [f] Evidence Verifier 參數
_MIN_QUOTE_LEN = 10       # 少於此長度不做驗證（太短的字串易誤判）
_FUZZY_THRESHOLD = 0.85   # SequenceMatcher ratio 門檻（0.85 = 允許 ~15% 字元差異）
_FUZZY_SOURCE_CAP = 2000  # [c] 模糊比對時來源文本上限；batch_detect 已截斷至此，
                           #     此處重申作為 belt-and-suspenders，避免直接呼叫時無上限

# [b] 法律免責聲明 boilerplate 特徵詞（出現任一即視為無主題內容）
# coverage sweep 以 min_score=0.25 補充缺漏季度時，disclaimer 因出現在每頁
# 而輕易達到門檻，若不過濾會讓矛盾偵測比較無意義的制式文字。
#
# 中文簽名來自台股法說會逐字稿常見句型（台積電、聯發科、鴻海等）；
# 比對前會先 lower() 並移除全形空白，因此英文不分大小寫、中文不受空白影響。
_BOILERPLATE_SIGNATURES = (
    # 英文（TSMC/Apple/Nvidia 美式財報常見）
    "forward-looking statements subject to significant risks",
    "actual results may differ materially",
    "statements of its current expectations are forward-looking",
    "safe harbor",
    "private securities litigation reform act",
    # 中文（台股法說會逐字稿常見）
    "前瞻性陳述",
    "前瞻性敘述",
    "前瞻性說明",
    "實際結果可能有所不同",
    "實際結果與預期",
    "實際結果可能與",
    "風險與不確定性",
    "風險及不確定性",
    "本公司不負更新",
    "本公司無義務更新",
    "僅供參考",
    "不構成投資建議",
    "本資料所載",
)
# [c] 預先計算「移除所有空白後」的簽名，避免每次比對時重複處理
_BOILERPLATE_NORMALIZED = tuple("".join(s.split()) for s in _BOILERPLATE_SIGNATURES)


def _is_boilerplate(text: str) -> bool:
    """偵測是否為法律免責聲明 boilerplate（大小寫不敏感、忽略空白與排版差異）。

    str.split() 預設會切所有 Unicode 空白（含全形 \\u3000、tab、換行），
    join 後得到一個「無空白」字串；簽名也以相同方式預先處理過，
    因此排版差異（換行、全形空白、字元間插入空白）都不會阻擋比對。
    """
    normalized = "".join(text.lower().split())
    return any(sig in normalized for sig in _BOILERPLATE_NORMALIZED)


def _unwrap(e: BaseException) -> BaseException:
    """[b] tenacity.RetryError 包住真正的 API 錯誤，UI/log 必須看到底層原因。"""
    if isinstance(e, RetryError):
        try:
            inner = e.last_attempt.exception()
            if inner is not None:
                return inner
        except Exception:
            pass
    return e


def _verify_quote(quote: str, source_text: str) -> tuple[bool, bool]:
    """
    [f] 驗證 LLM 回傳的引文是否真實存在於來源文字中。
    防止 LLM 幻覺引文污染跨季矛盾分析結論。

    Returns:
        (verified, is_fuzzy)
        - (True, False):  精確子串匹配
        - (True, True):   模糊滑動視窗匹配（ratio ≥ _FUZZY_THRESHOLD）
        - (False, False): 無法驗證（可能幻覺）

    設計取捨：
        - 太短的引文（< _MIN_QUOTE_LEN 字元）視為通過，避免短詞誤判。
        - 模糊匹配使用滑動視窗：寬度 = quote × 1.4，步幅 = quote // 3；
          容忍標點替換、縮寫展開等小差異，但抓住完全虛構的引文。
        - 比對前先摺疊空白（換行 / 全形空格），消除排版差異。
    """
    if not quote or not source_text:
        return False, False

    # 正規化：摺疊所有空白字元（換行、tab、全形空格）
    q = " ".join(quote.strip().split())
    s = " ".join(source_text.strip().split())

    if len(q) < _MIN_QUOTE_LEN:
        return True, False  # 太短不驗證，預設通過

    # 1. 精確子串匹配（最快路徑）
    if q in s:
        return True, False

    # 2. 模糊滑動視窗匹配
    # [c] 截斷來源文本：SequenceMatcher O(n*m) 複雜度，過長的來源會放大迭代次數；
    #     batch_detect 已做 _MAX_CONTENT=2000 截斷，此處再守一道確保直接呼叫安全。
    s_capped = s[:_FUZZY_SOURCE_CAP]
    q_len = len(q)
    win_size = min(len(s_capped), int(q_len * 1.4) + 10)
    # [c] 步幅從 q_len // 3 調整為 q_len // 2：減少約 33% 迭代次數；
    #     代價是邊緣字元精度微降，但在中文財報文字的實測中影響不顯著。
    step = max(1, q_len // 2)
    for i in range(0, max(1, len(s_capped) - win_size + 2), step):
        segment = s_capped[i : i + win_size]
        if difflib.SequenceMatcher(None, q, segment).ratio() >= _FUZZY_THRESHOLD:
            return True, True

    return False, False


def _extract_json(text: str) -> dict | list:
    """
    從 LLM 回應中安全萃取 JSON，支援 dict {} 與 list [] 兩種根結構。

    [b] 三層 fallback：
      1. 直接 json.loads（最快路徑）
      2. markdown fence（```json ... ```）萃取，同時匹配 {}/[]
      3. 首尾掃描：找最早的 { 或 [，配對最後的 } 或 ]
    全部失敗時回傳預設 dict（confidence=0 讓 self_reflect 偵測到）。
    """
    # 1. 直接 parse
    try:
        result = json.loads(text.strip())
        if isinstance(result, (dict, list)):
            return result
    except json.JSONDecodeError:
        pass

    # 2. markdown fence（同時支援 {} 與 []）
    match = re.search(r'```(?:json)?\s*([{\[].*?[}\]])\s*```', text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            if isinstance(result, (dict, list)):
                return result
        except json.JSONDecodeError:
            pass

    # 3. 首尾掃描：{} 或 []，取先出現者
    # [b] rfind 確保匹配最外層的閉括號，支援巢狀結構
    b_start, b_end = text.find('{'), text.rfind('}')
    a_start, a_end = text.find('['), text.rfind(']')
    candidates: list[tuple[int, int]] = []
    if b_start != -1 and b_end > b_start:
        candidates.append((b_start, b_end))
    if a_start != -1 and a_end > a_start:
        candidates.append((a_start, a_end))
    # 取最早出現的候選（避免陣列外包物件，或物件外包陣列時選錯）
    candidates.sort(key=lambda x: x[0])
    for start, end in candidates:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, (dict, list)):
                return result
        except json.JSONDecodeError:
            continue

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


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
)
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


def _extract_sources(chunks: list[dict]) -> list[dict]:
    """[R6] 從 chunks 萃取 (file, page) 來源清單，去重，給報告做引文。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for c in chunks:
        p = c.get("payload", {}) or {}
        key = (p.get("source_file", ""), p.get("source_page", ""))
        if key in seen or not key[0]:
            continue
        seen.add(key)
        out.append({"file": key[0], "page": key[1]})
    return out


def batch_detect(
    statements_by_quarter: dict[str, list[dict]],
    topic: str,
    pair_mode: str = "adjacent",
    chunks_per_pair: int = 4,
) -> list[dict]:
    """
    對多季發言做組合比對。

    Args:
        statements_by_quarter: {"2024Q1": [chunk1, chunk2], "2024Q3": [...], ...}
        topic: 比對主題（空字串時仍可執行，LLM 會做通用比對）
        pair_mode: "adjacent"（預設，N-1 次）或 "all_pairs"（N*(N-1)/2 次，cost 平方）
        chunks_per_pair: [R5] 每季塞入 LLM 的 chunk 上限（舊版 hardcode=2，現預設=4）

    Returns:
        [{quarter_a, quarter_b, analysis, sources_a, sources_b}, ...]
        sources_a/b 為 [R6] 引文用：[{file, page}]

    容錯設計：
        - 每個季度對獨立 try/except，單一失敗不影響其餘比對
        - content 截斷至 _MAX_CONTENT 字，防止 token 超限
    """
    # [b] 防呆：topic 非字串時強制轉換
    topic = str(topic).strip() if topic else ""

    quarters = sorted(statements_by_quarter.keys())

    # 單次 LLM 呼叫的內容上限（約 500 tokens）；超過截斷，不影響語意核心
    _MAX_CONTENT = 2000

    # [R3] 依 pair_mode 產生季度對列表
    quarter_pairs: list[tuple[str, str]] = []
    if pair_mode == "all_pairs":
        for i in range(len(quarters)):
            for j in range(i + 1, len(quarters)):
                quarter_pairs.append((quarters[i], quarters[j]))
    else:  # adjacent（預設）
        for i in range(len(quarters) - 1):
            quarter_pairs.append((quarters[i], quarters[i + 1]))

    # 預先建立比對 payload（跳過空內容的對）
    pairs: list[tuple[dict, dict, str, str, list[dict], list[dict]]] = []
    for q_a, q_b in quarter_pairs:
        chunks_a = statements_by_quarter.get(q_a, [])
        chunks_b = statements_by_quarter.get(q_b, [])

        # [b] 防呆：任一季無資料則跳過，避免 IndexError
        if not chunks_a or not chunks_b:
            continue

        # [R5] 提升至 chunks_per_pair（預設 4），增加跨季比對的資訊密度
        used_a = chunks_a[:chunks_per_pair]
        used_b = chunks_b[:chunks_per_pair]
        content_a = "\n".join(c.get("payload", {}).get("content", "") for c in used_a)
        content_b = "\n".join(c.get("payload", {}).get("content", "") for c in used_b)

        # [b] 空內容防呆：兩季都無內容則無法比對，記錄後跳過
        if not content_a.strip() or not content_b.strip():
            print(f"[Contradiction] ⚠ {q_a} 或 {q_b} 內容為空，跳過比對")
            continue

        # [b] boilerplate 過濾：coverage sweep 可能以低分抓到免責聲明當作主題內容，
        #     若兩季內容都是制式 disclaimer，比對結果毫無意義，直接跳過。
        if _is_boilerplate(content_a) and _is_boilerplate(content_b):
            print(f"[Contradiction] ⚠ {q_a} vs {q_b} 兩季均為法律免責聲明，跳過比對")
            continue

        stmt_a = {
            "quarter": q_a,
            "date":    used_a[0].get("payload", {}).get("date", ""),
            "content": content_a[:_MAX_CONTENT],   # [c] 截斷，防 token 超限
        }
        stmt_b = {
            "quarter": q_b,
            "date":    used_b[0].get("payload", {}).get("date", ""),
            "content": content_b[:_MAX_CONTENT],
        }
        # [R6] 紀錄來源清單，後續報告引文
        sources_a = _extract_sources(used_a)
        sources_b = _extract_sources(used_b)
        pairs.append((stmt_a, stmt_b, q_a, q_b, sources_a, sources_b))

    def _detect_pair(args: tuple) -> dict:
        """單一季度對 LLM 偵測，供 ThreadPoolExecutor 並行呼叫。"""
        stmt_a, stmt_b, q_a, q_b, src_a, src_b = args
        try:
            analysis = detect_contradiction(stmt_a, stmt_b, topic)
        except Exception as e:
            root = _unwrap(e)
            # [b] 截斷錯誤訊息防止 API key / endpoint 洩漏到 UI
            msg = str(root)[:120]
            print(f"[Contradiction] ⚠ {q_a} vs {q_b} 偵測失敗（{type(root).__name__}: {msg}）")
            analysis = {
                "same_topic":         False,
                "stance_change":      "無關",
                "has_contradiction":  False,
                "change_detail":      f"偵測失敗：{type(root).__name__}",
                "evidence_early":     "",
                "evidence_later":     "",
                "follow_up_question": "",
                "confidence":         0.0,
            }

        # [f] Evidence Verifier：驗證 LLM 引文是否真實存在於來源 chunks。
        # LLM 可能「合理捏造」一段與原文相似但未出現的句子（hallucination）。
        # 失敗時：降低 confidence 0.2、清空該引文、標記 verification_failed=True。
        # 模糊匹配通過時：標記 evidence_{key}_fuzzy=True，報告端可加標注。
        for ev_key, src_content in (
            ("evidence_early", stmt_a["content"]),
            ("evidence_later", stmt_b["content"]),
        ):
            quote = analysis.get(ev_key, "")
            if not quote:
                continue
            verified, is_fuzzy = _verify_quote(quote, src_content)
            if not verified:
                print(
                    f"[Contradiction] ⚠ {ev_key} 引文驗證失敗（可能幻覺）"
                    f"：{quote[:60]!r}"
                )
                analysis["confidence"] = round(
                    max(0.0, analysis.get("confidence", 0.5) - 0.2), 2
                )
                analysis[ev_key] = ""
                analysis["verification_failed"] = True
            elif is_fuzzy:
                # 模糊通過：保留引文但標記，讓 UI 可顯示 "~" 提示
                analysis[f"{ev_key}_fuzzy"] = True

        return {
            "quarter_a": q_a,
            "quarter_b": q_b,
            "analysis": analysis,
            "sources_a": src_a,  # [R6]
            "sources_b": src_b,  # [R6]
        }

    if not pairs:
        return []

    # [c] 並行 LLM 呼叫：LLM API 為 I/O-bound，ThreadPoolExecutor 可有效縮短總等待時間。
    # [b] max_workers=2：Gemini 免費層 RPM 緊（2.5-flash 約 10 RPM），加上 decomposer/judge
    #     /tool router 等其他 LLM 呼叫，並行度太高會立刻觸發 429。可由環境變數覆寫。
    _workers = min(len(pairs), int(os.getenv("LLM_PAIR_WORKERS", "2")))
    raw_results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futures = {pool.submit(_detect_pair, p): idx for idx, p in enumerate(pairs)}
        for fut in as_completed(futures):
            idx = futures[fut]
            raw_results[idx] = fut.result()

    # 按原始順序（時間序）排列結果
    return [raw_results[i] for i in sorted(raw_results)]


def detect_promises(chunks_by_quarter: dict[str, list[dict]], topic: str) -> list[dict]:
    """
    承諾兌現追蹤：找出前一季的「承諾」，判斷後一季是否兌現。

    Returns:
        [{"promise_quarter", "content", "status": "✅達標/❌未兌現/⚠不明", "detail"}, ...]
    """
    quarters = sorted(chunks_by_quarter.keys())

    # [c] 預先建立任務清單，後續以 ThreadPoolExecutor 並行送出 LLM 請求
    tasks: list[tuple[str, str, str]] = []  # (q_prev, q_next, prompt)
    for i in range(len(quarters) - 1):
        q_prev, q_next = quarters[i], quarters[i + 1]
        chunks_prev = chunks_by_quarter.get(q_prev, [])
        chunks_next = chunks_by_quarter.get(q_next, [])

        if not chunks_prev:
            continue
        combined_prev = "\n\n".join(
            c.get("payload", {}).get("content", "")
            for c in chunks_prev[:3]
        )[:2000]
        combined_next = "\n\n".join(
            c.get("payload", {}).get("content", "")
            for c in chunks_next[:2]
        )[:1500]

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
        tasks.append((q_prev, q_next, prompt))

    if not tasks:
        return []

    def _run_promise(args: tuple[str, str, str]) -> dict | None:
        q_prev, q_next, prompt = args
        try:
            raw = llm_chat(prompt, max_tokens=300)
            result = _extract_json(raw)
            if not result.get("has_promise"):
                return None
            status_emoji = {"達標": "✅", "未兌現": "❌", "不明": "⚠"}.get(
                result.get("status", "不明"), "⚠"
            )
            return {
                "promise_quarter": q_prev,
                "followup_quarter": q_next,
                "content": result.get("promise_summary", ""),
                "status": f"{status_emoji} {result.get('status', '不明')}",
                "detail": result.get("detail", ""),
                "confidence": result.get("confidence", 0.5),
                "_idx": None,  # 由呼叫端設定
            }
        except Exception as e:
            root = _unwrap(e)
            print(f"[Contradiction] 承諾分析失敗 {q_prev}->{q_next}: {type(root).__name__}: {str(root)[:120]}")
            return None

    # [c] 並行 LLM 呼叫（與 batch_detect 一致），保守 max_workers 避免 Gemini 免費層 RPM
    _workers = min(len(tasks), int(os.getenv("LLM_PAIR_WORKERS", "2")))
    indexed: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=_workers) as pool:
        futures = {pool.submit(_run_promise, t): idx for idx, t in enumerate(tasks)}
        for fut in as_completed(futures):
            idx = futures[fut]
            res = fut.result()
            if res is not None:
                indexed[idx] = res

    return [indexed[i] for i in sorted(indexed)]
