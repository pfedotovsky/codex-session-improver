#!/usr/bin/env python3
"""Dependency-free worker: redact transcripts and apply frozen patches on one host."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc
MAX_FILE_BYTES = 250_000
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


def iso() -> str:
    return now().isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def read_request() -> dict[str, Any]:
    bootstrapped = globals().get("BOOTSTRAP_REQUEST")
    if isinstance(bootstrapped, dict):
        return bootstrapped
    raw = sys.stdin.buffer.read(8_000_001)
    if len(raw) > 8_000_000:
        raise RuntimeError("Request is too large")
    value = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(value, dict):
        raise RuntimeError("Request must be a JSON object")
    return value


def read_config() -> dict[str, Any]:
    bootstrapped = globals().get("BOOTSTRAP_CONFIG")
    if isinstance(bootstrapped, dict):
        return bootstrapped
    path = Path(__file__).resolve().with_name("config.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Invalid remote worker config")
    return value


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def redact_text(value: str, config: dict[str, Any], max_chars: int = 6000) -> str:
    text = value
    home = str(Path(config["home"]).resolve())
    text = text.replace(home, "$HOME")
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


def extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    values = content if isinstance(content, list) else [content]
    for item in values:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key in ("text", "input_text", "output_text", "message"):
                if isinstance(item.get(key), str):
                    parts.append(item[key])
                    break
    return "\n".join(parts)


def append_unique(items: list[dict[str, str]], role: str, text: str, config: dict[str, Any]) -> None:
    cleaned = text.strip()
    if not cleaned:
        return
    signature = sha256_bytes((role + "\0" + cleaned).encode("utf-8", "replace"))
    if any(item.get("signature") == signature for item in items[-12:]):
        return
    items.append({"role": role, "text": redact_text(cleaned, config), "signature": signature})


def safe_session_path(raw: str, config: dict[str, Any]) -> Path:
    root = Path(config["sessions_root"]).resolve()
    path = Path(raw).resolve(strict=True)
    if not is_relative_to(path, root) or path.suffix != ".jsonl" or path.is_symlink() or not path.is_file():
        raise RuntimeError("Session path is outside the configured root")
    return path


def parse_session(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {"id": path.stem, "cwd": "", "source": "", "model": ""}
    messages: list[dict[str, str]] = []
    tools: list[dict[str, str]] = []
    errors: list[str] = []
    malformed = 0
    unknown: dict[str, int] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or len(line) > 4_000_000:
                malformed += 1 if line.strip() else 0
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                malformed += 1
                continue
            if not isinstance(event, dict):
                malformed += 1
                continue
            event_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "session_meta":
                meta.update({
                    "id": str(payload.get("session_id") or payload.get("id") or meta["id"]),
                    "cwd": str(payload.get("cwd") or ""),
                    "source": str(payload.get("source") or payload.get("thread_source") or ""),
                    "model": str(payload.get("model") or payload.get("model_provider") or ""),
                })
            elif event_type == "response_item":
                item_type = str(payload.get("type") or "")
                if item_type == "message":
                    role = str(payload.get("role") or "unknown")
                    if role in {"user", "assistant"}:
                        append_unique(messages, role, extract_text_content(payload.get("content")), config)
                elif item_type in {"function_call", "custom_tool_call"}:
                    raw_input = payload.get("arguments", payload.get("input", ""))
                    tools.append({"name": str(payload.get("name") or item_type), "input": redact_text(str(raw_input), config, 800)})
                elif item_type in {"function_call_output", "custom_tool_call_output"}:
                    output = str(payload.get("output") or "")
                    if re.search(r"(?i)\b(error|failed|failure|denied|exception|traceback)\b", output):
                        errors.append(redact_text(output, config, 1200))
            elif event_type == "event_msg":
                message_type = str(payload.get("type") or "")
                if message_type in {"user_message", "user_input"}:
                    append_unique(messages, "user", str(payload.get("message") or payload.get("text") or ""), config)
                elif message_type in {"agent_message", "assistant_message"}:
                    append_unique(messages, "assistant", str(payload.get("message") or payload.get("text") or ""), config)
                elif message_type in {"error", "turn_aborted", "stream_error"}:
                    errors.append(redact_text(str(payload.get("message") or payload), config, 1200))
            else:
                unknown[str(event_type)] = unknown.get(str(event_type), 0) + 1
    source_lower = meta["source"].lower()
    path_lower = str(path).lower()
    is_subagent = "subagent" in source_lower or "/subagents/" in path_lower
    excluded = [Path(value).resolve() for value in config.get("excluded_cwds", [])]
    cwd = Path(meta["cwd"]).resolve(strict=False) if meta["cwd"] else None
    is_self = bool(cwd and any(cwd == root or is_relative_to(cwd, root) for root in excluded))
    return {
        "session_id": meta["id"],
        "cwd": redact_text(meta["cwd"], config, 1000),
        "source": meta["source"],
        "model": meta["model"],
        "messages": [{"role": item["role"], "text": item["text"]} for item in messages[-40:]],
        "tool_calls": tools[-40:],
        "errors": errors[-20:],
        "malformed_records": malformed,
        "unknown_event_counts": unknown,
        "skip": is_subagent or is_self,
        "skip_reason": "subagent" if is_subagent else ("improver-session" if is_self else ""),
    }


def discover(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    root = Path(config["sessions_root"]).resolve()
    processed = request.get("processed", {})
    if not isinstance(processed, dict):
        raise RuntimeError("processed must be an object")
    settle = int(request.get("settle_seconds", 300))
    bootstrap = int(request.get("bootstrap_days", 7))
    limit = min(max(int(request.get("limit", 8)), 1), 100)
    cutoff = now().timestamp() - bootstrap * 86400
    candidates: list[tuple[float, Path, dict[str, int]]] = []
    for path in root.glob("**/*.jsonl"):
        try:
            stat = path.stat()
            if path.is_symlink() or now().timestamp() - stat.st_mtime < settle:
                continue
            value = fingerprint(path)
        except OSError:
            continue
        if processed.get(str(path)) == value:
            continue
        if not processed and stat.st_mtime < cutoff:
            continue
        candidates.append((stat.st_mtime, path, value))
    candidates.sort(key=lambda item: (item[0], str(item[1])))
    return {"status": "ok", "items": [{"path": str(p), "fingerprint": f, "mtime": m} for m, p, f in candidates[:limit]]}


def extract(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    items = request.get("items", [])
    if not isinstance(items, list) or len(items) > 100:
        raise RuntimeError("Invalid extraction item list")
    sessions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        try:
            if not isinstance(item, dict):
                raise RuntimeError("Invalid item")
            path = safe_session_path(str(item["path"]), config)
            if fingerprint(path) != item.get("fingerprint"):
                skipped.append({"path": str(path), "reason": "changed-since-discovery", "defer": True})
                continue
            parsed = parse_session(path, config)
            if parsed.pop("skip", False):
                skipped.append({"session_id": parsed["session_id"], "reason": parsed.pop("skip_reason", "skipped")})
            else:
                parsed.pop("skip_reason", None)
                sessions.append(parsed)
        except Exception as exc:
            skipped.append({"path": str(item.get("path", "")) if isinstance(item, dict) else "", "reason": f"parse-error: {type(exc).__name__}"})
    return {"status": "ok", "sessions": sessions, "skipped": skipped}


def validate_target(raw: str, config: dict[str, Any]) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError("Target path must be absolute")
    resolved = path.resolve(strict=False)
    home = Path(config["home"]).resolve()
    codex_home = Path(str(config.get("codex_home", home / ".codex"))).resolve()
    global_agents = codex_home / "AGENTS.md"
    personal_skill_roots = (codex_home / "skills", home / ".agents" / "skills")
    forbidden = {".git", "sessions", "plugins", "cache", "auth.json", "config.toml", "hooks.json"}
    if resolved == global_agents:
        return resolved
    for personal_skills in personal_skill_roots:
        if is_relative_to(resolved, personal_skills):
            relative = resolved.relative_to(personal_skills)
            if not relative.parts or relative.parts[0] == ".system" or any(part in forbidden for part in relative.parts):
                raise RuntimeError("Forbidden personal-skill target")
            return resolved
    roots = [Path(value).resolve() for value in config.get("allowed_roots", [])]
    if not any(is_relative_to(resolved, root) for root in roots):
        raise RuntimeError("Target is outside configured remote roots")
    if any(part in forbidden for part in resolved.parts):
        raise RuntimeError("Forbidden project target")
    normalized = resolved.as_posix()
    if resolved.name == "AGENTS.md" or "/.agents/skills/" in normalized or "/.codex/skills/" in normalized or resolved.suffix.lower() == ".md":
        return resolved
    raise RuntimeError("Only AGENTS.md, skills, and Markdown documentation are supported")


def inspect(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    path = validate_target(str(request.get("path", "")), config)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Target must be a regular file")
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES or b"\x00" in data:
            raise RuntimeError("Target is too large or not text")
        content = data.decode("utf-8")
        return {"status": "ok", "path": str(path), "exists": True, "content": content, "sha256": sha256_bytes(data), "mode": path.stat().st_mode & 0o777}
    return {"status": "ok", "path": str(path), "exists": False, "content": "", "sha256": None, "mode": 0o644}


def atomic_text(path: Path, value: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(value)
    os.chmod(temp, mode)
    os.replace(temp, path)


def git_root(path: Path) -> Path | None:
    result = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False, timeout=10)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def validate_skill(root: Path) -> tuple[bool, str]:
    skill = root / "SKILL.md"
    if not skill.is_file():
        return False, "SKILL.md is missing"
    text = skill.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 4 or lines[0].strip() != "---" or "---" not in [line.strip() for line in lines[1:]]:
        return False, "SKILL.md frontmatter is invalid"
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), -1)
    front = "\n".join(lines[1:end])
    name = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", front)
    description = re.search(r"(?m)^description:\s*.+", front)
    if not name or name.group(1).strip() != root.name or not description:
        return False, "Skill name/description is invalid"
    return True, "ok"


def run_validations(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    skills: set[Path] = set()
    repos: dict[Path, list[Path]] = {}
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
        if "/skills/" in normalized:
            prefix, suffix = normalized.split("/skills/", 1)
            name = suffix.split("/", 1)[0]
            if name and name != ".system":
                skills.add(Path(prefix + "/skills/" + name))
        root = git_root(target)
        if root:
            repos.setdefault(root, []).append(target)
    for root in sorted(skills):
        ok, output = validate_skill(root)
        results.append({"kind": "skill-validate", "path": str(root), "ok": ok, "output": output})
    for root, targets in repos.items():
        relative = [str(path.relative_to(root)) for path in targets]
        result = subprocess.run(["git", "-C", str(root), "diff", "--check", "--", *relative], capture_output=True, text=True, check=False, timeout=30)
        results.append({"kind": "git-diff-check", "path": str(root), "ok": result.returncode == 0, "output": (result.stdout + result.stderr)[-2000:]})
    return results


def apply(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    proposal_id = str(request.get("proposal_id", ""))
    if not re.fullmatch(r"P-\d{8}-\d{2}", proposal_id):
        raise RuntimeError("Invalid proposal id")
    raw_changes = request.get("changes")
    if not isinstance(raw_changes, list) or not 1 <= len(raw_changes) <= 8:
        raise RuntimeError("Invalid changes")
    changes: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for raw in raw_changes:
        if not isinstance(raw, dict) or not isinstance(raw.get("desired_content"), str):
            raise RuntimeError("Invalid change")
        target = validate_target(str(raw.get("path", "")), config)
        if target in seen:
            raise RuntimeError("Duplicate target")
        seen.add(target)
        desired = raw["desired_content"]
        encoded = desired.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES or b"\x00" in encoded or sha256_bytes(encoded) != raw.get("desired_sha256"):
            raise RuntimeError("Frozen desired content is invalid")
        observed = inspect({"path": str(target)}, config)["sha256"]
        if observed != raw.get("base_sha256"):
            return {"status": "stale", "reason": f"Base hash changed: {target}"}
        changes.append({**raw, "path": str(target)})

    backup_root = Path(config["worker_root"]).resolve() / "backups" / proposal_id
    if backup_root.exists():
        raise RuntimeError("A backup already exists for this proposal")
    backup_root.mkdir(parents=True)
    metadata: list[dict[str, Any]] = []
    for index, change in enumerate(changes, 1):
        target = Path(change["path"])
        entry = {"path": str(target), "existed": target.exists(), "mode": int(change.get("mode", 0o644))}
        if target.exists():
            backup = backup_root / f"{index:03d}.bin"
            shutil.copyfile(target, backup)
            entry["backup"] = backup.name
        metadata.append(entry)
    (backup_root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    validations: list[dict[str, Any]] = []
    try:
        for change in changes:
            atomic_text(Path(change["path"]), change["desired_content"], int(change.get("mode", 0o644)))
        validations = run_validations(changes)
        if not all(item.get("ok") for item in validations):
            raise RuntimeError("Validation failed")
    except Exception as exc:
        restored: list[str] = []
        for entry in reversed(metadata):
            target = Path(entry["path"])
            if entry["existed"]:
                data = (backup_root / entry["backup"]).read_bytes()
                temp = target.with_name(f".{target.name}.{os.getpid()}.rollback")
                temp.write_bytes(data)
                os.chmod(temp, int(entry["mode"]))
                os.replace(temp, target)
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            restored.append(str(target))
        return {"status": "failed", "reason": str(exc), "rolled_back": True, "restored": restored, "validation": validations, "backup": str(backup_root)}
    return {"status": "applied", "changes": [item["path"] for item in changes], "validation": validations, "backup": str(backup_root), "applied_at": iso()}


def diagnose(config: dict[str, Any]) -> dict[str, Any]:
    root = Path(config["sessions_root"]).resolve()
    return {
        "status": "ok",
        "host_id": config.get("host_id"),
        "sessions_root_readable": root.is_dir(),
        "session_files": sum(1 for _ in root.glob("**/*.jsonl")) if root.is_dir() else 0,
        "worker_version": 2,
    }


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"discover", "extract", "inspect", "apply", "diagnose"}:
        raise RuntimeError("Unknown worker action")
    action = sys.argv[1]
    config = read_config()
    request = read_request()
    handlers = {"discover": discover, "extract": extract, "inspect": inspect, "apply": apply}
    value = diagnose(config) if action == "diagnose" else handlers[action](request, config)
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
