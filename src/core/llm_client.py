"""
src/core/llm_client.py
統一 LLM 呼叫介面：支援四種後端，一個環境變數切換

支援的後端（在 .env 設定 LLM_BACKEND）：
  gemini    → Gemini 1.5 Flash（開發期，免費 1500次/天）✅ 目前使用
  openai    → GPT-4o-mini（已有 Key）
  groq      → Llama 3.3 70B（免費，速度快，選用）
  cohere    → Command R+（有 Cohere Key 可用）
  anthropic → Claude 3.5 Sonnet（Demo 最佳，取得 Key 後切換）

未設定 LLM_BACKEND 時自動偵測順序：
  anthropic → gemini → openai → groq → cohere
"""

import os
from functools import lru_cache


# ── 模型設定表 ────────────────────────────────────────────────────────────────
BACKEND_MODELS = {
    "anthropic": {
        "dev":  "claude-3-haiku-20240307",
        "demo": "claude-3-5-sonnet-20241022",
    },
    "openai": {
        "dev":  "gpt-4o-mini",
        "demo": "gpt-4o",
    },
    "gemini": {
        "dev":  "gemini-2.0-flash",   # 免費層，開發夠用
        "demo": "gemini-2.0-flash",   # Demo 時可切換
    },
    "groq": {
        "dev":  "llama-3.3-70b-versatile",
        "demo": "llama-3.3-70b-versatile",
    },
    "cohere": {
        "dev":  "command-r-08-2024",
        "demo": "command-r-plus-08-2024",
    },
}

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "groq":      "GROQ_API_KEY",
    "cohere":    "COHERE_API_KEY",
}
_AUTO_DETECT_ORDER = ["anthropic", "gemini", "openai", "groq", "cohere"]


def _detect_backend() -> str:
    """
    自動偵測可用的 LLM 後端。
    若明確設定 LLM_BACKEND 則直接使用。

    注意：此函數使用 lru_cache 快取，確保只在程序啟動時偵測一次，
    避免每次 chat() 呼叫都重複讀取環境變數。
    若需強制重新偵測（例如動態更換 Key），呼叫 _detect_backend.cache_clear()。
    """
    explicit = os.getenv("LLM_BACKEND", "").strip().lower()
    # [b] 有設定但值不合法時給明確警告，不靜默降級
    if explicit and explicit not in BACKEND_MODELS:
        print(
            f"[LLM] ⚠ LLM_BACKEND='{explicit}' 不在支援清單 {list(BACKEND_MODELS)}，"
            f"改為自動偵測"
        )
    if explicit in BACKEND_MODELS:
        key = os.getenv(_KEY_ENV[explicit], "").strip()
        if not key:
            raise EnvironmentError(
                f"❌ LLM_BACKEND={explicit} 但找不到 {_KEY_ENV[explicit]}，請填入 .env"
            )
        return explicit

    # 自動偵測
    for backend in _AUTO_DETECT_ORDER:
        if os.getenv(_KEY_ENV[backend], "").strip():
            return backend

    raise EnvironmentError(
        "❌ 找不到任何 LLM API Key！\n"
        "請在 .env 填入以下任一個：\n"
        "  GEMINI_API_KEY     → Gemini 1.5 Flash（免費）✅ 推薦\n"
        "  OPENAI_API_KEY     → GPT-4o-mini（已有）\n"
        "  GROQ_API_KEY       → Llama 3.3 70B（免費）\n"
        "  ANTHROPIC_API_KEY  → Claude（Demo 最佳）"
    )


# 用 lru_cache 包裝：後端偵測只執行一次，後續從快取讀取
_detect_backend = lru_cache(maxsize=1)(_detect_backend)


def get_model_name(mode: str = "demo") -> str:
    backend = _detect_backend()
    return BACKEND_MODELS[backend][mode]


# ── 各後端 Client 單例工廠（lru_cache 確保每個後端只建立一次）────────────────
# [c] 避免每次 LLM 呼叫都重新實例化 client，特別是矛盾偵測有 N-1 次呼叫時

@lru_cache(maxsize=1)
def _get_anthropic_client():
    import anthropic
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


@lru_cache(maxsize=None)
def _get_openai_client(api_key_env: str = "OPENAI_API_KEY", base_url: str | None = None):
    # [c] maxsize=None（無限快取）：依 (api_key_env, base_url) 參數組合分別快取。
    #     maxsize=1 只快取最後一組參數，Groq（不同 base_url）與 OpenAI 會互相覆蓋。
    import openai
    return openai.OpenAI(api_key=os.getenv(api_key_env), base_url=base_url)


@lru_cache(maxsize=1)
def _get_gemini_client():
    from google import genai
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


@lru_cache(maxsize=1)
def _get_cohere_llm_client():
    import cohere
    return cohere.Client(os.getenv("COHERE_API_KEY", ""))


# ── 各後端呼叫實作 ────────────────────────────────────────────────────────────

def _call_anthropic(prompt: str, model: str, max_tokens: int) -> str:
    client = _get_anthropic_client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _call_openai_compat(prompt: str, model: str, max_tokens: int,
                         base_url: str | None = None,
                         api_key_env: str = "OPENAI_API_KEY") -> str:
    """OpenAI SDK 相容介面，Groq 也用這個。"""
    client = _get_openai_client(api_key_env, base_url)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


def _call_gemini(prompt: str, model: str, max_tokens: int) -> str:
    from google.genai import types
    client = _get_gemini_client()
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    return resp.text.strip()


def _call_cohere(prompt: str, model: str, max_tokens: int) -> str:
    client = _get_cohere_llm_client()
    resp = client.chat(model=model, message=prompt, max_tokens=max_tokens)
    return resp.text.strip()


# ── 統一呼叫入口 ──────────────────────────────────────────────────────────────

def chat(prompt: str, max_tokens: int = 600, mode: str = "demo") -> str:
    """
    統一 LLM 呼叫介面。
    傳入 prompt，回傳文字結果，底層自動選擇後端。
    """
    # [b] 空 prompt 防呆：避免浪費 token 且回傳無意義結果
    if not prompt or not prompt.strip():
        raise ValueError("[LLM] prompt 不可為空字串")
    backend = _detect_backend()
    model = BACKEND_MODELS[backend][mode]

    if backend == "anthropic":
        return _call_anthropic(prompt, model, max_tokens)
    elif backend == "openai":
        return _call_openai_compat(prompt, model, max_tokens)
    elif backend == "gemini":
        return _call_gemini(prompt, model, max_tokens)
    elif backend == "groq":
        return _call_openai_compat(
            prompt, model, max_tokens,
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
        )
    elif backend == "cohere":
        return _call_cohere(prompt, model, max_tokens)
    else:
        raise ValueError(f"未知的 LLM 後端：{backend}")


def which_backend() -> str:
    """回傳目前後端資訊，給 UI 顯示用。"""
    try:
        backend = _detect_backend()
        model = get_model_name("demo")
        labels = {
            "anthropic": "Claude",
            "openai":    "OpenAI",
            "gemini":    "Gemini",
            "groq":      "Groq",
            "cohere":    "Cohere",
        }
        return f"{labels.get(backend, backend)} ({model})"
    except EnvironmentError as e:
        return str(e)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(f"🤖 目前 LLM 後端：{which_backend()}")
    result = chat("用繁體中文，一句話介紹台積電的主要業務。", max_tokens=100)
    print(f"💬 測試回應：{result}")
