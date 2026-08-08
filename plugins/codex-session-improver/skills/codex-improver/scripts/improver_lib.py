#!/usr/bin/env python3
"""Shared, dependency-free helpers for the Codex improver."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


UTC = dt.timezone.utc
PROPOSAL_ID_RE = re.compile(r"P-\d{8}-\d{2}")
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|cookie|authorization)(\s*[:=]\s*)[^\s,;]{6,}"),
]
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime | None = None) -> str:
    return (value or now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def atomic_text(path: Path, value: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(value)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def redact_text(value: str, max_chars: int = 6000) -> str:
    text = value.replace(str(Path.home()), "$HOME")
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED_SECRET]", text)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[TRUNCATED {len(text) - max_chars} CHARS]"
    return text


def redact_obj(value: Any, max_string: int = 6000) -> Any:
    if isinstance(value, str):
        return redact_text(value, max_string)
    if isinstance(value, list):
        return [redact_obj(item, max_string) for item in value[:200]]
    if isinstance(value, dict):
        return {str(key): redact_obj(item, max_string) for key, item in list(value.items())[:200]}
    return value


def load_config(control_root: Path) -> dict[str, Any]:
    config_path = control_root / "config.json"
    config = read_json(config_path)
    if not isinstance(config, dict):
        raise RuntimeError(f"Missing or invalid config: {config_path}")
    return config


def runtime_dir(control_root: Path) -> Path:
    return control_root / "runtime"


def ensure_runtime(control_root: Path) -> None:
    for name in ("batches", "findings", "proposals", "backups", "runs", "drafts"):
        (runtime_dir(control_root) / name).mkdir(parents=True, exist_ok=True)


def extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "input_text", "output_text", "message"):
                    candidate = item.get(key)
                    if isinstance(candidate, str):
                        parts.append(candidate)
                        break
    elif isinstance(content, dict):
        for key in ("text", "input_text", "output_text", "message"):
            candidate = content.get(key)
            if isinstance(candidate, str):
                parts.append(candidate)
    return "\n".join(parts)


def _append_unique(items: list[dict[str, str]], role: str, text: str) -> None:
    cleaned = text.strip()
    if not cleaned:
        return
    signature = sha256_bytes((role + "\0" + cleaned).encode("utf-8", "replace"))
    if any(item.get("signature") == signature for item in items[-12:]):
        return
    items.append({"role": role, "text": redact_text(cleaned), "signature": signature})


def parse_session(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {"path": str(path), "id": path.stem, "cwd": "", "source": ""}
    messages: list[dict[str, str]] = []
    tools: list[dict[str, str]] = []
    errors: list[str] = []
    malformed = 0
    unknown: dict[str, int] = {}

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            event_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "session_meta":
                meta.update(
                    {
                        "id": str(payload.get("session_id") or payload.get("id") or meta["id"]),
                        "cwd": str(payload.get("cwd") or ""),
                        "source": str(payload.get("source") or payload.get("thread_source") or ""),
                        "model": str(payload.get("model") or payload.get("model_provider") or ""),
                    }
                )
                continue
            if event_type == "response_item":
                item_type = str(payload.get("type") or "")
                if item_type == "message":
                    role = str(payload.get("role") or "unknown")
                    if role in {"user", "assistant"}:
                        _append_unique(messages, role, extract_text_content(payload.get("content")))
                elif item_type in {"function_call", "custom_tool_call"}:
                    name = str(payload.get("name") or item_type)
                    raw_input = payload.get("arguments", payload.get("input", ""))
                    tools.append({"name": name, "input": redact_text(str(raw_input), 800)})
                elif item_type in {"function_call_output", "custom_tool_call_output"}:
                    output = str(payload.get("output") or "")
                    if re.search(r"(?i)\b(error|failed|failure|denied|exception|traceback)\b", output):
                        errors.append(redact_text(output, 1200))
                continue
            if event_type == "event_msg":
                message_type = str(payload.get("type") or "")
                if message_type in {"user_message", "user_input"}:
                    _append_unique(messages, "user", str(payload.get("message") or payload.get("text") or ""))
                elif message_type in {"agent_message", "assistant_message"}:
                    _append_unique(messages, "assistant", str(payload.get("message") or payload.get("text") or ""))
                elif message_type in {"error", "turn_aborted", "stream_error"}:
                    errors.append(redact_text(str(payload.get("message") or payload), 1200))
                continue
            unknown[str(event_type)] = unknown.get(str(event_type), 0) + 1

    source_lower = meta.get("source", "").lower()
    path_lower = str(path).lower()
    is_subagent = "subagent" in source_lower or "/subagents/" in path_lower
    cwd = Path(meta["cwd"]).expanduser() if meta.get("cwd") else None
    control_root = Path(config["control_root"]).expanduser().resolve()
    is_self = bool(cwd and (cwd.resolve() == control_root or control_root in cwd.resolve().parents))
    return {
        "session_id": meta["id"],
        "cwd": redact_text(meta.get("cwd", ""), 1000),
        "source": meta.get("source", ""),
        "model": meta.get("model", ""),
        "messages": [{"role": item["role"], "text": item["text"]} for item in messages[-40:]],
        "tool_calls": tools[-40:],
        "errors": errors[-20:],
        "malformed_records": malformed,
        "unknown_event_counts": unknown,
        "skip": is_subagent or is_self,
        "skip_reason": "subagent" if is_subagent else ("improver-session" if is_self else ""),
    }


def proposal_dir(control_root: Path, proposal_id: str) -> Path:
    if not PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise ValueError(f"Invalid proposal id: {proposal_id}")
    return runtime_dir(control_root) / "proposals" / proposal_id


def load_manifest(control_root: Path, proposal_id: str) -> tuple[Path, dict[str, Any]]:
    path = proposal_dir(control_root, proposal_id) / "manifest.json"
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise FileNotFoundError(f"Proposal not found: {proposal_id}")
    return path, manifest


def proposal_target(manifest: dict[str, Any]) -> str:
    host = str(manifest.get("target_host", "local"))
    paths = []
    for change in manifest.get("changes", []):
        if isinstance(change, dict) and isinstance(change.get("path"), str):
            paths.append(change["path"])
    target = ", ".join(paths) if paths else "target recorded in the frozen manifest"
    return f"{host} — {target}"


def proposal_list_data(
    control_root: Path,
    proposal_ids: list[str],
    created_proposal_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    proposal_ids = list(dict.fromkeys(proposal_ids))
    if not 1 <= len(proposal_ids) <= 3:
        raise RuntimeError("A review result requires one to three proposals")
    created = set(created_proposal_ids)
    items = []
    for proposal_id in proposal_ids:
        _, manifest = load_manifest(control_root, proposal_id)
        if manifest.get("status") != "pending" or parse_iso(manifest["expiry_at"]) <= now():
            raise RuntimeError(f"Proposal is not pending and unexpired: {proposal_id}")
        items.append(
            {
                "id": proposal_id,
                "origin": "new" if proposal_id in created else "already-pending",
                "summary": str(manifest.get("summary", ""))[:500],
                "problem": str(manifest.get("root_cause", ""))[:1500],
                "target": proposal_target(manifest),
                "context_surface": str(manifest.get("context_surface", "unspecified"))[:100],
                "placement_reason": str(manifest.get("placement_reason", ""))[:1500],
                "evidence": redact_obj(manifest.get("evidence", []), 1500),
                "risk": str(manifest.get("risk", ""))[:1500],
                "rollback": str(manifest.get("rollback", ""))[:1500],
                "validation": redact_obj(manifest.get("validation_commands", []), 1500),
                "expires_at": str(manifest.get("expiry_at", "")),
                "patch": str(proposal_dir(control_root, proposal_id) / str(manifest.get("patch_file", "change.patch"))),
            }
        )
    return items


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def configured_roots(config: dict[str, Any]) -> list[Path]:
    return [Path(value).expanduser().resolve() for value in config.get("writable_roots", [])]


def validate_target(path: Path, config: dict[str, Any]) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError("Target path must be absolute")
    resolved = expanded.resolve(strict=False)
    home = Path.home().resolve()
    codex_home = Path(str(config.get("sessions_root", home / ".codex" / "sessions"))).expanduser().resolve().parent
    global_agents = codex_home / "AGENTS.md"
    personal_skill_roots = (
        codex_home / "skills",
        home / ".agents" / "skills",
    )
    forbidden_parts = {".git", "sessions", "plugins", "cache", "auth.json", "config.toml", "hooks.json"}
    if resolved == global_agents:
        return resolved
    for personal_skills in personal_skill_roots:
        if is_relative_to(resolved, personal_skills):
            relative = resolved.relative_to(personal_skills)
            if not relative.parts or relative.parts[0] == ".system":
                raise ValueError("System skills are not writable targets")
            if any(part in forbidden_parts for part in relative.parts):
                raise ValueError("Forbidden personal-skill target")
            return resolved
    roots = configured_roots(config)
    if not any(is_relative_to(resolved, root) for root in roots if root != codex_home):
        raise ValueError("Target is outside configured project roots")
    if any(part in forbidden_parts for part in resolved.parts):
        raise ValueError("Forbidden project target")
    normalized = resolved.as_posix()
    if resolved.name == "AGENTS.md":
        return resolved
    if "/.agents/skills/" in normalized or "/.codex/skills/" in normalized:
        return resolved
    if resolved.suffix.lower() == ".md":
        return resolved
    raise ValueError("Only AGENTS.md, skills, and Markdown documentation are supported")


def find_git_root(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def prune_runtime(control_root: Path, retention_days: int) -> list[str]:
    cutoff = now().timestamp() - retention_days * 86400
    removed: list[str] = []
    for category in ("findings", "runs", "backups"):
        root = runtime_dir(control_root) / category
        if not root.exists():
            continue
        for path in root.iterdir():
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if path.is_dir():
                    import shutil

                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed.append(str(path))
            except FileNotFoundError:
                pass
    return removed


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
