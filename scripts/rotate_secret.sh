#!/usr/bin/env bash
# Safely rotate one secret in GCP Secret Manager.
#
# Usage:
#   ./scripts/rotate_secret.sh OPENAI_API_KEY
#   ./scripts/rotate_secret.sh ANTHROPIC_API_KEY
#
# Why this exists:
#   The naive pattern (printf '%s' 'KEY' | gcloud secrets versions add)
#   breaks if you paste from a context that converts straight quotes ' to
#   curly quotes ' (zsh gets stuck at quote>) or if you paste the literal
#   "PASTE_NEW_KEY_HERE" placeholder. This script uses `read -rs` so the
#   key never goes through quote parsing, and shows you length + prefix
#   so you confirm it's a real key before upload.

set -euo pipefail

SECRET_NAME="${1:-}"
PROJECT="${GCP_SECRET_PROJECT:-earningswatch-demo}"

if [ -z "$SECRET_NAME" ]; then
    echo "Usage: $0 <SECRET_NAME>" >&2
    echo "Example: $0 OPENAI_API_KEY" >&2
    exit 1
fi

# Sanity: verify the secret already exists (rotation = new version, not creation).
if ! gcloud secrets describe "$SECRET_NAME" --project="$PROJECT" >/dev/null 2>&1; then
    echo "✗ secret '$SECRET_NAME' not found in project '$PROJECT'" >&2
    echo "  to create a brand-new secret, use scripts/setup_gcp_secrets.sh instead" >&2
    exit 1
fi

# Show current state so the user knows what they're replacing.
echo "→ project: $PROJECT"
echo "→ secret:  $SECRET_NAME"
echo ""
current_val=$(gcloud secrets versions access latest --secret="$SECRET_NAME" --project="$PROJECT" 2>/dev/null || true)
if [ -n "$current_val" ]; then
    echo "  current :latest — len=${#current_val}, prefix=$(printf '%s' "$current_val" | head -c 12)..."
else
    echo "  current :latest — (unable to read; maybe destroyed)"
fi
echo ""

# Read the new key. -s hides input, -r treats backslashes literally.
echo "Paste new key, then press Enter (input is hidden):"
read -rs NEW_KEY
echo ""

# Trim trailing newline/whitespace that some terminals append on paste.
NEW_KEY="${NEW_KEY%$'\n'}"
NEW_KEY="${NEW_KEY%$'\r'}"

if [ -z "$NEW_KEY" ]; then
    echo "✗ no input received — aborting" >&2
    exit 1
fi

# Common-mistake guard: catch placeholder strings and obvious junk.
case "$NEW_KEY" in
    PASTE_*|*"<your-"*|paste-here|*"placeholder"*)
        echo "✗ that looks like a placeholder string, not a real key — aborting" >&2
        exit 1
        ;;
esac

len=${#NEW_KEY}
prefix=$(printf '%s' "$NEW_KEY" | head -c 12)
echo "  new value — len=$len, prefix=$prefix..."
echo ""

# Confirmation gate.
read -r -p "Upload as new version of $SECRET_NAME? [y/N] " confirm
case "$confirm" in
    y|Y|yes|YES) ;;
    *) echo "aborted (no upload)"; exit 0 ;;
esac

# Stdin pipe — value never appears in shell history or process listing.
printf '%s' "$NEW_KEY" \
    | gcloud secrets versions add "$SECRET_NAME" \
        --project="$PROJECT" \
        --data-file=-

# Re-read to confirm.
unset NEW_KEY
final_val=$(gcloud secrets versions access latest --secret="$SECRET_NAME" --project="$PROJECT")
echo ""
echo "✓ rotated — :latest is now len=${#final_val}, prefix=$(printf '%s' "$final_val" | head -c 12)..."
echo ""
echo "⚠ Don't forget to revoke the old key on the provider dashboard."
