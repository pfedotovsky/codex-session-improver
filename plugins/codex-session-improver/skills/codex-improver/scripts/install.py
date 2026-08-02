#!/usr/bin/env python3
"""Install or upgrade a private Codex improver control project."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any


ENGINE_FILES = (
    "apply_proposals.py",
    "diagnose.py",
    "hook_dispatch.py",
    "host_discovery.py",
    "improver_lib.py",
    "proposal_tool.py",
    "remote_transport.py",
    "remote_worker.py",
    "session_batch.py",
    "validate_skill.py",
)
RUNTIME_DIRS = ("batches", "findings", "proposals", "approvals", "backups", "runs", "drafts")


def atomic_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def safe_root(value: Path, label: str) -> Path:
    root = value.expanduser().resolve(strict=False)
    home = Path.home().resolve()
    if root in {Path("/"), home}:
        raise ValueError(f"{label} must be narrower than {root}")
    return root


def unique_paths(values: list[Path]) -> list[Path]:
    result: list[Path] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def toml_string(value: Path | str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_config_toml(writable_roots: list[Path], network_access: bool) -> str:
    roots = "\n".join(f"  {toml_string(root)}," for root in writable_roots)
    network = "true" if network_access else "false"
    return (
        'sandbox_mode = "workspace-write"\n'
        "allow_login_shell = false\n\n"
        "[sandbox_workspace_write]\n"
        f"network_access = {network}\n"
        "writable_roots = [\n"
        f"{roots}\n"
        "]\n"
    )


def render_hooks(python: Path, control_root: Path) -> str:
    command = " ".join(
        shlex.quote(value)
        for value in (
            str(python),
            str(control_root / "libexec" / "hook_dispatch.py"),
            "pre-tool",
            "--control-root",
            str(control_root),
        )
    )
    return json_text(
        {
            "description": "Verify exact Codex improver approvals and block unapproved external writes.",
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": command,
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            },
        }
    )


def backup_managed_files(control_root: Path) -> Path | None:
    candidates = (
        control_root / "AGENTS.md",
        control_root / ".gitignore",
        control_root / "automation-prompt.md",
        control_root / ".codex" / "hooks.json",
        control_root / ".codex" / "config.toml",
    )
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = control_root / "runtime" / "installer-backups" / stamp
    for path in existing:
        destination = backup / path.relative_to(control_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return backup


def install_engine(source_scripts: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ENGINE_FILES:
        source = source_scripts / name
        if not source.is_file():
            raise FileNotFoundError(f"Missing bundled engine file: {source}")
        data = source.read_bytes()
        temporary = destination / f".{name}.{os.getpid()}.tmp"
        temporary.write_bytes(data)
        os.chmod(temporary, 0o755 if data.startswith(b"#!") else 0o644)
        os.replace(temporary, destination / name)


def merge_gitignore(path: Path, template: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    for line in template.splitlines():
        if line and line not in lines:
            lines.append(line)
    atomic_text(path, "\n".join(lines).rstrip() + "\n")


def install_standalone_skill(skill_root: Path, target: Path, upgrade: bool) -> Path | None:
    backup: Path | None = None
    if target.exists() or target.is_symlink():
        if not upgrade:
            raise FileExistsError(f"Standalone skill already exists: {target}; use --upgrade")
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError(f"Refusing to replace non-directory skill path: {target}")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target.parent.parent / "skill-backups" / f"codex-improver-{stamp}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target, backup)
    staged = target.parent / f".codex-improver.{os.getpid()}.tmp"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(
        skill_root,
        staged,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "tests"),
    )
    if target.exists():
        shutil.rmtree(target)
    os.replace(staged, target)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-root", type=Path, default=Path.home() / "projects" / "codex-improver")
    parser.add_argument("--project-root", action="append", type=Path, default=[])
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--no-remote", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--install-standalone-skill", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9 or newer is required")

    skill_root = Path(__file__).resolve().parent.parent
    templates = skill_root / "assets" / "control-project"
    control_root = safe_root(args.control_root, "control root")
    codex_home = safe_root(
        args.codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
        "Codex home",
    )
    project_values = args.project_root or [Path.home() / "projects"]
    project_roots = unique_paths([safe_root(value, "project root") for value in project_values])
    writable_roots = unique_paths([codex_home, control_root, *project_roots])
    config_path = control_root / "config.json"
    existing_config: dict[str, Any] | None = None
    if config_path.is_file():
        existing_config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(existing_config, dict):
            raise RuntimeError(f"Invalid existing config: {config_path}")
        if not args.upgrade:
            raise FileExistsError(f"Control project already exists: {control_root}; use --upgrade")

    managed_writable_roots = writable_roots
    managed_network_access = not args.no_remote
    if existing_config is not None:
        configured_roots = existing_config.get("writable_roots", [])
        if not isinstance(configured_roots, list) or not configured_roots:
            raise RuntimeError("Existing config has no writable_roots")
        managed_writable_roots = unique_paths(
            [safe_root(Path(str(value)), "configured writable root") for value in configured_roots]
        )
        discovery = existing_config.get("remote_auto_discovery", {})
        managed_network_access = bool(
            (isinstance(discovery, dict) and discovery.get("enabled"))
            or existing_config.get("remote_hosts")
        )

    plan = {
        "control_root": str(control_root),
        "codex_home": str(codex_home),
        "project_roots": [str(path) for path in project_roots],
        "remote_auto_discovery": not args.no_remote,
        "upgrade": args.upgrade,
        "standalone_skill": args.install_standalone_skill,
    }
    if args.dry_run:
        print(json_text({"status": "dry-run", **plan}), end="")
        return 0

    control_root.mkdir(parents=True, exist_ok=True)
    backup = backup_managed_files(control_root) if args.upgrade else None
    for name in RUNTIME_DIRS:
        (control_root / "runtime" / name).mkdir(parents=True, exist_ok=True)
    install_engine(skill_root / "scripts", control_root / "libexec")

    if existing_config is None:
        config = {
            "schema_version": 3,
            "control_root": str(control_root),
            "sessions_root": str(codex_home / "sessions"),
            "bootstrap_days": 7,
            "settle_seconds": 300,
            "retention_days": 90,
            "proposal_expiry_days": 14,
            "approval_expiry_minutes": 10,
            "max_sessions_per_run": 8,
            "max_proposals_per_run": 3,
            "writable_roots": [str(path) for path in writable_roots],
            "excluded_cwds": [str(control_root)],
            "remote_hosts": [],
            "remote_auto_discovery": {
                "enabled": not args.no_remote,
                "ssh_config": str(Path.home() / ".ssh" / "config"),
                "app_state": str(codex_home / ".codex-global-state.json"),
                "python_command": "python3",
                "max_hosts": 20,
                "probe_timeout_seconds": 12,
            },
        }
        atomic_text(config_path, json_text(config), 0o600)
    else:
        configured_root = Path(str(existing_config.get("control_root", ""))).expanduser().resolve(strict=False)
        if configured_root != control_root:
            raise RuntimeError("Existing config belongs to another control root")

    atomic_text(control_root / "AGENTS.md", (templates / "AGENTS.md").read_text(encoding="utf-8"))
    merge_gitignore(control_root / ".gitignore", (templates / "gitignore").read_text(encoding="utf-8"))
    atomic_text(
        control_root / "automation-prompt.md",
        (templates / "automation-prompt.md").read_text(encoding="utf-8"),
    )
    atomic_text(control_root / ".codex" / "hooks.json", render_hooks(Path(sys.executable).resolve(), control_root))
    atomic_text(
        control_root / ".codex" / "config.toml",
        render_config_toml(managed_writable_roots, network_access=managed_network_access),
    )

    skill_target: Path | None = None
    skill_backup: Path | None = None
    if args.install_standalone_skill:
        skill_target = Path.home() / ".agents" / "skills" / "codex-improver"
        skill_target.parent.mkdir(parents=True, exist_ok=True)
        skill_backup = install_standalone_skill(skill_root, skill_target, args.upgrade)

    result = {
        "status": "installed" if existing_config is None else "upgraded",
        **plan,
        "hook_trust_required": True,
        "automation_prompt": str(control_root / "automation-prompt.md"),
        "managed_backup": str(backup) if backup else None,
        "standalone_skill_path": str(skill_target) if skill_target else None,
        "standalone_skill_backup": str(skill_backup) if skill_backup else None,
    }
    print(json_text(result), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json_text({"status": "error", "error": str(exc)}), file=sys.stderr, end="")
        raise SystemExit(1)
