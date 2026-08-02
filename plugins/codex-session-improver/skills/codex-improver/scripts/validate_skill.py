#!/usr/bin/env python3
"""Small self-contained validator for approved skill changes."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    path = root / "SKILL.md"
    if not path.is_file():
        return [f"Missing {path}"]
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{path} is not UTF-8"]
    if not text.startswith("---\n"):
        return ["SKILL.md must start with YAML frontmatter"]
    end = text.find("\n---\n", 4)
    if end < 0:
        return ["SKILL.md frontmatter is not closed"]
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip().strip('"\'')
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not NAME_RE.fullmatch(name):
        errors.append("name must use lower-case hyphen-case")
    if root.name != name:
        errors.append(f"skill folder {root.name!r} does not match name {name!r}")
    if not description:
        errors.append("description is required")
    if set(fields) - {"name", "description"}:
        errors.append("frontmatter may contain only name and description")
    if not text[end + 5 :].strip():
        errors.append("skill body is empty")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("skill_root", type=Path)
    args = parser.parse_args()
    errors = validate(args.skill_root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Skill is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
