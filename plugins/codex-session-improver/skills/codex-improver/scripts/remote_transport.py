#!/usr/bin/env python3
"""Strict JSON-over-SSH transport for configured and auto-discovered hosts."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


HOST_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SSH_TARGET_RE = re.compile(r"[A-Za-z0-9._@:-]{1,255}")
REMOTE_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,1023}")
REMOTE_PYTHON_RE = re.compile(r"(?:/[A-Za-z0-9._/-]{1,1023}|python3(?:\.[0-9]+)?)")
MAX_RESPONSE_BYTES = 8_000_000
BOOTSTRAP_CODE = """import json,sys
e=json.load(sys.stdin)
sys.argv=['remote_worker.py',e['action']]
n={'__name__':'__main__','__file__':e['virtual_file'],'BOOTSTRAP_CONFIG':e['config'],'BOOTSTRAP_REQUEST':e['request']}
exec(compile(e['source'],e['virtual_file'],'exec'),n,n)
"""


def validate_host(raw: dict[str, Any], require_worker: bool) -> tuple[str, dict[str, Any]]:
    host_id = raw.get("id")
    target = raw.get("ssh_target")
    if not isinstance(host_id, str) or not HOST_ID_RE.fullmatch(host_id) or host_id == "local":
        raise RuntimeError(f"Invalid remote host id: {host_id!r}")
    if not isinstance(target, str) or not SSH_TARGET_RE.fullmatch(target) or target.startswith("-"):
        raise RuntimeError(f"Invalid SSH target for {host_id}")
    remote_python = raw.get("python_command", "python3")
    if not isinstance(remote_python, str) or not REMOTE_PYTHON_RE.fullmatch(remote_python) or "/../" in remote_python:
        raise RuntimeError(f"Invalid Python command for {host_id}")
    if require_worker:
        worker = raw.get("worker_path")
        if not isinstance(worker, str) or not REMOTE_PATH_RE.fullmatch(worker) or "/../" in worker:
            raise RuntimeError(f"Invalid worker path for {host_id}")
    else:
        worker_config = raw.get("worker_config")
        if raw.get("transport") != "ephemeral" or not isinstance(worker_config, dict):
            raise RuntimeError(f"Invalid ephemeral worker config for {host_id}")
        for key in ("home", "sessions_root", "worker_root"):
            value = worker_config.get(key)
            if not isinstance(value, str) or not REMOTE_PATH_RE.fullmatch(value) or "/../" in value:
                raise RuntimeError(f"Invalid ephemeral {key} for {host_id}")
        codex_home = worker_config.get("codex_home", str(Path(str(worker_config["sessions_root"])).parent))
        if not isinstance(codex_home, str) or not REMOTE_PATH_RE.fullmatch(codex_home) or "/../" in codex_home:
            raise RuntimeError(f"Invalid ephemeral codex_home for {host_id}")
        roots = worker_config.get("allowed_roots", [])
        if not isinstance(roots, list) or any(not isinstance(value, str) or not value.startswith("/") for value in roots):
            raise RuntimeError(f"Invalid ephemeral roots for {host_id}")
    return host_id, raw


def configured_remote_hosts(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    raw_hosts = config.get("remote_hosts", [])
    if not isinstance(raw_hosts, list):
        raise RuntimeError("remote_hosts must be a list")
    for raw in raw_hosts:
        if not isinstance(raw, dict):
            raise RuntimeError("Each remote host must be an object")
        host_id, host = validate_host(raw, require_worker=True)
        if host_id in result:
            raise RuntimeError(f"Duplicate remote host id: {host_id}")
        result[host_id] = host
    return result


def discovered_remote_hosts(config: dict[str, Any], include_unavailable: bool = False) -> dict[str, dict[str, Any]]:
    control_root = config.get("control_root")
    if not isinstance(control_root, str):
        return {}
    path = Path(control_root).expanduser().resolve(strict=False) / "runtime" / "discovered-hosts.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    raw_hosts = value.get("hosts", []) if isinstance(value, dict) else []
    if not isinstance(raw_hosts, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_hosts:
        if not isinstance(raw, dict) or (not include_unavailable and not raw.get("available", False)):
            continue
        host_id, host = validate_host(raw, require_worker=False)
        if host_id in result:
            raise RuntimeError(f"Duplicate discovered host id: {host_id}")
        result[host_id] = host
    return result


def remote_hosts(config: dict[str, Any], include_unavailable: bool = False) -> dict[str, dict[str, Any]]:
    result = configured_remote_hosts(config)
    for host_id, host in discovered_remote_hosts(config, include_unavailable).items():
        if host_id in result:
            raise RuntimeError(f"Configured and discovered host IDs collide: {host_id}")
        result[host_id] = host
    return result


def get_remote_host(config: dict[str, Any], host_id: str) -> dict[str, Any]:
    try:
        return remote_hosts(config, include_unavailable=True)[host_id]
    except KeyError as exc:
        raise RuntimeError(f"Unknown remote host: {host_id}") from exc


def ssh_command(target: str, remote_arguments: list[str]) -> list[str]:
    remote_command = " ".join(shlex.quote(value) for value in remote_arguments)
    return [
        shutil.which("ssh") or "/usr/bin/ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        target,
        remote_command,
    ]


def call_remote(
    config: dict[str, Any],
    host_id: str,
    action: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    host = get_remote_host(config, host_id)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", action):
        raise RuntimeError("Invalid remote action")
    request_value: dict[str, Any] = payload or {}
    remote_python = str(host.get("python_command", "python3"))
    if host.get("transport") == "ephemeral":
        source_path = Path(__file__).resolve().with_name("remote_worker.py")
        source = source_path.read_text(encoding="utf-8")
        worker_root = str(host["worker_config"]["worker_root"])
        virtual_file = str(Path(worker_root) / "remote_worker.py")
        request_value = {
            "action": action,
            "source": source,
            "virtual_file": virtual_file,
            "config": host["worker_config"],
            "request": request_value,
        }
        command = ssh_command(str(host["ssh_target"]), [remote_python, "-c", BOOTSTRAP_CODE])
    else:
        command = ssh_command(
            str(host["ssh_target"]),
            [remote_python, str(host["worker_path"]), action],
        )
    request = json.dumps(request_value, ensure_ascii=False, separators=(",", ":"))
    try:
        result = subprocess.run(
            command,
            input=request,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Remote host {host_id} is unavailable: {type(exc).__name__}") from exc
    stdout = result.stdout.encode("utf-8", "replace")
    if len(stdout) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"Remote host {host_id} returned too much data")
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"exit {result.returncode}"
        raise RuntimeError(f"Remote host {host_id} failed: {detail[:500]}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Remote host {host_id} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Remote host {host_id} returned a non-object response")
    if value.get("status") == "error":
        raise RuntimeError(f"Remote host {host_id}: {value.get('error', 'unknown error')}")
    return value


def inspect_remote_target(config: dict[str, Any], host_id: str, path: str) -> dict[str, Any]:
    return call_remote(config, host_id, "inspect", {"path": path}, timeout=30)
