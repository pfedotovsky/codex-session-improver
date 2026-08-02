#!/usr/bin/env python3
"""Reject private machine data, likely secrets, and runtime artifacts."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
IGNORED_PARTS = {".git", ".validation-venv", "__pycache__", ".venv"}
TEXT_SUFFIXES = {"", ".md", ".py", ".json", ".toml", ".yaml", ".yml", ".txt"}
PRIVATE_HOME_RE = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")
PLACEHOLDER_USERS = {"example", "me", "test", "user"}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def main() -> int:
    failures: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if "runtime" in relative.parts and path.is_file():
            failures.append(f"runtime artifact: {relative}")
            continue
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 2_000_000:
            failures.append(f"oversized text file: {relative}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in PRIVATE_HOME_RE.finditer(text):
            if match.group(1) not in PLACEHOLDER_USERS:
                failures.append(f"machine-specific home path in {relative}: {match.group(0)}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"likely secret in {relative}: {pattern.pattern}")
    if failures:
        print("\n".join(failures))
        return 1
    print("Public-tree scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
