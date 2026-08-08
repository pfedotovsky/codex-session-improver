#!/usr/bin/env python3
"""Inspect allowed targets and freeze exact, host-bound proposal artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import sys
from pathlib import Path
from typing import Any

from improver_lib import (
    atomic_json,
    atomic_text,
    ensure_runtime,
    is_relative_to,
    iso,
    load_config,
    now,
    print_json,
    proposal_dir,
    read_json,
    redact_obj,
    runtime_dir,
    sha256_bytes,
    validate_target,
)
from remote_transport import HOST_ID_RE, inspect_remote_target, remote_hosts


CONTEXT_SURFACES = {
    "global-agents",
    "personal-skill",
    "project-agents",
    "project-skill",
    "project-docs",
}


def compatible_context_surfaces(config: dict[str, Any], host_id: str, raw_path: str) -> set[str]:
    path = Path(raw_path)
    normalized = path.as_posix()
    if host_id != "local":
        if "/skills/" in normalized:
            return {"personal-skill", "project-skill"}
        if path.name == "AGENTS.md":
            return {"global-agents", "project-agents"}
        return {"project-docs"}

    resolved = path.resolve(strict=False)
    home = Path.home().resolve()
    codex_home = Path(
        str(config.get("sessions_root", home / ".codex" / "sessions"))
    ).expanduser().resolve().parent
    if resolved == codex_home / "AGENTS.md":
        return {"global-agents"}
    if any(
        is_relative_to(resolved, root)
        for root in (codex_home / "skills", home / ".agents" / "skills")
    ):
        return {"personal-skill"}
    if resolved.name == "AGENTS.md":
        return {"project-agents"}
    if "/.agents/skills/" in normalized or "/.codex/skills/" in normalized:
        return {"project-skill"}
    return {"project-docs"}


def next_id(control_root: Path) -> str:
    prefix = f"P-{now().strftime('%Y%m%d')}-"
    existing = {path.name for path in (runtime_dir(control_root) / "proposals").glob(f"{prefix}*")}
    for number in range(1, 100):
        candidate = f"{prefix}{number:02d}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("Daily proposal ID space exhausted")


def target_snapshot(config: dict[str, Any], host_id: str, raw_path: str) -> dict[str, Any]:
    if host_id == "local":
        target = validate_target(Path(raw_path), config)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(f"Target must be a regular file: {target}")
            data = target.read_bytes()
            if len(data) > 250_000 or b"\x00" in data:
                raise RuntimeError(f"Target is too large or not text: {target}")
            return {"path": str(target), "exists": True, "content": data.decode("utf-8"), "sha256": sha256_bytes(data), "mode": target.stat().st_mode & 0o777}
        return {"path": str(target), "exists": False, "content": "", "sha256": None, "mode": 0o644}
    return inspect_remote_target(config, host_id, raw_path)


def unified_patch(host_id: str, path: str, before: str, after: str) -> str:
    label = path if host_id == "local" else f"{host_id}:{path}"
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=label,
            tofile=label,
            lineterm="\n",
        )
    )


def derived_validations(host_id: str, paths: list[str]) -> list[dict[str, str]]:
    validations = [{"kind": "utf8-and-nul-check", "description": "All desired files are UTF-8 text without NUL bytes."}]
    skill_roots: set[str] = set()
    for raw in paths:
        path = Path(raw)
        parts = list(path.parts)
        if "skills" in parts:
            index = len(parts) - 1 - parts[::-1].index("skills")
            if index + 1 < len(parts):
                skill_roots.add(str(Path(*parts[: index + 2])))
    for root in sorted(skill_roots):
        validations.append({"kind": "skill-validate", "path": root, "description": f"Validate skill metadata on {host_id}: {root}"})
    validations.append({"kind": "git-diff-check", "description": f"Run git diff --check on {host_id} for changed repository files."})
    return validations


def inspect_target(args: argparse.Namespace) -> int:
    control_root = args.control_root.resolve()
    config = load_config(control_root)
    if args.host != "local" and args.host not in remote_hosts(config):
        raise RuntimeError(f"Unknown remote host: {args.host}")
    print_json({"host": args.host, **target_snapshot(config, args.host, args.path)})
    return 0


def create(args: argparse.Namespace) -> int:
    control_root = args.control_root.resolve()
    config = load_config(control_root)
    ensure_runtime(control_root)
    draft_path = args.draft.resolve()
    if not draft_path.is_relative_to(control_root):
        raise RuntimeError("Draft must be inside the control project")
    draft = read_json(draft_path)
    if not isinstance(draft, dict):
        raise RuntimeError("Draft must be a JSON object")
    required = (
        "summary",
        "root_cause",
        "evidence",
        "source_session_ids",
        "risk",
        "rollback",
        "context_surface",
        "placement_reason",
        "changes",
    )
    missing = [key for key in required if key not in draft]
    if missing:
        raise RuntimeError(f"Draft is missing fields: {', '.join(missing)}")
    host_id = str(draft.get("target_host", "local"))
    if host_id != "local" and host_id not in remote_hosts(config):
        raise RuntimeError(f"Unknown remote host: {host_id}")
    changes = draft.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 8:
        raise RuntimeError("Draft must contain one to eight changes")
    if not isinstance(draft.get("evidence"), list) or not draft["evidence"]:
        raise RuntimeError("Draft evidence must be a non-empty list")
    if not isinstance(draft.get("source_session_ids"), list) or not draft["source_session_ids"]:
        raise RuntimeError("Draft source_session_ids must be a non-empty list")
    feedback_scope = str(draft.get("feedback_scope", "host-specific"))
    if feedback_scope not in {"host-specific", "general"}:
        raise RuntimeError("feedback_scope must be host-specific or general")
    context_surface = draft.get("context_surface")
    if not isinstance(context_surface, str) or context_surface not in CONTEXT_SURFACES:
        raise RuntimeError(
            "context_surface must be one of: " + ", ".join(sorted(CONTEXT_SURFACES))
        )
    placement_reason = draft.get("placement_reason")
    if not isinstance(placement_reason, str) or not placement_reason.strip():
        raise RuntimeError("placement_reason must explain why this is the smallest effective context surface")
    raw_source_hosts = draft.get("source_hosts")
    if raw_source_hosts is None:
        raw_source_hosts = [str(value).split(":", 1)[0] for value in draft["source_session_ids"] if ":" in str(value)]
        if not raw_source_hosts:
            raw_source_hosts = ["local"]
    if not isinstance(raw_source_hosts, list) or not raw_source_hosts:
        raise RuntimeError("source_hosts must identify at least one evidence host")
    source_hosts = list(dict.fromkeys(str(value) for value in raw_source_hosts))
    if any(value != "local" and not HOST_ID_RE.fullmatch(value) for value in source_hosts):
        raise RuntimeError("source_hosts contains an invalid host ID")
    date_prefix = f"P-{now().strftime('%Y%m%d')}-"
    if len(list((runtime_dir(control_root) / "proposals").glob(f"{date_prefix}*"))) >= int(config.get("max_proposals_per_run", 3)):
        raise RuntimeError("Daily proposal cap reached")

    proposal_id = next_id(control_root)
    directory = proposal_dir(control_root, proposal_id)
    content_dir = directory / "files"
    content_dir.mkdir(parents=True, exist_ok=False)
    manifest_changes: list[dict[str, Any]] = []
    patches: list[str] = []
    target_paths: list[str] = []
    seen: set[str] = set()
    for index, change in enumerate(changes, 1):
        if not isinstance(change, dict) or not isinstance(change.get("path"), str) or not isinstance(change.get("new_content"), str):
            raise RuntimeError("Each change requires string path and new_content")
        snapshot = target_snapshot(config, host_id, change["path"])
        target = str(snapshot["path"])
        compatible_surfaces = compatible_context_surfaces(config, host_id, target)
        if context_surface not in compatible_surfaces:
            raise RuntimeError(
                f"context_surface {context_surface} does not match target {target}; "
                f"expected one of: {', '.join(sorted(compatible_surfaces))}"
            )
        if target in seen:
            raise RuntimeError(f"Duplicate target: {target}")
        seen.add(target)
        new_content = change["new_content"]
        encoded = new_content.encode("utf-8")
        if b"\x00" in encoded or len(encoded) > 250_000:
            raise RuntimeError(f"Target content is invalid or too large: {target}")
        before = str(snapshot["content"])
        if before == new_content:
            raise RuntimeError(f"Proposal makes no change: {target}")
        content_file = content_dir / f"{index:03d}.txt"
        atomic_text(content_file, new_content)
        patches.append(unified_patch(host_id, target, before, new_content))
        manifest_changes.append({
            "path": target,
            "operation": "update" if snapshot["exists"] else "create",
            "base_sha256": snapshot["sha256"],
            "desired_sha256": sha256_bytes(encoded),
            "desired_content_file": str(content_file.relative_to(directory)),
            "mode": int(snapshot["mode"]),
        })
        target_paths.append(target)

    created = now()
    manifest = {
        "schema_version": 3,
        "id": proposal_id,
        "status": "pending",
        "target_host": host_id,
        "feedback_scope": feedback_scope,
        "context_surface": context_surface,
        "placement_reason": redact_obj(placement_reason, 1500),
        "source_hosts": source_hosts,
        "transfer_directions": [f"{source}->{host_id}" for source in source_hosts],
        "created_at": iso(created),
        "expiry_at": iso(created + dt.timedelta(days=int(config.get("proposal_expiry_days", 14)))),
        "summary": str(draft["summary"])[:500],
        "root_cause": str(draft["root_cause"])[:2000],
        "evidence": redact_obj(draft["evidence"], 1500),
        "source_session_ids": [str(value)[:250] for value in draft["source_session_ids"][:30]],
        "risk": str(draft["risk"])[:1500],
        "rollback": str(draft["rollback"])[:1500],
        "changes": manifest_changes,
        "validation_commands": derived_validations(host_id, target_paths),
        "patch_file": "change.patch",
    }
    atomic_text(directory / "change.patch", "\n".join(patches))
    atomic_json(directory / "manifest.json", manifest)
    draft_path.unlink()
    print_json(manifest)
    return 0


def list_proposals(args: argparse.Namespace) -> int:
    control_root = args.control_root.resolve()
    ensure_runtime(control_root)
    values = []
    for path in sorted((runtime_dir(control_root) / "proposals").glob("P-*/manifest.json")):
        value = read_json(path)
        if isinstance(value, dict) and (not args.status or value.get("status") == args.status):
            values.append(value)
    print_json({"proposals": values})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--control-root", type=Path, required=True)
    inspect_parser.add_argument("--host", default="local")
    inspect_parser.add_argument("--path", required=True)
    inspect_parser.set_defaults(handler=inspect_target)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--control-root", type=Path, required=True)
    create_parser.add_argument("--draft", type=Path, required=True)
    create_parser.set_defaults(handler=create)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--control-root", type=Path, required=True)
    list_parser.add_argument("--status")
    list_parser.set_defaults(handler=list_proposals)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
