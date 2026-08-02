#!/usr/bin/env python3
"""Install Codex Session Improver from a repository clone."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


repository = Path(__file__).resolve().parent.parent
installer = (
    repository
    / "plugins"
    / "codex-session-improver"
    / "skills"
    / "codex-improver"
    / "scripts"
    / "install.py"
)
sys.argv = [str(installer), "--install-standalone-skill", *sys.argv[1:]]
runpy.run_path(str(installer), run_name="__main__")
