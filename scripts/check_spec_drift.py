"""
scripts/check_spec_drift.py

Detect drift between OpenSpec specs and the codebase.

For each spec at openspec/specs/<cap>/spec.md, verify two things:

1. **Path references** — every `src/.../py` or `scripts/.../py` path that the
   spec quotes must still exist on disk. If a file is renamed or deleted
   without updating the spec, this catches it.

2. **Symbol references** (identifier check) — every backtick-quoted Python
   identifier (e.g. `should_continue`, `_split_qa.cache_clear`,
   `STANCE_SCORE`) must be defined SOMEWHERE under src/ or scripts/ as a
   `def`/`class`/constant. Pure stdlib / 3rd-party names (e.g. `lru_cache`,
   `bigquery.Client`) are accepted as long as they appear in the source as
   call sites.

This is intentionally simple: greps + path checks. Goal is to catch
"renamed/deleted but spec not updated" cases. False positives on identifier
checks are warnings (informational), not errors — only missing paths fail CI.

Exit code:
  0 = no path drift
  1 = at least one referenced path missing
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = ROOT / "openspec" / "specs"
SRC_DIRS = [ROOT / "src", ROOT / "scripts"]

# `src/x.py` or `scripts/x.py` mentioned anywhere in the spec body
PATH_RE = re.compile(r"(?:src|scripts)/[a-zA-Z0-9_/]+\.py")

# Backtick-quoted Python-style identifier: `name`, `name.attr`, `name()`,
# `name.method()`. Excludes ones with spaces (which are usually prose).
IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)\(?\)?`")


def _collect_source_text() -> str:
    """Concatenate all .py files under src/ and scripts/ into one big string.

    Cheap and good enough for substring search — we only ask whether an
    identifier appears anywhere in the source tree, not in a specific way.
    """
    chunks: list[str] = []
    for src_dir in SRC_DIRS:
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            try:
                chunks.append(py_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(chunks)


def _looks_like_spec_concept(ident: str) -> bool:
    """Heuristic: identifiers that look like JSON / state keys, not Python symbols.

    Specs use backticks to wrap many things — state keys (`confidence`),
    JSON fields (`stance_change`), prose tokens (`true`). Anything that
    isn't recognised as a Python symbol is reported only as a warning,
    not a hard error.
    """
    if not ident or ident.startswith((".", "_")) and len(ident) <= 2:
        return True
    # Single lowercase word with no dots is most often a state/JSON key
    if "." not in ident and ident.islower() and "_" not in ident:
        return True
    # Common Gherkin keywords
    return ident in {"GIVEN", "WHEN", "THEN", "AND", "MUST", "SHALL", "MAY"}


def main() -> int:
    if not SPECS_DIR.exists():
        print(f"No specs directory at {SPECS_DIR}")
        return 0

    spec_files = sorted(SPECS_DIR.glob("*/spec.md"))
    if not spec_files:
        print(f"No specs found under {SPECS_DIR}")
        return 0

    source_text = _collect_source_text()

    path_errors: list[str] = []
    ident_warnings: list[str] = []

    for spec_path in spec_files:
        cap = spec_path.parent.name
        text = spec_path.read_text(encoding="utf-8")

        # 1. Path references must exist on disk
        for path_str in sorted(set(PATH_RE.findall(text))):
            if not (ROOT / path_str).exists():
                path_errors.append(f"{cap}: missing referenced path '{path_str}'")

        # 2. Identifier references should appear somewhere in source
        for raw in sorted(set(IDENT_RE.findall(text))):
            if _looks_like_spec_concept(raw):
                continue
            # Check the leaf name only (e.g. `client.query` → `query`)
            leaf = raw.split(".")[-1]
            if leaf in source_text:
                continue
            # Also accept the full dotted form (e.g. `bigquery.Client`)
            if raw in source_text:
                continue
            ident_warnings.append(
                f"{cap}: `{raw}` not found anywhere under src/ or scripts/"
            )

    # Report ───────────────────────────────────────────────────────────────
    if path_errors:
        print("❌ Spec drift — broken path references:")
        for err in path_errors:
            print(f"  - {err}")
        print()

    if ident_warnings:
        # Warnings are informational. Many will be false positives because
        # the regex catches every backtick-quoted token, including state
        # keys. The signal-to-noise ratio is too low to fail CI on.
        print(f"⚠  Identifier warnings ({len(ident_warnings)} — informational, "
              "may include false positives):")
        for w in ident_warnings[:15]:
            print(f"  - {w}")
        if len(ident_warnings) > 15:
            print(f"  ... and {len(ident_warnings) - 15} more")
        print()

    if path_errors:
        print(f"❌ {len(path_errors)} broken path reference(s). Fix the spec "
              "(or the rename) and rerun.")
        return 1

    print(f"✅ Spec drift check passed: {len(spec_files)} specs, all "
          "src/scripts path references valid.")
    if ident_warnings:
        print(f"   ({len(ident_warnings)} identifier warnings, not blocking)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
