#!/usr/bin/env python3
"""Project-local hook dispatcher for approval receipts and write guards."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from improver_lib import (
    UTC,
    atomic_json,
    configured_roots,
    ensure_runtime,
    iso,
    load_config,
    load_manifest,
    now,
    parse_approval_decision,
    parse_iso,
    question_path,
    read_json,
    receipt_path,
    sha256_bytes,
)


SKILL_SCRIPTS = Path(__file__).resolve().parent
APPLIER = SKILL_SCRIPTS / "apply_proposals.py"
QUESTIONER = SKILL_SCRIPTS / "approval_prompt.py"
ALLOWED_SCRIPT_NAMES = {"session_batch.py", "proposal_tool.py", "diagnose.py", "host_discovery.py"}
READ_ONLY_COMMANDS = {"ls", "find", "rg", "sed", "jq", "stat", "test", "pwd", "head", "tail", "wc"}


def output(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False))


def deny(reason: str) -> None:
    output(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def pending_proposal_error(control_root: Path, proposal_ids: list[str]) -> str | None:
    invalid: list[str] = []
    for proposal_id in proposal_ids:
        try:
            _, manifest = load_manifest(control_root, proposal_id)
            if manifest.get("status") != "pending" or parse_iso(manifest["expiry_at"]) <= now():
                invalid.append(proposal_id)
        except Exception:
            invalid.append(proposal_id)
    if invalid:
        return f"Unknown, expired, or non-pending proposal IDs: {', '.join(invalid)}"
    return None


def create_receipt(
    control_root: Path,
    session_id: str,
    turn_id: str,
    prompt: str,
    proposal_ids: list[str],
) -> Path:
    path = receipt_path(control_root, session_id, turn_id)
    receipt = {
        "schema_version": 1,
        "session_id": session_id,
        "turn_id": turn_id,
        "proposal_ids": proposal_ids,
        "approval_kind": "question-response",
        "created_at": iso(),
        "expires_at": iso(now() + dt.timedelta(minutes=10)),
        "prompt_sha256": sha256_bytes(prompt.strip().encode("utf-8")),
        "consumed_at": None,
    }
    atomic_json(path, receipt)
    return path


def create_question(control_root: Path, session_id: str, turn_id: str, proposal_ids: list[str]) -> Path:
    expiries = []
    summaries = []
    for proposal_id in proposal_ids:
        _, manifest = load_manifest(control_root, proposal_id)
        expiries.append(parse_iso(manifest["expiry_at"]))
        summaries.append({"id": proposal_id, "summary": str(manifest.get("summary", ""))[:500]})
    value = {
        "schema_version": 1,
        "session_id": session_id,
        "question_turn_id": turn_id,
        "proposal_ids": proposal_ids,
        "summaries": summaries,
        "created_at": iso(),
        "expires_at": iso(min(expiries)),
        "status": "pending",
        "response_turn_id": None,
        "response_prompt_sha256": None,
        "responded_at": None,
    }
    path = question_path(control_root, session_id)
    atomic_json(path, value)
    return path


def active_question(control_root: Path, session_id: str) -> tuple[Path, dict[str, Any]] | None:
    path = question_path(control_root, session_id)
    value = read_json(path)
    if not isinstance(value, dict) or value.get("session_id") != session_id or value.get("status") != "pending":
        return None
    try:
        if parse_iso(value["expires_at"]) <= now():
            return None
    except Exception:
        return None
    proposal_ids = value.get("proposal_ids")
    if not isinstance(proposal_ids, list) or not proposal_ids or not all(isinstance(item, str) for item in proposal_ids):
        return None
    if pending_proposal_error(control_root, proposal_ids):
        return None
    return path, value


def input_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in {"input_text", "text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts) if parts else None


def current_user_prompt(config: dict[str, Any], event: dict[str, Any]) -> tuple[str | None, str | None]:
    session_id = str(event.get("session_id") or "")
    turn_id = str(event.get("turn_id") or "")
    raw_path = event.get("transcript_path")
    if not session_id or not turn_id or not isinstance(raw_path, str) or not raw_path:
        return None, "Codex did not provide a session ID, turn ID, and transcript path."
    try:
        path = Path(raw_path).resolve(strict=True)
        sessions_root = Path(config["sessions_root"]).expanduser().resolve(strict=True)
    except (KeyError, OSError, RuntimeError, ValueError):
        return None, "The current transcript path could not be verified."
    if path != sessions_root and sessions_root not in path.parents:
        return None, "The current transcript is outside the configured sessions directory."

    transcript_session_id: str | None = None
    active_turn: str | None = None
    prompt: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(line) > 4_000_000:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = record.get("type")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record_type == "session_meta":
                    candidate = payload.get("session_id") or payload.get("id")
                    if isinstance(candidate, str):
                        transcript_session_id = candidate
                elif record_type == "event_msg" and payload.get("type") == "task_started":
                    candidate = payload.get("turn_id")
                    active_turn = candidate if isinstance(candidate, str) else None
                elif record_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    metadata = payload.get("internal_chat_message_metadata_passthrough")
                    message_turn = metadata.get("turn_id") if isinstance(metadata, dict) else None
                    if message_turn == turn_id or (message_turn is None and active_turn == turn_id):
                        candidate = input_text(payload)
                        if candidate is not None:
                            prompt = candidate
                elif record_type == "event_msg" and payload.get("type") == "user_message" and active_turn == turn_id:
                    candidate = payload.get("message")
                    if isinstance(candidate, str):
                        prompt = candidate
    except OSError:
        return None, "The current transcript could not be read."
    if transcript_session_id != session_id:
        return None, "The transcript is bound to another task."
    if prompt is None:
        return None, "No user-authored prompt was found for the current turn."
    return prompt, None


def applier_tokens(command: str) -> list[str] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 2 or not is_python_executable(tokens[0]):
        return None
    if Path(tokens[1]) != APPLIER:
        return None
    return tokens


def questioner_tokens(command: str) -> list[str] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 6 or not is_python_executable(tokens[0]):
        return None
    if Path(tokens[1]) != QUESTIONER:
        return None
    return tokens


def canonical_apply_command(control_root: Path, session_id: str, turn_id: str) -> str:
    return (
        f"{shlex.quote(str(Path(sys.executable).resolve()))} {shlex.quote(str(APPLIER))} "
        f"--control-root {shlex.quote(str(control_root))} "
        f"--session-id {shlex.quote(session_id)} --turn-id {shlex.quote(turn_id)}"
    )


def authorize_question(control_root: Path, event: dict[str, Any], command: str) -> bool:
    tokens = questioner_tokens(command)
    if tokens is None:
        return False
    normalized = [str(Path(sys.executable).resolve()), *tokens[1:]]
    try:
        root_matches = (
            normalized[2] == "--control-root"
            and Path(normalized[3]).resolve(strict=False) == control_root
        )
    except (IndexError, OSError, RuntimeError, ValueError):
        root_matches = False
    remainder = normalized[4:]
    if not root_matches or len(remainder) % 2 or not remainder:
        deny("The approval question command is malformed.")
        return True
    proposal_ids = []
    for index in range(0, len(remainder), 2):
        if remainder[index] != "--proposal-id" or not re.fullmatch(r"P-\d{8}-\d{2}", remainder[index + 1]):
            deny("The approval question command is malformed.")
            return True
        proposal_ids.append(remainder[index + 1])
    proposal_ids = list(dict.fromkeys(proposal_ids))
    if not 1 <= len(proposal_ids) <= 3:
        deny("An approval question must contain one to three proposal IDs.")
        return True
    proposal_error = pending_proposal_error(control_root, proposal_ids)
    if proposal_error:
        deny(proposal_error)
        return True
    session_id = str(event.get("session_id") or "")
    turn_id = str(event.get("turn_id") or "")
    if not session_id or not turn_id:
        deny("Codex did not provide a session and turn ID for the approval question.")
        return True
    ensure_runtime(control_root)
    create_question(control_root, session_id, turn_id, proposal_ids)
    return True


def valid_receipt(
    control_root: Path,
    session_id: str,
    turn_id: str,
    prompt: str,
) -> tuple[dict[str, Any] | None, str | None]:
    receipt = read_json(receipt_path(control_root, session_id, turn_id))
    expected_prompt_hash = sha256_bytes(prompt.strip().encode("utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("session_id") != session_id
        or receipt.get("turn_id") != turn_id
        or receipt.get("prompt_sha256") != expected_prompt_hash
        or receipt.get("consumed_at")
    ):
        return None, "A matching unconsumed approval receipt was not found for this task and turn."
    proposal_ids = receipt.get("proposal_ids")
    if not isinstance(proposal_ids, list) or not proposal_ids or not all(isinstance(item, str) for item in proposal_ids):
        return None, "The approval receipt is invalid."
    try:
        if parse_iso(receipt["expires_at"]) <= now():
            return None, "The approval receipt expired."
    except Exception:
        return None, "The approval receipt is invalid."
    proposal_error = pending_proposal_error(control_root, proposal_ids)
    if proposal_error:
        return None, proposal_error
    return receipt, None


def authorize_apply(control_root: Path, config: dict[str, Any], event: dict[str, Any], tool_input: Any, command: str) -> bool:
    tokens = applier_tokens(command)
    if tokens is None:
        return False
    if not isinstance(tool_input, dict):
        deny("The deterministic applier requires structured Bash input.")
        return True

    session_id = str(event.get("session_id") or "")
    turn_id = str(event.get("turn_id") or "")
    prompt, prompt_error = current_user_prompt(config, event)
    if prompt_error:
        deny(prompt_error)
        return True
    assert prompt is not None

    normalized = [str(Path(sys.executable).resolve()), *tokens[1:]]
    root_matches = False
    if len(normalized) >= 4 and normalized[2] == "--control-root":
        try:
            root_matches = Path(normalized[3]).resolve(strict=False) == control_root
        except (OSError, RuntimeError, ValueError):
            pass
    is_direct = (
        len(normalized) == 8
        and root_matches
        and normalized[4:] == ["--session-id", session_id, "--turn-id", turn_id]
    )
    if is_direct:
        _, receipt_error = valid_receipt(control_root, session_id, turn_id, prompt)
        if receipt_error:
            deny(receipt_error)
            return True
        return True
    deny("The deterministic applier may only use the receipt-bound command issued after an active approval question.")
    return True


def user_prompt(control_root: Path, event: dict[str, Any]) -> int:
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        return 0
    session_id = str(event.get("session_id") or "")
    turn_id = str(event.get("turn_id") or "")
    if not session_id or not turn_id:
        return 0
    ensure_runtime(control_root)

    question = active_question(control_root, session_id)
    if question is None:
        return 0
    question_file, question_value = question
    decision = parse_approval_decision(prompt)
    if decision is None:
        question_value.update(
            {
                "status": "cancelled",
                "response_turn_id": turn_id,
                "response_prompt_sha256": sha256_bytes(prompt.strip().encode("utf-8")),
                "responded_at": iso(),
            }
        )
        atomic_json(question_file, question_value)
        output(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "The user's response was not an unambiguous yes or no, so the active "
                    "approval question is cancelled and nothing is approved or rejected. Address the user's "
                    "message normally. Register and ask a new task-bound question before any later application.",
                }
            }
        )
        return 0
    proposal_ids = list(question_value["proposal_ids"])
    question_value.update(
        {
            "status": "approved" if decision else "rejected",
            "response_turn_id": turn_id,
            "response_prompt_sha256": sha256_bytes(prompt.strip().encode("utf-8")),
            "responded_at": iso(),
        }
    )
    atomic_json(question_file, question_value)
    if not decision:
        rejected = []
        for proposal_id in proposal_ids:
            manifest_path, manifest = load_manifest(control_root, proposal_id)
            if manifest.get("status") == "pending":
                manifest.update({"status": "rejected", "rejected_at": iso()})
                atomic_json(manifest_path, manifest)
                rejected.append(proposal_id)
        output(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "The user declined the active task-bound approval question. "
                    f"The following proposals are now rejected and must not be applied: {', '.join(rejected)}. "
                    "Acknowledge the decision without running the applier.",
                }
            }
        )
        return 0

    proposal_error = pending_proposal_error(control_root, proposal_ids)
    if proposal_error:
        output({"decision": "block", "reason": proposal_error})
        return 0
    create_receipt(control_root, session_id, turn_id, prompt, proposal_ids)
    command = canonical_apply_command(control_root, session_id, turn_id)
    output(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "The user approved the proposal IDs bound to the active question. Run the following receipt-bound command once; do not edit targets directly or add IDs:\n" + command,
            }
        }
    )
    return 0


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(strings(item))
        return result
    return []


def paths_are_allowed(raw: str, roots: list[Path], allow_relative: bool = True) -> bool:
    if "../" in raw or raw.startswith("~") or "$HOME" in raw or "${HOME}" in raw:
        return False
    for token in re.findall(r"(?:/[^\s'\"\n]+)", raw):
        cleaned = token.rstrip(":,)")
        try:
            path = Path(cleaned).resolve(strict=False)
            if any(path == root or root in path.parents for root in roots):
                continue
            return False
        except (OSError, ValueError):
            return False
    return True


def is_python_executable(value: str) -> bool:
    executable = Path(value).expanduser()
    if executable.name not in {"python3", "python"} and not executable.name.startswith("python3."):
        return False
    if executable.is_absolute():
        try:
            return executable.resolve() == Path(sys.executable).resolve()
        except OSError:
            return False
    return executable.name in {"python3", "python"}


def allowed_python_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 2 or not is_python_executable(tokens[0]):
        return False
    script = Path(tokens[1])
    return script.parent == SKILL_SCRIPTS and script.name in ALLOWED_SCRIPT_NAMES


def allowed_read_command(command: str, roots: list[Path]) -> bool:
    if any(token in command for token in (">", "<", "|", ";", "&&", "||", "`", "$(", "\n")):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return True
    executable = Path(tokens[0]).name
    if executable == "git" and len(tokens) >= 2 and tokens[1] in {"status", "diff", "show", "rev-parse"}:
        return paths_are_allowed(command, roots)
    return executable in READ_ONLY_COMMANDS and paths_are_allowed(command, roots)


def direct_write_is_draft_only(tool: str, tool_input: Any, control_root: Path) -> bool:
    drafts = (control_root / "runtime" / "drafts").resolve()
    if tool == "apply_patch":
        combined = "\n".join(strings(tool_input))
        targets = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", combined, re.M)
        if not targets:
            return False
        for raw in targets:
            candidate = Path(raw.strip())
            resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (control_root / candidate).resolve(strict=False)
            if resolved != drafts and drafts not in resolved.parents:
                return False
        return True
    paths: list[str] = []
    if isinstance(tool_input, dict):
        for key, value in tool_input.items():
            if key.lower() in {"path", "file", "file_path", "filepath"} and isinstance(value, str):
                paths.append(value)
    if not paths:
        return False
    for raw in paths:
        candidate = Path(raw)
        resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (control_root / candidate).resolve(strict=False)
        if resolved != drafts and drafts not in resolved.parents:
            return False
    return True


def pre_tool(control_root: Path, config: dict[str, Any], event: dict[str, Any]) -> int:
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input")
    if tool == "Bash":
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        if authorize_question(control_root, event, command):
            return 0
        if authorize_apply(control_root, config, event, tool_input, command):
            return 0
        roots = [control_root, *configured_roots(config)]
        if allowed_python_command(command) or allowed_read_command(command, roots):
            return 0
        deny("The Codex improver control project permits shell writes only through its deterministic bundled scripts.")
        return 0
    if tool in {"apply_patch", "Edit", "Write"}:
        if direct_write_is_draft_only(tool, tool_input, control_root):
            return 0
        deny("Direct edits are limited to runtime/drafts; use deterministic scripts for proposals and approved target changes.")
        return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event", choices=("user-prompt", "pre-tool"))
    parser.add_argument("--control-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    control_root = args.control_root.resolve()
    config = load_config(control_root)
    if args.event == "user-prompt":
        return user_prompt(control_root, event)
    return pre_tool(control_root, config, event)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"codex-improver hook failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
