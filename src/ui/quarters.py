"""
src/ui/quarters.py

從 Qdrant 動態讀取實際存在的季度列表（給 sidebar 下拉用）。

抽出 app.py 的目的：
  - app.py 變薄成入口 + dispatch；資料載入函式不該擠在 view 入口裡
  - 此函式有完整 Qdrant 容錯路徑（facet → scroll → hardcoded fallback），
    獨立成模組後便於後續加 unit test
"""

from __future__ import annotations

import streamlit as st


@st.cache_data(ttl=300, show_spinner=False)
def get_available_quarters() -> list[str]:
    """
    從 Qdrant 動態讀取實際存在的季度列表，按時間排序。

    效能優化：優先使用 Qdrant facet API（v1.10+，一次查詢即可取得所有唯一值）。
    若版本不支援，改用 scroll + 提早停止策略，避免全資料掃描。
    """
    try:
        from src.core.qdrant_client import get_qdrant_client, COLLECTION_NAME
        client = get_qdrant_client()

        # ── 優先：facet API（Qdrant >= 1.10，效率最高）──────────────
        if hasattr(client, "facet"):
            resp = client.facet(
                collection_name=COLLECTION_NAME,
                key="quarter",
                limit=100,   # 季度數量不會超過 100
            )
            quarters = [hit.value for hit in resp.hits if hit.value]
            if quarters:
                return sorted(quarters, key=lambda x: (x[:4], x[4:]))

        # ── 降級：scroll 限制最大掃描筆數（避免 full scan）────────────
        quarters: set[str] = set()
        offset = None
        MAX_SCAN = 2000      # 最多掃 2000 筆即可涵蓋所有季度組合
        # [b] 用獨立計數器，不依賴 offset 型別
        # （Qdrant >= 1.7 的 offset 是 UUID 字串，(offset or 0) 會 TypeError）
        total_scanned = 0

        while True:
            results, next_offset = client.scroll(
                collection_name=COLLECTION_NAME,
                limit=200,
                offset=offset,
                with_payload=["quarter"],
                with_vectors=False,
            )
            for r in results:
                q = r.payload.get("quarter", "")
                if q:
                    quarters.add(q)

            total_scanned += len(results)
            if next_offset is None or total_scanned >= MAX_SCAN:
                break
            offset = next_offset

        # 排序：2022Q4, 2023Q1, ... 2026Q1
        return sorted(quarters, key=lambda x: (x[:4], x[4:]))

    except (ConnectionError, TimeoutError, OSError):
        # Qdrant 未啟動 / 連線失敗時的 fallback
        pass
    except Exception as _e:
        # [b] 非預期的程式錯誤（如 API 格式變更）記錄但不讓它靜默吞掉
        print(f"[UI] get_available_quarters 意外失敗: {type(_e).__name__}: {_e}")
    return ["2022Q4", "2023Q1", "2023Q2", "2023Q3",
            "2024Q2", "2024Q3", "2024Q4",
            "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1"]
