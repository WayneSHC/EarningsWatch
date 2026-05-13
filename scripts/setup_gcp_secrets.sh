#!/usr/bin/env bash
# Push every API key from a local .env file into GCP Secret Manager.
#
# Idempotent: creates the secret if missing, otherwise adds a new version.
# Skips empty values and obvious placeholders (<your-...>, sk-..., etc.).
#
# Usage:
#   ./scripts/setup_gcp_secrets.sh                  # uses ./.env
#   ./scripts/setup_gcp_secrets.sh path/to/.env     # explicit file
#
# Prereqs:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - Active project set (gcloud config set project YOUR_PROJECT_ID)
#   - Secret Manager API enabled:
#       gcloud services enable secretmanager.googleapis.com

set -euo pipefail

ENV_FILE="${1:-.env}"
if [ ! -f "$ENV_FILE" ]; then
    echo "✗ env file not found: $ENV_FILE" >&2
    exit 1
fi

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    echo "✗ no active gcloud project. Run: gcloud config set project YOUR_PROJECT_ID" >&2
    exit 1
fi

echo "→ project: $PROJECT_ID"
echo "→ env file: $ENV_FILE"
echo

# Keys to upload. GCP_SECRET_PROJECT itself is intentionally excluded —
# it's a config flag, not a credential, and lives in the env on the host.
KEYS=(
    OPENAI_API_KEY
    ANTHROPIC_API_KEY
    GEMINI_API_KEY
    COHERE_API_KEY
    TAVILY_API_KEY
    LLAMA_CLOUD_API_KEY
    QDRANT_URL
    QDRANT_API_KEY
    APP_PASSWORD
    LANGSMITH_API_KEY
)

# Patterns that mean "this is a placeholder, not a real key".
# Use `tr` for lowercasing — `${var,,}` needs bash 4+, macOS ships bash 3.2.
is_placeholder() {
    local lower
    lower=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    case "$lower" in
        *"<your-"*|*"your-key-here"*|*placeholder*|*changeme*) return 0 ;;
        *"sk-..."*|*"tvly-..."*|*"llx-..."*|*"ls__..."*) return 0 ;;
        *) return 1 ;;
    esac
}

uploaded=0
skipped=0
for KEY in "${KEYS[@]}"; do
    # Extract value from .env (handles KEY=value and KEY="value" forms).
    VALUE="$(grep -E "^${KEY}=" "$ENV_FILE" | head -n1 | cut -d= -f2- | sed -E 's/^"(.*)"$/\1/;s/^'\''(.*)'\''$/\1/' || true)"

    if [ -z "$VALUE" ]; then
        echo "  ⊘ $KEY  (empty in $ENV_FILE)"
        skipped=$((skipped + 1))
        continue
    fi
    if is_placeholder "$VALUE"; then
        echo "  ⊘ $KEY  (placeholder)"
        skipped=$((skipped + 1))
        continue
    fi

    # Create secret if it doesn't exist (suppress noise on existing secrets).
    if ! gcloud secrets describe "$KEY" --project="$PROJECT_ID" >/dev/null 2>&1; then
        gcloud secrets create "$KEY" --project="$PROJECT_ID" \
            --replication-policy="automatic" >/dev/null
        echo "  + $KEY  (created)"
    fi

    # Add new version. Using stdin so the value never appears in shell history.
    printf '%s' "$VALUE" \
        | gcloud secrets versions add "$KEY" \
            --project="$PROJECT_ID" \
            --data-file=- >/dev/null
    echo "  ✓ $KEY  (new version added)"
    uploaded=$((uploaded + 1))
done

echo
echo "done — $uploaded uploaded, $skipped skipped"
echo
echo "Next steps:"
echo "  1. Grant access to the runtime service account, e.g.:"
echo "       PROJECT_NUMBER=\$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')"
echo "       SA=\"\${PROJECT_NUMBER}-compute@developer.gserviceaccount.com\""
echo "       for s in ${KEYS[*]}; do"
echo "         gcloud secrets add-iam-policy-binding \$s --project=$PROJECT_ID \\"
echo "           --member=\"serviceAccount:\$SA\" \\"
echo "           --role=\"roles/secretmanager.secretAccessor\" --quiet 2>/dev/null || true"
echo "       done"
echo
echo "  2. To use Secret Manager from this machine, set in .env:"
echo "       GCP_SECRET_PROJECT=$PROJECT_ID"
echo "     and authenticate ADC:"
echo "       gcloud auth application-default login"
echo
echo "  3. Rotate a key later:"
echo "       printf '%s' 'new-key-value' | gcloud secrets versions add OPENAI_API_KEY \\"
echo "         --project=$PROJECT_ID --data-file=-"
