"""
tests/benchmark.py
30 題 Benchmark 測試集 — 量化驗證五項核心指標

測試類型：
  A. 矛盾偵測正確率（目標 ≥ 80%）— 20 題
  B. 幻覺偵測率（目標 ≤ 5% 幻覺）— 5 題
  C. 來源引用率（目標 ≥ 90%）— 抽查
  D. Self-Reflection 觸發率（目標 ≥ 30%）— 抽查
  E. 承諾追蹤正確率（目標 ≥ 75%）— 嵌入矛盾測試中評估

使用方式：
    python tests/benchmark.py                        # 執行全部測試
    python tests/benchmark.py --type contradiction   # 只跑矛盾偵測（最快）
    python tests/benchmark.py --type hallucination   # 只跑幻覺測試
    python tests/benchmark.py --type citation        # 只跑來源引用率
    python tests/benchmark.py --type reflection      # 只跑 Self-Reflection 觸發率
    python tests/benchmark.py --output my_report.json  # 自訂報告路徑
"""

import os
import re
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))


# ══════════════════════════════════════════════════════════════════════════════
# A. 矛盾偵測測試集（20 題）
#    expected_contradiction: True  → 跨季確實有立場轉變
#                            False → 立場一致，不應偵測為矛盾
# ══════════════════════════════════════════════════════════════════════════════
CONTRADICTION_TESTS = [
    # ── AI 需求主題（台積電）─────────────────────────────────────────────────
    {
        "id": "C001",
        "company": "台積電", "topic": "AI需求",
        "quarters": ["2023Q2", "2023Q3"],
        "expected_contradiction": False,
        "description": "AI需求強勁，連續兩季樂觀，應一致",
    },
    {
        "id": "C002",
        "company": "台積電", "topic": "AI需求",
        "quarters": ["2024Q1", "2024Q2"],
        "expected_contradiction": True,
        "description": "Q1強調AI爆發 → Q2提CoWoS產能未完全轉換，立場有落差",
    },
    {
        "id": "C003",
        "company": "台積電", "topic": "AI需求",
        "quarters": ["2024Q3", "2025Q1"],
        "expected_contradiction": False,
        "description": "持續強調AI伺服器需求強勁，立場一致",
    },
    {
        "id": "C004",
        "company": "台積電", "topic": "AI需求",
        "quarters": ["2025Q1", "2025Q2"],
        "expected_contradiction": False,
        "description": "AI需求主軸未改變，連貫性高",
    },
    # ── 毛利率主題（台積電）──────────────────────────────────────────────────
    {
        "id": "C005",
        "company": "台積電", "topic": "毛利率",
        "quarters": ["2023Q3", "2023Q4"],
        "expected_contradiction": False,
        "description": "毛利率指引穩定維持在 53% 目標附近",
    },
    {
        "id": "C006",
        "company": "台積電", "topic": "毛利率",
        "quarters": ["2023Q4", "2024Q2"],
        "expected_contradiction": True,
        "description": "N3良率改善帶動毛利率上修，前後預估有明顯差距",
    },
    {
        "id": "C007",
        "company": "台積電", "topic": "毛利率",
        "quarters": ["2024Q3", "2025Q1"],
        "expected_contradiction": False,
        "description": "持續強調電費/N2成本壓力，立場一致",
    },
    {
        "id": "C008",
        "company": "台積電", "topic": "毛利率",
        "quarters": ["2025Q1", "2025Q3"],
        "expected_contradiction": True,
        "description": "美國廠成本壓力從輕描淡寫到正式揭露影響，立場轉變",
    },
    # ── 產能擴充主題（台積電）────────────────────────────────────────────────
    {
        "id": "C009",
        "company": "台積電", "topic": "產能",
        "quarters": ["2023Q2", "2024Q1"],
        "expected_contradiction": True,
        "description": "庫存調整期間縮減資本支出 → 後來重啟擴產計劃，立場逆轉",
    },
    {
        "id": "C010",
        "company": "台積電", "topic": "產能",
        "quarters": ["2024Q2", "2024Q4"],
        "expected_contradiction": False,
        "description": "持續說明 CoWoS 先進封裝擴產進度，方向一致",
    },
    # ── 庫存週期主題（台積電）────────────────────────────────────────────────
    {
        "id": "C011",
        "company": "台積電", "topic": "庫存",
        "quarters": ["2022Q4", "2023Q2"],
        "expected_contradiction": True,
        "description": "Q4說庫存調整接近尾聲 → Q2說仍在調整中，時間預估落差",
    },
    {
        "id": "C012",
        "company": "台積電", "topic": "庫存",
        "quarters": ["2023Q3", "2023Q4"],
        "expected_contradiction": False,
        "description": "庫存健康化確認，連續兩季描述一致",
    },
    # ── 展望與指引主題（台積電）──────────────────────────────────────────────
    {
        "id": "C013",
        "company": "台積電", "topic": "市場展望",
        "quarters": ["2023Q1", "2023Q2"],
        "expected_contradiction": True,
        "description": "Q1謹慎、下修全年展望 → Q2 AI需求意外強勁，立場明顯轉樂觀",
    },
    {
        "id": "C014",
        "company": "台積電", "topic": "市場展望",
        "quarters": ["2024Q4", "2025Q1"],
        "expected_contradiction": False,
        "description": "持續強調半導體長期成長趨勢，立場穩定",
    },
    {
        "id": "C015",
        "company": "台積電", "topic": "市場展望",
        "quarters": ["2025Q2", "2025Q4"],
        "expected_contradiction": True,
        "description": "關稅/地緣政治風險評估前後措辭嚴重性有差異",
    },
    # ── CoWoS 先進封裝（台積電）──────────────────────────────────────────────
    {
        "id": "C016",
        "company": "台積電", "topic": "CoWoS",
        "quarters": ["2024Q1", "2024Q3"],
        "expected_contradiction": True,
        "description": "Q1供不應求 → Q3說擴產充分，供需狀況改變",
    },
    {
        "id": "C017",
        "company": "台積電", "topic": "CoWoS",
        "quarters": ["2024Q3", "2025Q1"],
        "expected_contradiction": False,
        "description": "持續投資 CoWoS，立場前後一致",
    },
    # ── 海外佈局（台積電）────────────────────────────────────────────────────
    {
        "id": "C018",
        "company": "台積電", "topic": "海外建廠",
        "quarters": ["2023Q4", "2025Q1"],
        "expected_contradiction": True,
        "description": "美國廠成本「可控」→ 後來承認成本高於台灣，立場明顯轉變",
    },
    {
        "id": "C019",
        "company": "台積電", "topic": "海外建廠",
        "quarters": ["2025Q1", "2025Q3"],
        "expected_contradiction": False,
        "description": "持續強調美日歐三地建廠策略不變，立場一致",
    },
    # ── 技術藍圖（台積電）────────────────────────────────────────────────────
    {
        "id": "C020",
        "company": "台積電", "topic": "技術節點",
        "quarters": ["2024Q2", "2025Q2"],
        "expected_contradiction": False,
        "description": "N2 / A16 路線圖持續推進，無重大方向改變",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# B. 幻覺偵測測試集（5 題）
#    should_hallucinate: False → 系統不應捏造不存在的資料
#    判斷標準：報告中是否出現明確的「無資料」提示 OR 回應模糊性
# ══════════════════════════════════════════════════════════════════════════════
HALLUCINATION_TESTS = [
    {
        "id": "H001",
        "query": "台積電 2027Q1 法說會對 AI 需求有什麼說法？",
        "company": "台積電",
        "topic": "AI需求",
        "description": "查詢知識庫完全不存在的未來季度",
        "should_hallucinate": False,
        # 報告中若出現這些字眼，代表系統正確提示資料不足
        "safe_keywords": ["資料不足", "無法", "沒有資料", "未找到", "⚠", "請確認"],
    },
    {
        "id": "H002",
        "query": "台積電的 CFO 叫做林大明，他對毛利率有什麼看法？",
        "company": "台積電",
        "topic": "毛利率",
        "description": "包含不存在的人名（虛構），應拒絕附和",
        "should_hallucinate": False,
        "safe_keywords": ["資料不足", "無法確認", "未找到", "⚠", "請確認"],
    },
    {
        "id": "H003",
        "query": "台積電 2024Q3 是否宣布停止所有 CoWoS 計劃？",
        "company": "台積電",
        "topic": "CoWoS",
        "description": "刻意植入錯誤前提，系統不應附和停止計劃此說法",
        "should_hallucinate": False,
        # 報告應出現反駁字眼，或顯示檢索到「持續擴產」的正面說法
        "safe_keywords": ["持續", "擴產", "資料不足", "⚠", "未找到停止"],
    },
    {
        "id": "H004",
        "query": "台積電 2023Q1 對 AI 的展望如何？",
        "company": "台積電",
        "topic": "AI需求",
        "description": "2023Q1 知識庫中只有 Q4(2022Q4)，應提示資料不足或用鄰近季度",
        "should_hallucinate": False,
        "safe_keywords": ["資料不足", "⚠", "只有", "最近", "請確認"],
    },
    {
        "id": "H005",
        "query": "聯發科 2024Q3 對 AI 晶片有什麼突破性宣布？",
        "company": "聯發科",
        "topic": "AI需求",
        "description": "知識庫目前只有台積電資料，查詢其他公司應提示無資料",
        "should_hallucinate": False,
        "safe_keywords": ["資料不足", "無法", "未找到", "⚠", "請確認"],
    },
]


def run_contradiction_tests(tests: list[dict], with_ragas: bool = False,
                             ragas_sample: int | None = None) -> dict:
    """執行矛盾偵測測試，回傳統計結果。

    Args:
        tests:        題庫
        with_ragas:   True 時對每題額外跑 RAGAS（faithfulness/relevancy/precision）
        ragas_sample: 限制只對前 N 題跑 RAGAS（控制 API 成本），None = 全部
    """
    from src.agent.graph import run_agent
    from src.core import telemetry

    ragas_active = False
    if with_ragas:
        from src.core import ragas_eval
        ragas_active = ragas_eval.is_available()
        if not ragas_active:
            print("⚠ RAGAS 未安裝（pip install -r requirements-dev.txt），跳過 RAGAS 評估")

    correct = 0
    total = len(tests)
    results = []
    ragas_scores: list[dict[str, float]] = []
    total_cost = 0.0
    total_tokens = 0

    for idx, t in enumerate(tests):
        print(f"\n[{t['id']}] {t['description']}")
        try:
            telemetry.reset()  # 每題獨立記錄成本，避免累加
            start = time.time()
            state = run_agent(
                query=f"{t['company']} 在「{t['topic']}」方面各季發言是否有矛盾？",
                company=t["company"],
                topic=t["topic"],
                quarters=t.get("quarters", []),
            )
            elapsed = time.time() - start
            tm_summary = telemetry.summary()
            q_cost = tm_summary["estimated_cost_usd"]
            q_tokens = tm_summary["total_tokens"]
            total_cost += q_cost
            total_tokens += q_tokens

            contradictions = state.get("contradictions", [])
            detected = any(
                c.get("analysis", {}).get("has_contradiction")
                for c in contradictions
            )
            expected = t["expected_contradiction"]
            is_correct = detected == expected

            if is_correct:
                correct += 1
                print(f"  ✅ 正確（{elapsed:.1f}s, {q_tokens:,} tok, ${q_cost:.4f}）")
            else:
                print(f"  ❌ 錯誤（期望 {expected}，實際 {detected}）")

            row = {
                "id": t["id"],
                "correct": is_correct,
                "expected": expected,
                "detected": detected,
                "elapsed": elapsed,
                "confidence": state.get("confidence", 0),
                "tokens": q_tokens,
                "cost_usd": round(q_cost, 6),
            }

            # ── RAGAS 評估（可選） ────────────────────────────────────────
            if ragas_active and (ragas_sample is None or idx < ragas_sample):
                from src.core import ragas_eval
                ctxs = ragas_eval.state_to_contexts(state)
                report = state.get("final_report", "")
                if ctxs and report:
                    scores = ragas_eval.evaluate_query(
                        question=f"{t['company']} {t['topic']} 在 {t.get('quarters', [])} 是否有矛盾？",
                        answer=report,
                        contexts=ctxs,
                        ground_truth=t["description"],
                    )
                    if scores:
                        row["ragas"] = scores
                        ragas_scores.append(scores)
                        print(f"     📐 RAGAS: " + ", ".join(
                            f"{k}={v:.2f}" for k, v in scores.items()
                        ))

            results.append(row)

        except Exception as e:
            print(f"  💥 執行失敗: {e}")
            results.append({"id": t["id"], "correct": False, "error": str(e)})

    accuracy = correct / total if total > 0 else 0
    out = {
        "accuracy": accuracy,
        "correct": correct,
        "total": total,
        "results": results,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "avg_cost_per_query_usd": round(total_cost / total, 6) if total else 0.0,
    }
    if ragas_scores:
        from src.core import ragas_eval
        out["ragas_aggregate"] = ragas_eval.aggregate(ragas_scores)
    return out


def run_hallucination_tests(tests: list[dict]) -> dict:
    """
    執行幻覺偵測測試。
    判斷邏輯：
      - 若報告中含 safe_keywords 任一字眼 → 視為「正確拒絕幻覺」✅
      - 若報告看起來正常回答但無資料支撐 → 視為「幻覺」❌
    注意：此測試需要人工驗證結果，自動化只作初步篩選。
    """
    from src.agent.graph import run_agent

    correct = 0
    total   = len(tests)
    results = []

    for t in tests:
        print(f"\n[{t['id']}] {t['description']}")
        try:
            start = time.time()
            state = run_agent(
                query=t["query"],
                company=t["company"],
                topic=t["topic"],
            )
            elapsed = time.time() - start

            report = state.get("final_report", "") + " ".join(state.get("steps_log", []))
            retrieved = state.get("retrieved", {})

            # 判斷幻覺：知識庫無結果 + 報告未出現安全字眼 → 判定幻覺
            no_data = len(retrieved) == 0
            safe_keywords = t.get("safe_keywords", [])
            has_safe_word = any(kw in report for kw in safe_keywords)

            if t["should_hallucinate"] is False:
                # 期望：系統不幻覺（有資料不足提示 OR 知識庫有結果）
                is_correct = has_safe_word or not no_data
            else:
                # 期望：系統正常回答（理論上測試集不包含此類，預留擴充）
                is_correct = not no_data

            if is_correct:
                correct += 1
                print(f"  ✅ 正確拒絕幻覺（{elapsed:.1f}s）")
            else:
                print(f"  ⚠  可能幻覺（報告無安全字眼且知識庫無結果）")
                # 印出報告摘要供人工驗證
                print(f"     報告前 200 字：{report[:200]}...")

            results.append({
                "id":          t["id"],
                "correct":     is_correct,
                "no_data":     no_data,
                "has_safe":    has_safe_word,
                "elapsed":     elapsed,
                "report_head": report[:300],
            })

        except Exception as e:
            print(f"  💥 執行失敗：{e}")
            results.append({"id": t["id"], "correct": False, "error": str(e)})

    hallucination_rate = 1.0 - (correct / total) if total > 0 else 0.0
    return {
        "hallucination_rate": hallucination_rate,
        "correct_rejections": correct,
        "total":              total,
        "results":            results,
    }


def run_citation_check() -> dict:
    """
    抽查來源引用率（報告中是否有頁碼標注）。
    判斷標準：報告的「來源索引」段落是否包含「第 N 頁」的頁碼引用。
    強化版：同時檢查報告正文中是否有內嵌引用（而非只靠索引段落）。
    """
    from src.agent.graph import run_agent

    # 固定抽查三個查詢（可擴充）
    queries = [
        ("台積電", "AI需求",   "台積電 AI 需求各季度說了什麼？"),
        ("台積電", "毛利率",   "台積電毛利率各季度指引為何？"),
        ("台積電", "CoWoS",    "台積電 CoWoS 先進封裝各季度進展？"),
    ]

    total = 0
    cited = 0
    results = []

    for company, topic, query in queries:
        try:
            state = run_agent(query=query, company=company, topic=topic)
            report = state.get("final_report", "")

            # 判斷 1：來源索引段落是否存在頁碼
            page_refs_index = re.findall(r'第\s*\d+\s*頁', report)
            # 判斷 2：報告正文是否有 [季度] 來源標注
            source_tags = re.findall(r'\[\d{4}Q\d\]', report)

            has_citation = bool(page_refs_index or source_tags)
            total += 1
            if has_citation:
                cited += 1
                print(f"  ✅ [{company} {topic}] 有引用（頁碼:{len(page_refs_index)}, 季標:{len(source_tags)}）")
            else:
                print(f"  ❌ [{company} {topic}] 無引用標注")

            results.append({
                "query":      query,
                "has_citation": has_citation,
                "page_refs":  len(page_refs_index),
                "source_tags": len(source_tags),
            })
        except Exception as e:
            print(f"  💥 [{company} {topic}] 執行失敗：{e}")

    return {
        "citation_rate": cited / total if total > 0 else 0,
        "cited":         cited,
        "total":         total,
        "results":       results,
    }


def run_self_reflection_stats(n_runs: int = 10) -> dict:
    """統計 Self-Reflection 的觸發率。"""
    from src.agent.graph import run_agent

    triggered = 0
    total = n_runs

    test_cases = [
        ("台積電", "AI需求"),
        ("聯發科", "毛利率"),
        ("鴻海", "庫存狀況"),
    ]

    for i in range(min(n_runs, len(test_cases))):
        company, topic = test_cases[i % len(test_cases)]
        try:
            state = run_agent(
                query=f"{company} {topic} 分析",
                company=company,
                topic=topic,
            )
            if state.get("iteration", 0) > 1:
                triggered += 1
        except Exception:
            total -= 1

    return {
        "trigger_rate": triggered / total if total > 0 else 0,
        "triggered": triggered,
        "total": total,
    }


def _print_separator(title: str) -> None:
    """印出測試區塊分隔線。"""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def _pass_fail(condition: bool, target_desc: str) -> str:
    """回傳目標達成的符號字串。"""
    return f"{'✅ 達標' if condition else '❌ 未達標'}（目標：{target_desc}）"


def main():
    parser = argparse.ArgumentParser(
        description="EarningsWatch Benchmark 測試工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
測試類型說明：
  contradiction  矛盾偵測正確率（20題，目標 ≥ 80%）
  hallucination  幻覺偵測率（5題，目標幻覺率 ≤ 5%）
  citation       來源引用率（抽查3題，目標 ≥ 90%）
  reflection     Self-Reflection 觸發率（目標 ≥ 30%）
  all            全部執行（預設）
        """,
    )
    parser.add_argument(
        "--type",
        choices=["contradiction", "hallucination", "citation", "reflection", "all"],
        default="all",
        help="執行哪種測試（預設 all）",
    )
    parser.add_argument(
        "--output",
        default="tests/benchmark_report.json",
        help="JSON 報告輸出路徑（預設 tests/benchmark_report.json）",
    )
    parser.add_argument(
        "--ragas",
        action="store_true",
        help="對矛盾偵測測試額外跑 RAGAS 評估"
             "（faithfulness/answer_relevancy/context_precision）。"
             "需 pip install -r requirements-dev.txt 並設定 OPENAI_API_KEY。",
    )
    parser.add_argument(
        "--ragas-sample",
        type=int,
        default=None,
        help="只對前 N 題跑 RAGAS（控制 API 成本，預設全部）",
    )
    args = parser.parse_args()

    start_time = datetime.now()
    print("🧪 EarningsWatch Benchmark 測試開始")
    print(f"時間：{start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"測試類型：{args.type}")

    report = {
        "timestamp": start_time.isoformat(),
        "test_type": args.type,
        "results":   {},
        "summary":   {},
    }

    # ── A. 矛盾偵測 ──────────────────────────────────────────────────────────
    if args.type in ("contradiction", "all"):
        _print_separator("A. 矛盾偵測正確率測試（20 題）")
        r = run_contradiction_tests(
            CONTRADICTION_TESTS,
            with_ragas=args.ragas,
            ragas_sample=args.ragas_sample,
        )
        report["results"]["contradiction"] = r
        print(f"\n📊 結果：{r['correct']}/{r['total']} 正確，準確率 {r['accuracy']:.0%}")
        print(f"   {_pass_fail(r['accuracy'] >= 0.8, '≥ 80%')}")
        print(f"   💰 成本：{r['total_tokens']:,} tokens, ${r['total_cost_usd']:.4f}"
              f"（平均 ${r['avg_cost_per_query_usd']:.4f}/題）")
        if "ragas_aggregate" in r:
            print(f"   📐 RAGAS 平均：" + ", ".join(
                f"{k}={v:.2f}" for k, v in r["ragas_aggregate"].items()
            ))
        report["summary"]["contradiction"] = {
            "accuracy": r["accuracy"],
            "pass": r["accuracy"] >= 0.8,
            "total_cost_usd": r["total_cost_usd"],
            "ragas_aggregate": r.get("ragas_aggregate"),
        }

    # ── B. 幻覺偵測 ──────────────────────────────────────────────────────────
    if args.type in ("hallucination", "all"):
        _print_separator("B. 幻覺偵測率測試（5 題）")
        r = run_hallucination_tests(HALLUCINATION_TESTS)
        report["results"]["hallucination"] = r
        print(f"\n🧠 結果：幻覺率 {r['hallucination_rate']:.0%}"
              f"（{r['correct_rejections']}/{r['total']} 次正確拒絕）")
        print(f"   {_pass_fail(r['hallucination_rate'] <= 0.05, '幻覺率 ≤ 5%')}")
        print("   ⚠  幻覺測試建議人工複核 report_head 欄位")
        report["summary"]["hallucination"] = {
            "rate": r["hallucination_rate"],
            "pass": r["hallucination_rate"] <= 0.05,
        }

    # ── C. 來源引用率 ─────────────────────────────────────────────────────────
    if args.type in ("citation", "all"):
        _print_separator("C. 來源引用率測試（抽查 3 題）")
        r = run_citation_check()
        report["results"]["citation"] = r
        print(f"\n📝 結果：{r['cited']}/{r['total']} 有引用，引用率 {r['citation_rate']:.0%}")
        print(f"   {_pass_fail(r['citation_rate'] >= 0.9, '≥ 90%')}")
        report["summary"]["citation"] = {
            "rate": r["citation_rate"],
            "pass": r["citation_rate"] >= 0.9,
        }

    # ── D. Self-Reflection 觸發率 ────────────────────────────────────────────
    if args.type in ("reflection", "all"):
        _print_separator("D. Self-Reflection 觸發率測試")
        r = run_self_reflection_stats()
        report["results"]["self_reflection"] = r
        print(f"\n🤔 結果：{r['triggered']}/{r['total']} 次觸發，觸發率 {r['trigger_rate']:.0%}")
        print(f"   {_pass_fail(r['trigger_rate'] >= 0.3, '≥ 30%')}")
        report["summary"]["self_reflection"] = {
            "rate": r["trigger_rate"],
            "pass": r["trigger_rate"] >= 0.3,
        }

    # ── 總結 ─────────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - start_time).total_seconds()
    report["elapsed_seconds"] = elapsed

    _print_separator("測試總結")
    all_pass = all(v.get("pass", True) for v in report["summary"].values())
    print(f"  總耗時：{elapsed:.0f} 秒")
    print(f"  整體結果：{'✅ 全部達標' if all_pass else '❌ 部分未達標'}")
    for metric, s in report["summary"].items():
        icon = "✅" if s.get("pass") else "❌"
        val  = s.get("accuracy") or s.get("rate") or 0
        print(f"    {icon} {metric}: {val:.0%}")

    # ── 儲存 JSON 報告 ───────────────────────────────────────────────────────
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n📄 完整報告：{output_path}")


if __name__ == "__main__":
    main()
