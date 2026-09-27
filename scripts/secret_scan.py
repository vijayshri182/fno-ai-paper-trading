"""WP-18 credential-hygiene scan for the paper-trading repo.

Checks that production entry points never accept credentials on the command
line and never embed credential *values* in source. Three checks run over
``src/`` and ``scripts/``:

* **A — CLI credential flags**: a ``--token`` / ``--client-secret`` argparse
  flag would put secrets in process listings and shell history; any occurrence
  is a hard failure.
* **B — hardcoded credential values**: a non-empty string literal assigned to
  an obvious credential key (``access_token``, ``client_secret``,
  ``api_secret``, ``api_key``, ``password``) leaks the secret to every reader
  of the source and VCS history.
* **C — dead credential arguments**: ``args.token`` / ``args.client_secret``
  references no longer backed by a CLI flag (and must come from env alone).

Tests under ``tests/`` are exempt: they intentionally hold fake secrets to
verify redaction. The scan is stdlib-only and runnable offline.

Exit code 0 when clean, 1 when any violation is found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("src", "scripts")

CLI_SECRET_FLAG = re.compile(r'"--(?:token|client-secret)"')
HARDCODED_SECRET = re.compile(
    r"\b(access_token|client_secret|api_secret|api_key|password)\s*=\s*\"" r'[^"\r\n]{4,}"'
)
DEAD_CRED_ARG = re.compile(r"\bargs\.(token|client_secret)\b")

EXEMPT_DEAD_CRED_DOC = "paper_track/runner.py"  # internal provider wiring, not an argparse arg
_THIS_TOOL = "secret_scan.py"  # the audit tool itself matches its own regexes


def scan_root(root: Path) -> tuple[list[str], list[Path], list[Path]]:
    """Returns (violations, scanned_files, clean_files)."""
    violations: list[str] = []
    scanned: list[Path] = []
    for dirname in SCAN_DIRS:
        target = root / dirname
        if not target.is_dir():
            continue
        for path in sorted(target.rglob("*.py")):
            if path.name.startswith("."):
                continue
            scanned.append(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            problems: list[str] = []

            flag_hits = sorted(set(CLI_SECRET_FLAG.findall(text)))
            if flag_hits:
                problems.append(
                    f"credential CLI flag {', '.join(flag_hits)} would leak secrets "
                    "into process listings; secrets must come from the environment only"
                )

            secret_hits = sorted(set(HARDCODED_SECRET.findall(text)))
            if secret_hits:
                problems.append(
                    f"hardcoded credential value(s) assigned to {', '.join(secret_hits)}"
                )

            if DEAD_CRED_ARG.search(text) and path.name not in (EXEMPT_DEAD_CRED_DOC, _THIS_TOOL):
                problems.append(
                    "args.token/args.client_secret reference without a CLI flag; "
                    "read credentials from the environment only"
                )

            for problem in problems:
                rel = path.relative_to(root).as_posix()
                violations.append(f"{rel}: {problem}")
    return violations, scanned, list(Path(p) for p in scanned)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)

    violations, scanned, _ = scan_root(Path(args.root))
    for line in violations:
        print(f"[credential-scan] VIOLATION: {line}")
    if violations:
        print(
            f"[credential-scan] {len(violations)} violation(s) across {len(scanned)} source files"
        )
        return 1
    print(f"[credential-scan] clean: no credential CLI flags or hardcoded values "
          f"in {len(scanned)} source files under {'/'.join(SCAN_DIRS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())