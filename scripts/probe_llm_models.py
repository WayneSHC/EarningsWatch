"""
scripts/probe_llm_models.py
驗證 src/core/llm_client.py 的 BACKEND_MODELS 在實際 API 上仍可用。

用法：
    python scripts/probe_llm_models.py

當 OpenAI / Gemini 改名 / 下線模型時，BACKEND_MODELS 會在執行時 404，
要等兩層 fallback 失敗才 cascade，benchmark 從 ~10s/query 變成 ~5min/query。
此腳本三步驟回答「設定的模型是否還能用」：
  1. models.list() 看當下 catalog 裡有什麼
  2. BACKEND_MODELS[*] 名稱是否在 catalog 裡
  3. 對每個 configured 模型發極短 chat 請求，確認 API 真的回應（不是 404 / 403）

設計：
  - 不引入任何新套件；只用 llm_client 已宣告過的 SDK
  - 缺 API key 的後端 → 標 "skipped"，不報錯
  - 失敗 → 印實際錯誤類型 + 訊息（短截），便於辨別 404 vs 429 vs 401
  - 對 stdout 友善：可直接 grep "OK" / "FAIL"

歷史：
  2026-05-08 寫於 BACKEND_MODELS 從 `gpt-5o` / `gemini-3.0-flash` 校正回
  `gpt-5` / `gemini-2.5-flash` 之後（commit 992f77a）。下次再出現 404 直接重跑此腳本。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 讓 `python scripts/probe_llm_models.py` 從 repo 根目錄能 import src.*
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv()

from src.core.llm_client import BACKEND_MODELS, _KEY_ENV  # noqa: E402


# ── 輸出格式工具 ──────────────────────────────────────────────────────────────

def _pad(s: str, n: int) -> str:
    return s.ljust(n)


def _short_err(e: Exception, n: int = 100) -> str:
    msg = f"{type(e).__name__}: {e}"
    return msg if len(msg) <= n else msg[:n] + "…"


# ── 各後端探測 ────────────────────────────────────────────────────────────────

def probe_openai() -> None:
    print("\n── OpenAI ────────────────────────────────────────────────")
    if not os.getenv(_KEY_ENV["openai"], "").strip():
        print("  skipped — OPENAI_API_KEY 未設定")
        return
    try:
        import openai
        client = openai.OpenAI()
    except Exception as e:
        print(f"  client init FAIL — {_short_err(e)}")
        return

    catalog: set[str] = set()
    try:
        for m in client.models.list():
            catalog.add(m.id)
        print(f"  catalog: {len(catalog)} models")
    except Exception as e:
        # list 失敗仍探 chat：有時 models.list 被 quota 擋，但 chat 能用
        print(f"  models.list() FAIL — {_short_err(e)}")

    for mode, model in BACKEND_MODELS["openai"].items():
        in_cat = "✓" if (not catalog or model in catalog) else "✗"
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_completion_tokens=5,
            )
            tok = int(getattr(resp.usage, "completion_tokens", 0) or 0) if resp.usage else 0
            print(f"  {_pad(mode, 6)} {_pad(model, 28)} catalog={in_cat}  OK   ({tok} tok)")
        except Exception as e:
            print(f"  {_pad(mode, 6)} {_pad(model, 28)} catalog={in_cat}  FAIL — {_short_err(e)}")


def probe_gemini() -> None:
    print("\n── Gemini ────────────────────────────────────────────────")
    if not os.getenv(_KEY_ENV["gemini"], "").strip():
        print("  skipped — GEMINI_API_KEY 未設定")
        return
    try:
        from google import genai
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    except Exception as e:
        print(f"  client init FAIL — {_short_err(e)}")
        return

    catalog: set[str] = set()
    try:
        for m in client.models.list():
            # genai 回傳的 .name 是 "models/gemini-..."；標準化成裸名
            name = getattr(m, "name", "") or ""
            catalog.add(name.removeprefix("models/"))
        print(f"  catalog: {len(catalog)} models")
    except Exception as e:
        print(f"  models.list() FAIL — {_short_err(e)}")

    for mode, model in BACKEND_MODELS["gemini"].items():
        in_cat = "✓" if (not catalog or model in catalog) else "✗"
        try:
            resp = client.models.generate_content(model=model, contents="ping")
            tok = 0
            meta = getattr(resp, "usage_metadata", None)
            if meta:
                tok = int(getattr(meta, "candidates_token_count", 0) or 0)
            print(f"  {_pad(mode, 6)} {_pad(model, 28)} catalog={in_cat}  OK   ({tok} tok)")
        except Exception as e:
            print(f"  {_pad(mode, 6)} {_pad(model, 28)} catalog={in_cat}  FAIL — {_short_err(e)}")


def probe_cohere() -> None:
    """Cohere 沒有需要的 models.list 等價端點；直接 chat 探測即可。"""
    print("\n── Cohere ────────────────────────────────────────────────")
    if not os.getenv(_KEY_ENV["cohere"], "").strip():
        print("  skipped — COHERE_API_KEY 未設定")
        return
    try:
        import cohere
        client = cohere.Client(os.getenv("COHERE_API_KEY", ""))
    except Exception as e:
        print(f"  client init FAIL — {_short_err(e)}")
        return

    for mode, model in BACKEND_MODELS["cohere"].items():
        try:
            resp = client.chat(model=model, message="ping", max_tokens=5)
            print(f"  {_pad(mode, 6)} {_pad(model, 28)} OK   ({len(resp.text)} chars)")
        except Exception as e:
            print(f"  {_pad(mode, 6)} {_pad(model, 28)} FAIL — {_short_err(e)}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("BACKEND_MODELS probe — 驗證模型名稱在實際 API 上仍可用")
    print("設定來源：src/core/llm_client.py BACKEND_MODELS")
    for backend, models in BACKEND_MODELS.items():
        print(f"  {backend}: {models}")
    probe_openai()
    probe_gemini()
    probe_cohere()
    print("\n完成。任何 FAIL 都表示需要更新 BACKEND_MODELS（或 API key / quota 失效）。")


if __name__ == "__main__":
    main()
