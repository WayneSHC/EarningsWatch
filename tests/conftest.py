"""
Shared pytest fixtures.

Critical: most tests stub out llm_client.chat to avoid real API calls.
Set dummy env so contradiction.py / llm_client.py modules import cleanly
even when no real keys are present (e.g. on CI).
"""
import os
import sys
from pathlib import Path

import pytest

# Make `src.*` imports work without installing the package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """Provide dummy keys so module-level code paths don't blow up."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    # anthropic / groq backends removed (no API key) — ensure absent in tests.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    # Neutralise Secret Manager: if a real .env (with GCP_SECRET_PROJECT) is
    # present in the repo root and gets loaded — e.g. a developer running the
    # suite locally, or a stray `import tests.benchmark` triggering
    # load_dotenv() during collection — get_secret() would consult GCP Secret
    # Manager and resolve REAL keys. That breaks backend-detection tests that
    # assert "no keys available". Unset it so the suite stays hermetic and
    # depends only on the dummy env vars above.
    monkeypatch.delenv("GCP_SECRET_PROJECT", raising=False)
    # Reset detection cache so each test sees the fixture env.
    from src.core import llm_client
    llm_client._detect_backend.cache_clear()
    yield


def make_chunk(content: str, quarter: str = "2024Q1",
               file: str = "TSMC 1Q24.pdf", page: int = 1,
               date: str = "2024-04-18") -> dict:
    """Build a Qdrant-style chunk for tests."""
    return {
        "payload": {
            "content": content,
            "quarter": quarter,
            "date": date,
            "source_file": file,
            "source_page": page,
        }
    }
