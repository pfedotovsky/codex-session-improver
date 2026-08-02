#!/usr/bin/env python3
"""Discover Codex SSH hosts from concrete OpenSSH aliases and saved remote projects."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from improver_lib import atomic_json, iso, load_config, now, print_json, read_json, runtime_dir
from remote_transport import HOST_ID_RE, SSH_TARGET_RE, configured_remote_hosts


PROBE_CODE = """import json,sys
from pathlib import Path
r=json.load(sys.stdin)
h=Path.home().resolve()
s=h/'.codex'/'sessions'
state=h/'.codex'/'.codex-global-state.json'
environment_id=''
try:
    if state.is_file(): environment_id=str(json.loads(state.read_text()).get('electron-local-remote-control-environment-id') or '')
except (OSError,ValueError): pass
matched=[]
for raw in r.get('project_paths',[]):
    try:
        p=Path(raw)
        if p.is_absolute() and p.exists(): matched.append(raw)
    except OSError: pass
print(json.dumps({'home':str(h),'sessions_root':str(s),'sessions_readable':s.is_dir(),'remote_environment_id':environment_id,'matched_project_paths':matched},separators=(',',':')))
"""
SSH = shutil.which("ssh") or "/usr/bin/ssh"


def discovery_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("remote_auto_discovery", {})
    return raw if isinstance(raw, dict) else {}


def concrete_aliases(config_path: Path, max_files: int = 50) -> list[str]:
    aliases: list[str] = []
    visited: set[Path] = set()

    def visit(path: Path) -> None:
        if len(visited) >= max_files:
            return
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError:
            return
        if resolved in visited or not resolved.is_file():
            return
        visited.add(resolved)
        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if not parts:
                continue
            keyword = parts[0].lower()
            if keyword == "include":
                for pattern in parts[1:]:
                    candidate = Path(pattern).expanduser()
                    if not candidate.is_absolute():
                        candidate = Path.home() / ".ssh" / candidate
                    for match in sorted(glob.glob(str(candidate))):
                        visit(Path(match))
            elif keyword == "host":
                for alias in parts[1:]:
                    if (
                        SSH_TARGET_RE.fullmatch(alias)
                        and not alias.startswith(("-", "!"))
                        and not any(char in alias for char in "*?[]")
                        and alias not in aliases
                    ):
                        aliases.append(alias)

    visit(config_path)
    return aliases


def saved_remote_projects(app_state_path: Path) -> list[dict[str, str]]:
    value = read_json(app_state_path, {})
    if not isinstance(value, dict):
        return []
    raw_projects = value.get("remote-projects")
    if not isinstance(raw_projects, list):
        atom = value.get("electron-persisted-atom-state")
        raw_projects = atom.get("remote-projects", []) if isinstance(atom, dict) else []
    if not isinstance(raw_projects, list):
        return []
    projects: list[dict[str, str]] = []
    for raw in raw_projects[:200]:
        if not isinstance(raw, dict):
            continue
        path = raw.get("remotePath") or raw.get("path")
        host_id = raw.get("hostId")
        label = raw.get("label")
        if isinstance(path, str) and path.startswith("/"):
            projects.append({
                "path": path[:2000],
                "app_host_id": str(host_id or "")[:200],
                "label": str(label or "")[:200],
            })
    return projects


def resolve_alias(alias: str) -> dict[str, str]:
    result = subprocess.run(
        [SSH, "-G", alias],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("OpenSSH could not resolve the alias")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in {"hostname", "user", "port"} and value:
            fields[key] = value.strip()
    if not all(key in fields for key in ("hostname", "user", "port")):
        raise RuntimeError("OpenSSH resolution was incomplete")
    return fields


def endpoint_key(fields: dict[str, str]) -> str:
    raw = "\0".join(fields[key] for key in ("hostname", "user", "port"))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def probe(alias: str, project_paths: list[str], timeout: int, python_command: str = "python3") -> dict[str, Any]:
    remote_command = " ".join(shlex.quote(value) for value in (python_command, "-c", PROBE_CODE))
    result = subprocess.run(
        [
            SSH,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={timeout}",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ForwardAgent=no",
            alias,
            remote_command,
        ],
        input=json.dumps({"project_paths": project_paths}, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout + 8,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"exit {result.returncode}"
        raise RuntimeError(detail[:400])
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Probe returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Probe returned a non-object")
    return value


def cache_path(control_root: Path) -> Path:
    return runtime_dir(control_root) / "discovered-hosts.json"


def sync_discovered_hosts(control_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    settings = discovery_config(config)
    if not settings.get("enabled", False):
        return {"enabled": False, "discovered": [], "configured_matches": [], "errors": []}
    ssh_config = Path(str(settings.get("ssh_config", Path.home() / ".ssh" / "config"))).expanduser()
    app_state = Path(str(settings.get("app_state", Path.home() / ".codex" / ".codex-global-state.json"))).expanduser()
    max_hosts = min(max(int(settings.get("max_hosts", 20)), 1), 50)
    timeout = min(max(int(settings.get("probe_timeout_seconds", 12)), 3), 30)
    python_command = str(settings.get("python_command", "python3"))
    if not re.fullmatch(r"(?:/[A-Za-z0-9._/-]{1,1023}|python3(?:\.[0-9]+)?)", python_command) or "/../" in python_command:
        raise RuntimeError("Invalid remote_auto_discovery.python_command")
    aliases = concrete_aliases(ssh_config)[:max_hosts]
    projects = saved_remote_projects(app_state)
    project_paths = [item["path"] for item in projects]
    configured = configured_remote_hosts(config)
    configured_by_alias = {str(value["ssh_target"]): host_id for host_id, value in configured.items()}
    aliases.sort(key=lambda value: value not in configured_by_alias)
    previous = read_json(cache_path(control_root), {"hosts": []})
    prior_by_alias = {
        str(item.get("ssh_target")): item
        for item in previous.get("hosts", [])
        if isinstance(previous, dict) and isinstance(item, dict) and isinstance(item.get("ssh_target"), str)
    } if isinstance(previous, dict) else {}
    discovered: list[dict[str, Any]] = []
    configured_matches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen_endpoints: set[str] = set()
    for alias in aliases:
        try:
            fields = resolve_alias(alias)
            endpoint = endpoint_key(fields)
            if endpoint in seen_endpoints:
                continue
            seen_endpoints.add(endpoint)
            result = probe(alias, project_paths, timeout, python_command)
            matched_paths = [str(value) for value in result.get("matched_project_paths", []) if isinstance(value, str)]
            matched_projects = [item for item in projects if item["path"] in matched_paths]
            environment_id = str(result.get("remote_environment_id") or "")
            environment_host_id = f"remote-control:{environment_id}" if environment_id else ""
            if not result.get("sessions_readable") or (not matched_projects and alias not in configured_by_alias):
                continue
            if alias in configured_by_alias:
                configured_matches.append({
                    "host_id": configured_by_alias[alias],
                    "ssh_target": alias,
                    "app_host_ids": sorted({item["app_host_id"] for item in matched_projects if item["app_host_id"]}),
                    "verified_app_host_id": environment_host_id if any(item["app_host_id"] == environment_host_id for item in matched_projects) else None,
                    "project_paths": sorted({item["path"] for item in matched_projects}),
                })
                continue
            host_id = f"ssh-{endpoint[:12]}"
            if not HOST_ID_RE.fullmatch(host_id):
                raise RuntimeError("Derived host ID is invalid")
            home = str(result.get("home") or "")
            sessions_root = str(result.get("sessions_root") or "")
            if not home.startswith("/") or not sessions_root.startswith("/"):
                raise RuntimeError("Probe returned invalid paths")
            worker_root = str(Path(home) / ".codex" / "codex-improver")
            discovered.append({
                "id": host_id,
                "display_name": alias,
                "ssh_target": alias,
                "transport": "ephemeral",
                "python_command": python_command,
                "available": True,
                "last_seen_at": iso(),
                "app_host_ids": sorted({item["app_host_id"] for item in matched_projects if item["app_host_id"]}),
                "verified_app_host_id": environment_host_id if any(item["app_host_id"] == environment_host_id for item in matched_projects) else None,
                "project_paths": sorted({item["path"] for item in matched_projects}),
                "worker_config": {
                    "schema_version": 2,
                    "host_id": host_id,
                    "home": home,
                    "codex_home": str(Path(sessions_root).parent),
                    "sessions_root": sessions_root,
                    "worker_root": worker_root,
                    "allowed_roots": sorted({item["path"] for item in matched_projects}),
                    "excluded_cwds": [worker_root],
                },
            })
        except Exception as exc:
            prior = prior_by_alias.get(alias)
            if isinstance(prior, dict):
                retained = dict(prior)
                retained.update({"available": False, "last_error": str(exc)[:400], "last_checked_at": iso()})
                discovered.append(retained)
            errors.append({"ssh_target": alias, "reason": str(exc)[:400]})
    configured_paths = {
        path
        for match in configured_matches
        for path in match.get("project_paths", [])
        if isinstance(path, str)
    }
    path_owners: dict[str, list[str]] = {}
    for item in discovered:
        if not item.get("available", False):
            continue
        for path in item.get("project_paths", []):
            path_owners.setdefault(str(path), []).append(str(item["ssh_target"]))
    accepted: list[dict[str, Any]] = []
    for item in discovered:
        paths = [str(value) for value in item.get("project_paths", [])]
        ambiguous = [path for path in paths if path in configured_paths or len(path_owners.get(path, [])) > 1]
        if item.get("available", False) and ambiguous:
            errors.append({
                "ssh_target": str(item["ssh_target"]),
                "reason": "Saved Codex project path matched another SSH endpoint: " + ", ".join(ambiguous[:5]),
            })
            continue
        accepted.append(item)
    discovered = accepted
    payload = {
        "schema_version": 1,
        "updated_at": iso(),
        "source": "concrete-ssh-aliases-plus-saved-codex-remote-projects",
        "hosts": discovered,
    }
    atomic_json(cache_path(control_root), payload)
    return {
        "enabled": True,
        "concrete_aliases": aliases,
        "saved_remote_projects": len(projects),
        "discovered": [{"id": item["id"], "ssh_target": item["ssh_target"], "available": item.get("available", False), "project_paths": item.get("project_paths", [])} for item in discovered],
        "configured_matches": configured_matches,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("sync",))
    parser.add_argument("--control-root", type=Path, required=True)
    args = parser.parse_args()
    control_root = args.control_root.resolve()
    config = load_config(control_root)
    print_json(sync_discovered_hosts(control_root, config))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
