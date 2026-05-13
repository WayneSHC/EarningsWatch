"""
src/core/secrets.py
GCP Secret Manager backed credential loader with .env fallback.

Resolution order for every key:
  1. If GCP_SECRET_PROJECT env var is set AND google-cloud-secret-manager is
     installed → fetch secret <name>:latest from that GCP project.
  2. Otherwise → fall back to os.getenv(name) (local dev / Streamlit Cloud /
     Cloud Run with --set-secrets injecting env vars).

Why both:
  - Cloud Run can mount secrets as env vars natively (--set-secrets) — that
    path is preferred in production because the SDK never has to make a
    Secret Manager API call. Step (2) covers this case unchanged.
  - For local dev with parity to prod (or any compute that can't use
    --set-secrets, e.g. GCE / GKE without CSI driver), step (1) pulls
    secrets directly using Application Default Credentials.

Failures in step (1) (auth missing, secret absent, transient API error)
degrade silently to step (2) — the app should still boot if Secret Manager
is briefly unavailable. A one-line warning is printed per missing secret.

Placeholder detection (<your-...-here>, sk-..., etc.) is centralised here
so every consumer benefits from it, not just llm_client.
"""

from __future__ import annotations

import os
from functools import lru_cache

# Reuse the placeholder check from llm_client so policy stays in one place.
_PLACEHOLDER_MARKERS = (
    "<your-", "your-key-here", "placeholder", "changeme",
    "sk-...", "tvly-...", "llx-...", "ls__...",
)


def _is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return bool(v) and any(m in v for m in _PLACEHOLDER_MARKERS)


def _gcp_project() -> str:
    """Return GCP project id used for Secret Manager, or '' if disabled."""
    return os.getenv("GCP_SECRET_PROJECT", "").strip()


@lru_cache(maxsize=1)
def _sm_client():
    """Lazy import + singleton. Returns None if the SDK isn't installed."""
    try:
        from google.cloud import secretmanager  # type: ignore
        return secretmanager.SecretManagerServiceClient()
    except ImportError:
        print(
            "[secrets] ⚠ google-cloud-secret-manager not installed; "
            "falling back to env vars. Run: pip install google-cloud-secret-manager"
        )
        return None
    except Exception as exc:  # auth / network failure
        print(f"[secrets] ⚠ Secret Manager client init failed ({type(exc).__name__}); falling back to env vars")
        return None


@lru_cache(maxsize=64)
def _fetch_from_gcp(secret_name: str, project: str) -> str | None:
    """Fetch latest version of a secret from GCP Secret Manager. Returns None on any failure."""
    client = _sm_client()
    if client is None:
        return None
    resource = f"projects/{project}/secrets/{secret_name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": resource})
        return response.payload.data.decode("utf-8")
    except Exception as exc:
        # Common: NotFound (secret doesn't exist), PermissionDenied (SA lacks accessor role).
        # We don't print the secret name's value, only that we failed.
        print(f"[secrets] ⚠ failed to fetch '{secret_name}' from Secret Manager ({type(exc).__name__}); falling back to env")
        return None


def get_secret(name: str, default: str = "") -> str:
    """
    Resolve a secret by name. The same `name` is used as both the env-var key
    and the Secret Manager secret id (keeps cognitive load low — no separate
    naming scheme to remember).

    Returns the resolved value, or `default` if nothing was found. Placeholder
    values from .env.example are treated as missing.

    Caller code stays simple:
        api_key = get_secret("OPENAI_API_KEY")
    """
    project = _gcp_project()
    if project:
        value = _fetch_from_gcp(name, project)
        if value and not _is_placeholder(value):
            return value
        # fall through to env on miss / placeholder

    env_value = os.getenv(name, "").strip()
    if env_value and not _is_placeholder(env_value):
        return env_value

    return default


def is_gcp_enabled() -> bool:
    """True iff Secret Manager will be consulted (project set + SDK importable)."""
    return bool(_gcp_project()) and _sm_client() is not None


def clear_cache() -> None:
    """Force re-read of all secrets. Useful in tests / after rotation."""
    _fetch_from_gcp.cache_clear()
    _sm_client.cache_clear()
