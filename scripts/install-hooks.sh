#!/usr/bin/env bash
# Install repo-managed git hooks by pointing core.hooksPath at .githooks/.
# Run once per clone: ./scripts/install-hooks.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .githooks ]; then
    echo "✗ .githooks/ not found — are you in the repo root?" >&2
    exit 1
fi

chmod +x .githooks/pre-commit
git config core.hooksPath .githooks

echo "✓ git hooks installed (core.hooksPath = .githooks)"
echo "  pre-commit will block staged secrets (sk-..., AIza..., tvly-..., llx-..., ls__...)"
echo "  bypass once with: git commit --no-verify  (then rotate the key!)"
