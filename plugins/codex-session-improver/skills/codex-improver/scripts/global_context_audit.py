#!/usr/bin/env python3
"""Audit persistent global Codex context without reading session transcripts."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


TABLE_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*(?:#.*)?$")


def approx_tokens(characters: int) -> int:
    return (characters + 3) // 4


def file_stats(path: Path, display: Callable[[Path], str]) -> dict[str, Any]:
    if not path.is_file():
        return {"path": display(path), "exists": False, "bytes": 0, "lines": 0, "approx_tokens": 0}
    data = path.read_bytes()
    return {
        "path": display(path),
        "exists": True,
        "bytes": len(data),
        "lines": len(data.splitlines()),
        "approx_tokens": approx_tokens(len(data)),
    }


def decode_table_key(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
            return str(decoded)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def table_suffix(section: str, prefix: str) -> str | None:
    marker = prefix + "."
    if not section.startswith(marker):
        return None
    raw_suffix = section[len(marker) :]
    if "." in raw_suffix and not raw_suffix.startswith(('"', "'")):
        return None
    return decode_table_key(raw_suffix)


def boolean_value(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def parse_global_config(path: Path) -> dict[str, Any]:
    """Project only non-secret structure from global config TOML.

    This intentionally avoids a general TOML dependency so the installed runtime
    remains compatible with Python 3.9. Values other than booleans in selected
    safe sections are never retained.
    """

    result: dict[str, Any] = {
        "projects": {},
        "plugins": {},
        "mcp_servers": {},
        "features": {},
        "model_providers": set(),
        "profiles": set(),
        "hook_state_entries": 0,
    }
    if not path.is_file():
        return result
    current_kind: str | None = None
    current_name: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return result
    for line in lines:
        table_match = TABLE_RE.match(line)
        if table_match:
            section = table_match.group(1).strip()
            current_kind = None
            current_name = None
            for kind, prefix in (
                ("project", "projects"),
                ("plugin", "plugins"),
                ("mcp", "mcp_servers"),
                ("model_provider", "model_providers"),
                ("profile", "profiles"),
            ):
                suffix = table_suffix(section, prefix)
                if suffix is None:
                    continue
                current_kind = kind
                current_name = suffix
                if kind == "project":
                    result["projects"].setdefault(suffix, {})
                elif kind == "plugin":
                    result["plugins"].setdefault(suffix, {"enabled": True})
                elif kind == "mcp":
                    result["mcp_servers"].setdefault(suffix, {"enabled": True})
                elif kind == "model_provider":
                    result["model_providers"].add(suffix)
                elif kind == "profile":
                    result["profiles"].add(suffix)
                break
            if section == "features":
                current_kind = "features"
            elif section.startswith("hooks.state."):
                result["hook_state_entries"] += 1
            continue

        assignment = ASSIGNMENT_RE.match(line)
        if not assignment:
            continue
        key, raw_value = assignment.groups()
        value = boolean_value(raw_value)
        if value is None:
            continue
        if current_kind == "features":
            result["features"][key] = value
        elif current_kind == "plugin" and current_name and key == "enabled":
            result["plugins"][current_name]["enabled"] = value
        elif current_kind == "mcp" and current_name and key == "enabled":
            result["mcp_servers"][current_name]["enabled"] = value
    return result


def scalar_text(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def frontmatter(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}
    result: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        if ":" not in line or line[:1].isspace():
            index += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value in {"|", "|-", ">", ">-"}:
            parts: list[str] = []
            index += 1
            while index < end and (not lines[index] or lines[index][:1].isspace()):
                parts.append(lines[index].strip())
                index += 1
            separator = "\n" if raw_value.startswith("|") else " "
            result[key] = separator.join(part for part in parts if part).strip()
            continue
        result[key] = scalar_text(raw_value)
        index += 1
    return result


def skill_inventory(
    roots: list[tuple[str, Path]],
    display: Callable[[Path], str],
    exclude_system: bool = False,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for source, root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            try:
                relative_parts = path.relative_to(root).parts
            except ValueError:
                relative_parts = path.parts
            if exclude_system and ".system" in relative_parts:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            metadata = frontmatter(path)
            stats = file_stats(path, display)
            description = metadata.get("description", "")
            records.append(
                {
                    "source": source,
                    "path": display(path),
                    "name": metadata.get("name", path.parent.name),
                    "description_chars": len(description),
                    "description_approx_tokens": approx_tokens(len(description)),
                    "full_file_bytes": stats["bytes"],
                    "full_file_approx_tokens": stats["approx_tokens"],
                }
            )
    return records


def plugin_inventory(
    cache_root: Path,
    configured: dict[str, Any],
    display: Callable[[Path], str],
) -> dict[str, Any]:
    cached: list[dict[str, Any]] = []
    by_name: dict[str, list[dict[str, Any]]] = {}
    if cache_root.is_dir():
        for manifest_path in sorted(cache_root.rglob(".codex-plugin/plugin.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            plugin_root = manifest_path.parent.parent
            plugin_skills = skill_inventory([("plugin", plugin_root / "skills")], display)
            record = {
                "name": str(manifest.get("name", plugin_root.name)),
                "version": str(manifest.get("version", "unknown")),
                "path": display(plugin_root),
                "skill_count": len(plugin_skills),
                "skill_description_chars": sum(item["description_chars"] for item in plugin_skills),
                "has_app_connector": bool(manifest.get("apps")) or (plugin_root / ".app.json").is_file(),
                "has_mcp_config": bool(manifest.get("mcp_servers")),
            }
            cached.append(record)
            by_name.setdefault(record["name"], []).append(record)

    enabled: list[dict[str, Any]] = []
    disabled: list[str] = []
    for plugin_id, details in sorted(configured.items()):
        is_enabled = bool(details.get("enabled", True)) if isinstance(details, dict) else True
        name = plugin_id.split("@", 1)[0]
        matches = by_name.get(name, [])
        projected = {
            "id": plugin_id,
            "name": name,
            "enabled": is_enabled,
            "cached_versions": sorted({item["version"] for item in matches}),
            "skill_count": max((item["skill_count"] for item in matches), default=0),
            "skill_description_chars": max(
                (item["skill_description_chars"] for item in matches), default=0
            ),
            "has_app_connector": any(item["has_app_connector"] for item in matches),
            "has_mcp_config": any(item["has_mcp_config"] for item in matches),
        }
        if is_enabled:
            enabled.append(projected)
        else:
            disabled.append(plugin_id)

    return {
        "configured_enabled": enabled,
        "configured_disabled": disabled,
        "configured_enabled_count": len(enabled),
        "cached_plugin_count": len(cached),
        "cached": cached,
        "enabled_skill_count": sum(item["skill_count"] for item in enabled),
        "enabled_skill_description_chars": sum(item["skill_description_chars"] for item in enabled),
        "enabled_app_connector_count": sum(1 for item in enabled if item["has_app_connector"]),
    }


def effective_mcp_inventory(codex_home: Path) -> dict[str, Any]:
    executable = shutil.which("codex")
    if not executable:
        return {"status": "unavailable", "servers": [], "error": "codex executable not found"}
    environment = dict(os.environ, CODEX_HOME=str(codex_home))
    try:
        completed = subprocess.run(
            [executable, "mcp", "list", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
        raw = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return {"status": "error", "servers": [], "error": str(exc)[:300]}
    servers = []
    for item in raw if isinstance(raw, list) else []:
        transport = item.get("transport") if isinstance(item, dict) else {}
        servers.append(
            {
                "name": str(item.get("name", "unknown")),
                "enabled": item.get("enabled"),
                "transport_type": transport.get("type") if isinstance(transport, dict) else None,
                "auth_status": item.get("auth_status"),
            }
        )
    return {"status": "ok", "servers": servers}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--agents-home", type=Path, default=Path.home() / ".agents")
    args = parser.parse_args()

    codex_home = args.codex_home.expanduser().resolve()
    agents_home = args.agents_home.expanduser().resolve()

    def display(path: Path) -> str:
        resolved = path.expanduser().resolve(strict=False)
        for root, label in ((codex_home, "$CODEX_HOME"), (agents_home, "$AGENTS_HOME")):
            try:
                relative = resolved.relative_to(root)
                return label if not relative.parts else str(Path(label) / relative)
            except ValueError:
                pass
        try:
            relative = resolved.relative_to(Path.home().resolve())
            return "~" if not relative.parts else str(Path("~") / relative)
        except ValueError:
            return str(resolved)

    config_path = codex_home / "config.toml"
    config = parse_global_config(config_path)
    agents_stats = file_stats(codex_home / "AGENTS.md", display)
    config_stats = file_stats(config_path, display)
    global_state_stats = file_stats(codex_home / ".codex-global-state.json", display)
    rules_files = sorted((codex_home / "rules").rglob("*.rules")) if (codex_home / "rules").is_dir() else []
    rules_stats = [file_stats(path, display) for path in rules_files]
    approval_rule_count = sum(stats["lines"] for stats in rules_stats)

    skills = skill_inventory(
        [
            ("codex-user", codex_home / "skills"),
            ("agents-user", agents_home / "skills"),
        ],
        display,
        exclude_system=True,
    )
    plugins = plugin_inventory(codex_home / "plugins" / "cache", config["plugins"], display)
    projects = config["projects"]
    missing_projects = sorted(path for path in projects if not Path(path).expanduser().exists())
    configured_mcp = config["mcp_servers"]
    disabled_mcp = sorted(
        name
        for name, details in configured_mcp.items()
        if isinstance(details, dict) and details.get("enabled") is False
    )

    review_candidates: list[dict[str, Any]] = []
    if disabled_mcp:
        review_candidates.append(
            {
                "key": "disabled-mcp-registrations",
                "summary": "Remove disabled MCP registrations that are intentionally retired.",
                "evidence": {"servers": disabled_mcp},
                "automatic_action": False,
            }
        )
    if plugins["configured_enabled_count"] >= 6:
        review_candidates.append(
            {
                "key": "enabled-plugin-surface",
                "summary": "Review enabled plugins and app connectors against actual usage.",
                "evidence": {
                    "enabled_plugins": plugins["configured_enabled_count"],
                    "enabled_app_connectors": plugins["enabled_app_connector_count"],
                    "configured_plugin_skills": plugins["enabled_skill_count"],
                },
                "automatic_action": False,
            }
        )
    if approval_rule_count >= 10:
        review_candidates.append(
            {
                "key": "persistent-approval-rules",
                "summary": "Review persistent command approvals for obsolete or overly broad entries.",
                "evidence": {"rule_entries": approval_rule_count},
                "automatic_action": False,
            }
        )
    if missing_projects:
        review_candidates.append(
            {
                "key": "stale-project-config",
                "summary": "Review project configuration entries whose paths no longer exist.",
                "evidence": {
                    "missing_count": len(missing_projects),
                    "paths": [display(Path(path)) for path in missing_projects],
                },
                "automatic_action": False,
            }
        )

    result = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "codex_home": "$CODEX_HOME",
            "agents_home": "$AGENTS_HOME",
            "includes_session_history": False,
            "includes_secrets": False,
        },
        "global_agents": agents_stats,
        "config": {
            **config_stats,
            "project_entries": len(projects),
            "missing_project_entries": len(missing_projects),
            "model_provider_entries": len(config["model_providers"]),
            "profile_entries": len(config["profiles"]),
            "hook_state_entries": config["hook_state_entries"],
            "feature_flags": config["features"],
            "raw_file_assumed_injected": False,
        },
        "rules": {"files": rules_stats, "rule_entries": approval_rule_count},
        "skills": {
            "installed_user_skill_count": len(skills),
            "catalog_description_chars": sum(item["description_chars"] for item in skills),
            "catalog_description_approx_tokens": approx_tokens(
                sum(item["description_chars"] for item in skills)
            ),
            "full_files_approx_tokens_if_loaded": sum(item["full_file_approx_tokens"] for item in skills),
            "items": skills,
        },
        "plugins": plugins,
        "mcp": {
            "configured_count": len(configured_mcp),
            "configured_disabled": disabled_mcp,
            "effective": effective_mcp_inventory(codex_home),
        },
        "global_state": {**global_state_stats, "assumed_injected": False},
        "review_candidates": review_candidates,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)[:500]}), file=sys.stderr)
        raise SystemExit(1)
