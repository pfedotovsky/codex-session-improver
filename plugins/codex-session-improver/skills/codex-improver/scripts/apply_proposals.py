#!/usr/bin/env python3
"""Apply explicitly selected frozen proposals with stale checks and rollback."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from host_discovery import sync_discovered_hosts
from improver_lib import (
    atomic_json,
    atomic_text,
    ensure_runtime,
    find_git_root,
    iso,
    load_config,
    load_manifest,
    now,
    parse_iso,
    print_json,
    proposal_dir,
    read_json,
    runtime_dir,
    sha256_bytes,
    validate_target,
)
from remote_transport import call_remote, remote_hosts


SKILL_VALIDATE = Path(__file__).resolve().with_name("validate_skill.py")


def current_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        return "INVALID"
    return sha256_bytes(path.read_bytes())


def backup_targets(control_root: Path, proposal_id: str, changes: list[dict]) -> Path:
    root = runtime_dir(control_root) / "backups" / proposal_id
    root.mkdir(parents=True, exist_ok=False)
    metadata = []
    for index, change in enumerate(changes, 1):
        target = Path(change["path"])
        entry = {"path": str(target), "existed": target.exists(), "mode": change.get("mode", 0o644)}
        if target.exists():
            backup = root / f"{index:03d}.bin"
            shutil.copyfile(target, backup)
            entry["backup"] = backup.name
        metadata.append(entry)
    atomic_json(root / "metadata.json", metadata)
    return root


def restore_backup(backup_root: Path) -> list[str]:
    metadata = read_json(backup_root / "metadata.json", [])
    restored: list[str] = []
    for entry in reversed(metadata):
        target = Path(entry["path"])
        if entry.get("existed"):
            data = (backup_root / entry["backup"]).read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.rollback")
            tmp.write_bytes(data)
            os.chmod(tmp, int(entry.get("mode", 0o644)))
            os.replace(tmp, target)
        else:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        restored.append(str(target))
    return restored


def run_validations(changes: list[dict], proposal_directory: Path) -> list[dict]:
    results: list[dict] = []
    skill_roots: set[Path] = set()
    git_groups: dict[Path, list[Path]] = {}
    for change in changes:
        target = Path(change["path"])
        data = target.read_bytes()
        try:
            data.decode("utf-8")
            ok = b"\x00" not in data
        except UnicodeDecodeError:
            ok = False
        results.append({"kind": "utf8-and-nul-check", "path": str(target), "ok": ok})
        normalized = target.as_posix()
        marker = "/skills/"
        if marker in normalized:
            prefix, suffix = normalized.split(marker, 1)
            skill_name = suffix.split("/", 1)[0]
            if skill_name and skill_name != ".system":
                skill_roots.add(Path(prefix + marker + skill_name))
        git_root = find_git_root(target)
        if git_root:
            git_groups.setdefault(git_root, []).append(target)
    for root in sorted(skill_roots):
        result = subprocess.run(
            [sys.executable, str(SKILL_VALIDATE), str(root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        results.append(
            {
                "kind": "skill-validate",
                "path": str(root),
                "ok": result.returncode == 0,
                "output": (result.stdout + result.stderr)[-2000:],
            }
        )
    for root, targets in git_groups.items():
        relative = [str(path.relative_to(root)) for path in targets]
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--check", "--", *relative],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        results.append(
            {
                "kind": "git-diff-check",
                "path": str(root),
                "ok": result.returncode == 0,
                "output": (result.stdout + result.stderr)[-2000:],
            }
        )
    return results


def apply_one(control_root: Path, config: dict, proposal_id: str) -> dict:
    manifest_path, manifest = load_manifest(control_root, proposal_id)
    if manifest.get("status") != "pending":
        return {"id": proposal_id, "status": "failed", "reason": f"Proposal status is {manifest.get('status')}"}
    if parse_iso(manifest["expiry_at"]) <= now():
        manifest.update({"status": "stale", "stale_at": iso(), "failure_reason": "Proposal expired"})
        atomic_json(manifest_path, manifest)
        return {"id": proposal_id, "status": "stale", "reason": "Proposal expired"}

    host_id = str(manifest.get("target_host", "local"))
    if host_id != "local":
        if host_id not in remote_hosts(config, include_unavailable=True):
            manifest.update({"status": "stale", "stale_at": iso(), "failure_reason": f"Remote host is no longer configured or discoverable: {host_id}"})
            atomic_json(manifest_path, manifest)
            return {"id": proposal_id, "status": "stale", "reason": f"Remote host is no longer configured or discoverable: {host_id}"}
        directory = proposal_dir(control_root, proposal_id)
        frozen_changes = []
        for change in manifest.get("changes", []):
            desired_file = directory / change["desired_content_file"]
            desired = desired_file.read_text(encoding="utf-8")
            if sha256_bytes(desired.encode("utf-8")) != change["desired_sha256"]:
                manifest.update({"status": "failed", "failed_at": iso(), "failure_reason": "Frozen desired content hash mismatch"})
                atomic_json(manifest_path, manifest)
                return {"id": proposal_id, "status": "failed", "reason": "Frozen desired content hash mismatch"}
            frozen_changes.append({
                "path": change["path"],
                "base_sha256": change.get("base_sha256"),
                "desired_sha256": change["desired_sha256"],
                "desired_content": desired,
                "mode": int(change.get("mode", 0o644)),
            })
        manifest.update({"status": "approved", "approved_at": iso()})
        atomic_json(manifest_path, manifest)
        try:
            response = call_remote(
                config,
                host_id,
                "apply",
                {"proposal_id": proposal_id, "changes": frozen_changes},
                timeout=120,
            )
        except Exception as exc:
            manifest.update({"status": "pending", "last_apply_error": str(exc)[:1000], "last_apply_error_at": iso()})
            atomic_json(manifest_path, manifest)
            return {"id": proposal_id, "status": "pending", "reason": str(exc), "retryable": True}
        remote_status = str(response.get("status"))
        if remote_status == "stale":
            manifest.update({"status": "stale", "stale_at": iso(), "failure_reason": response.get("reason")})
        elif remote_status == "failed":
            manifest.update({
                "status": "failed",
                "failed_at": iso(),
                "failure_reason": response.get("reason"),
                "rolled_back": bool(response.get("rolled_back")),
                "restored": response.get("restored", []),
                "validation_results": response.get("validation", []),
                "backup": response.get("backup"),
            })
        elif remote_status == "applied":
            manifest.update({
                "status": "applied",
                "applied_at": response.get("applied_at", iso()),
                "validation_results": response.get("validation", []),
                "backup": response.get("backup"),
            })
        else:
            manifest.update({"status": "failed", "failed_at": iso(), "failure_reason": "Invalid remote apply response"})
            remote_status = "failed"
        atomic_json(manifest_path, manifest)
        return {
            "id": proposal_id,
            "host": host_id,
            "status": remote_status,
            "reason": response.get("reason"),
            "rolled_back": response.get("rolled_back"),
            "changes": response.get("changes", []),
            "validation": response.get("validation", []),
        }

    changes = manifest.get("changes", [])
    for change in changes:
        target = validate_target(Path(change["path"]), config)
        observed = current_hash(target)
        if observed != change.get("base_sha256"):
            manifest.update(
                {
                    "status": "stale",
                    "stale_at": iso(),
                    "failure_reason": f"Base hash changed: {target}",
                }
            )
            atomic_json(manifest_path, manifest)
            return {"id": proposal_id, "status": "stale", "reason": f"Base hash changed: {target}"}

    manifest.update({"status": "approved", "approved_at": iso()})
    atomic_json(manifest_path, manifest)
    backup = backup_targets(control_root, proposal_id, changes)
    directory = proposal_dir(control_root, proposal_id)
    try:
        for change in changes:
            target = validate_target(Path(change["path"]), config)
            desired_file = directory / change["desired_content_file"]
            desired = desired_file.read_text(encoding="utf-8")
            if sha256_bytes(desired.encode("utf-8")) != change["desired_sha256"]:
                raise RuntimeError(f"Frozen desired content hash mismatch: {target}")
            atomic_text(target, desired, int(change.get("mode", 0o644)))
        validations = run_validations(changes, directory)
        if not all(item.get("ok") for item in validations):
            raise RuntimeError("Validation failed")
    except Exception as exc:
        restored = restore_backup(backup)
        manifest.update(
            {
                "status": "failed",
                "failed_at": iso(),
                "failure_reason": str(exc),
                "rolled_back": True,
                "restored": restored,
                "validation_results": locals().get("validations", []),
            }
        )
        atomic_json(manifest_path, manifest)
        return {"id": proposal_id, "status": "failed", "reason": str(exc), "rolled_back": True}

    manifest.update({"status": "applied", "applied_at": iso(), "validation_results": validations, "backup": str(backup)})
    atomic_json(manifest_path, manifest)
    return {"id": proposal_id, "status": "applied", "changes": [item["path"] for item in changes], "validation": validations}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--proposal-id", action="append", required=True)
    args = parser.parse_args()
    control_root = args.control_root.resolve()
    config = load_config(control_root)
    ensure_runtime(control_root)
    sync_discovered_hosts(control_root, config)
    ids = list(dict.fromkeys(args.proposal_id))
    if len(ids) > int(config.get("max_proposals_per_run", 3)):
        raise RuntimeError("Too many proposals in one application request")
    for proposal_id in ids:
        load_manifest(control_root, proposal_id)

    results = []
    for proposal_id in ids:
        results.append(apply_one(control_root, config, proposal_id))
    print_json({"status": "complete", "results": results})
    return 0 if all(item["status"] == "applied" for item in results) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
