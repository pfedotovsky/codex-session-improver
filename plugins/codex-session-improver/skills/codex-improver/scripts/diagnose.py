#!/usr/bin/env python3
"""Read-only diagnostics and optional retention cleanup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from host_discovery import sync_discovered_hosts
from improver_lib import ensure_runtime, load_config, print_json, prune_runtime, read_json, runtime_dir
from remote_transport import call_remote, remote_hosts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    control_root = args.control_root.resolve()
    config = load_config(control_root)
    ensure_runtime(control_root)
    sessions_root = Path(config["sessions_root"]).expanduser()
    runtime = runtime_dir(control_root)
    pending_batches = list((runtime / "batches").glob("*.json"))
    manifests = [read_json(path) for path in (runtime / "proposals").glob("P-*/manifest.json")]
    status_counts: dict[str, int] = {}
    for manifest in manifests:
        if isinstance(manifest, dict):
            status = str(manifest.get("status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
    removed = prune_runtime(control_root, int(config.get("retention_days", 90))) if args.cleanup else []
    discovery = sync_discovered_hosts(control_root, config)
    remote_checks = []
    for host_id in remote_hosts(config):
        try:
            remote_checks.append(call_remote(config, host_id, "diagnose", {}, timeout=30))
        except Exception as exc:
            remote_checks.append({"host_id": host_id, "status": "unavailable", "error": str(exc)[:500]})
    checks = {
        "control_root_exists": control_root.is_dir(),
        "config_exists": (control_root / "config.json").is_file(),
        "sessions_root_readable": sessions_root.is_dir(),
        "session_files": sum(1 for _ in sessions_root.glob("**/*.jsonl")) if sessions_root.is_dir() else 0,
        "cursor": read_json(runtime / "cursor.json", {}),
        "pending_batches": len(pending_batches),
        "proposal_statuses": status_counts,
        "cleanup_removed": removed,
        "host_discovery": discovery,
        "remote_hosts": remote_checks,
    }
    print_json(checks)
    return 0 if all(checks[key] for key in ("control_root_exists", "config_exists", "sessions_root_readable")) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
