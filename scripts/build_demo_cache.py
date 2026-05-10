"""
scripts/build_demo_cache.py

預先跑過所有 (公司, 主題) 預設組合，把結果寫入 cache/demo_cache.json。
之後 Streamlit Cloud 上若 LLM API 配額用盡或 Qdrant 連線失敗，
UI 仍可從這個快取載入結果，避免「無快取資料」的死路。

執行方式：
    # 確保 .env 已設定且 LLM 配額充足
    python scripts/build_demo_cache.py                    # 跑全部組合（4 公司 × 6 主題 = 24 跑）
    python scripts/build_demo_cache.py --companies 台積電  # 只跑指定公司
    python scripts/build_demo_cache.py --topics AI需求 毛利率
    python scripts/build_demo_cache.py --dry-run           # 只列出將要跑的組合

注意：
- 每跑一個組合會呼叫多次 LLM；24 組合粗估 $0.50 ~ $2 USD（依模型而定）。
- 跑失敗的組合會略過（不阻擋其他組合），失敗清單會在最後印出。
- 已存在於 cache 中且 --force 未指定的組合會跳過。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

# 與 UI 一致的預設清單，避免兩處定義漂移
COMPANIES = ["台積電", "聯發科", "鴻海", "台達電"]
TOPICS = ["AI需求", "毛利率", "產能與擴產", "庫存狀況", "市場展望", "CoWoS"]


def _run_one(company: str, topic: str) -> dict:
    """執行一個組合，回傳 Agent 最終 state（含 final_report 等）。"""
    from src.agent.graph import get_agent

    agent = get_agent()
    query = (
        f"{company} 在「{topic}」方面，各季度發言是否有矛盾或立場轉變？"
        "請追蹤承諾兌現情況。"
    )
    initial_state = {
        "query": query,
        "company": company,
        "topic": topic,
        "quarters": [],  # 留空 = 全部季度，與 UI 預設行為一致
        "sub_queries": [],
        "tool_plan": [],
        "retrieved": {},
        "news_context": [],
        "stock_data": {},
        "contradictions": [],
        "promises": [],
        "confidence": 1.0,
        "iteration": 0,
        "reflection_issues": [],
        "reflection_gaps": [],
        "final_report": "",
        "steps_log": [],
    }
    return agent.invoke(initial_state)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build demo_cache.json baseline")
    parser.add_argument("--companies", nargs="*", default=COMPANIES,
                        help=f"要跑的公司（預設：{COMPANIES}）")
    parser.add_argument("--topics", nargs="*", default=TOPICS,
                        help=f"要跑的主題（預設：{TOPICS}）")
    parser.add_argument("--force", action="store_true",
                        help="即使快取已存在也重跑")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出組合，不實際執行")
    parser.add_argument("--min-confidence", type=float, default=0.6,
                        help="低於此 confidence 的結果不寫入快取（預設 0.6，避免 LLM 全掛時"
                             "存入無用的「偵測失敗」垃圾資料；設為 0 可一律保存）")
    args = parser.parse_args()

    from src.ui.cache import get_cached_result, save_to_cache

    combos = [(c, t) for c in args.companies for t in args.topics]
    print(f"目標組合：{len(combos)} 個（{len(args.companies)} 公司 × {len(args.topics)} 主題）")
    print(f"品質門檻：confidence >= {args.min_confidence:.2f} 才寫入 cache")

    if args.dry_run:
        for c, t in combos:
            cached = get_cached_result(c, t, [], "")
            mark = "已快取" if cached else "待產出"
            print(f"  [{mark}] {c} × {t}")
        return 0

    failed: list[tuple[str, str, str]] = []
    skipped = 0
    succeeded = 0
    rejected_low_conf = 0

    for i, (company, topic) in enumerate(combos, 1):
        if not args.force and get_cached_result(company, topic, [], ""):
            print(f"[{i}/{len(combos)}] ⏭  跳過（已快取）：{company} × {topic}")
            skipped += 1
            continue

        print(f"[{i}/{len(combos)}] ⏳ 執行：{company} × {topic} ...")
        t0 = time.perf_counter()
        try:
            result = _run_one(company, topic)
            dt = time.perf_counter() - t0
            conf = float(result.get("confidence", 0) or 0)
            if conf < args.min_confidence:
                # [b] 低 confidence 通常代表 LLM 全掛、矛盾偵測全失敗，
                # 這種結果留在 cache 會誤導 demo viewer，改成「跑了但不存」
                print(
                    f"            ⚠ 跳過寫入（{dt:.1f}s，confidence={conf:.2f} "
                    f"< {args.min_confidence:.2f} 門檻）"
                )
                rejected_low_conf += 1
            else:
                save_to_cache(company, topic, result, [], "")
                print(f"            ✅ 完成（{dt:.1f}s，confidence={conf:.2f}）")
                succeeded += 1
        except Exception as e:
            etype = type(e).__name__
            print(f"            ❌ 失敗：{etype}: {str(e)[:160]}")
            failed.append((company, topic, etype))

    print()
    print("=" * 60)
    print(
        f"完成：成功 {succeeded}　跳過 {skipped}　"
        f"低 confidence {rejected_low_conf}　失敗 {len(failed)}"
    )
    if failed:
        print("失敗清單：")
        for c, t, etype in failed:
            print(f"  - {c} × {t} → {etype}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
