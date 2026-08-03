from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
TEST_SECRET = "sk-" + "abcdefghijklmnopqrstuvwxyz"

from improver_lib import (
    parse_approval_decision,
    parse_session,
    question_path,
    read_json,
    redact_text,
    receipt_path,
    runtime_dir,
    validate_target,
)
from apply_proposals import apply_one
from host_discovery import concrete_aliases, saved_remote_projects, sync_discovered_hosts
from proposal_tool import target_snapshot
from remote_transport import BOOTSTRAP_CODE, remote_hosts
from session_batch import discover_all, processed_for


class ImproverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = self.root / "control"
        self.sessions = self.root / "sessions"
        self.project = self.root / "project"
        self.control.mkdir()
        self.sessions.mkdir()
        self.project.mkdir()
        self.config = {
            "schema_version": 1,
            "control_root": str(self.control),
            "sessions_root": str(self.sessions),
            "bootstrap_days": 7,
            "settle_seconds": 0,
            "retention_days": 90,
            "proposal_expiry_days": 14,
            "max_sessions_per_run": 8,
            "max_proposals_per_run": 3,
            "writable_roots": [str(self.project)],
        }
        (self.control / "config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, name: str, *args: str, input_value: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            input=json.dumps(input_value) if input_value is not None else None,
            text=True,
            capture_output=True,
            check=check,
        )

    def remote_worker(self, action: str, request: dict, check: bool = True) -> subprocess.CompletedProcess:
        worker_root = self.root / "remote-home" / ".codex" / "codex-improver"
        worker_root.mkdir(parents=True, exist_ok=True)
        worker = worker_root / "remote_worker.py"
        if not worker.exists():
            shutil.copyfile(SCRIPTS / "remote_worker.py", worker)
        remote_home = self.root / "remote-home"
        remote_sessions = remote_home / ".codex" / "sessions"
        remote_project = remote_home / "projects"
        remote_sessions.mkdir(parents=True, exist_ok=True)
        remote_project.mkdir(parents=True, exist_ok=True)
        config = {
            "schema_version": 2,
            "host_id": "test-remote",
            "home": str(remote_home),
            "sessions_root": str(remote_sessions),
            "worker_root": str(worker_root),
            "allowed_roots": [str(remote_project)],
            "excluded_cwds": [str(worker_root)],
        }
        (worker_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(worker), action],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            check=check,
        )

    def write_session(self, name: str = "session.jsonl", cwd: Path | None = None) -> Path:
        path = self.sessions / name
        records = [
            {
                "timestamp": "2026-08-01T10:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "thr-test", "cwd": str(cwd or self.project), "source": "cli"},
            },
            {
                "timestamp": "2026-08-01T10:00:01Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Use token {TEST_SECRET} and email me@example.com"}]},
            },
            {
                "timestamp": "2026-08-01T10:00:02Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "ERROR: command failed"},
            },
            {"timestamp": "2026-08-01T10:00:03Z", "type": "future_event", "payload": {"value": 1}},
        ]
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n{truncated", encoding="utf-8")
        return path

    def create_draft(self, target: Path, new_content: str) -> Path:
        draft_dir = self.control / "runtime" / "drafts"
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft = draft_dir / "draft.json"
        draft.write_text(
            json.dumps(
                {
                    "summary": "Improve instructions",
                    "root_cause": "A concrete correction exposed a gap",
                    "evidence": ["Session thr-test required a correction"],
                    "source_session_ids": ["thr-test"],
                    "risk": "Low",
                    "rollback": "Restore previous content",
                    "changes": [{"path": str(target), "new_content": new_content}],
                }
            ),
            encoding="utf-8",
        )
        return draft

    def proposal_id(self, output: str) -> str:
        return json.loads(output)["id"]

    def write_turn_transcript(
        self,
        prompt: str,
        session_id: str = "session-a",
        turn_id: str = "turn-a",
        assistant_text: str | None = None,
    ) -> Path:
        path = self.sessions / f"rollout-{session_id}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"session_id": session_id, "cwd": str(self.control)}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
        ]
        if assistant_text is not None:
            records.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": assistant_text}],
                    },
                }
            )
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        return path

    def approval_question_command(self, *proposal_ids: str) -> str:
        suffix = " ".join(f"--proposal-id {proposal_id}" for proposal_id in proposal_ids)
        return (
            f"{sys.executable} {SCRIPTS / 'approval_prompt.py'} "
            f"--control-root {self.control} {suffix}"
        )

    def ask_approval(
        self,
        *proposal_ids: str,
        session_id: str = "session-a",
        turn_id: str = "question-turn",
    ) -> None:
        event = {
            "session_id": session_id,
            "turn_id": turn_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": self.approval_question_command(*proposal_ids)},
        }
        result = self.run_script(
            "hook_dispatch.py",
            "pre-tool",
            "--control-root",
            str(self.control),
            input_value=event,
        )
        self.assertEqual(result.stdout, "")
        question = read_json(question_path(self.control, session_id))
        self.assertEqual(question["proposal_ids"], list(proposal_ids))

    def approve(self, proposal_id: str, session_id: str = "session-a", turn_id: str = "turn-a") -> None:
        self.ask_approval(proposal_id, session_id=session_id, turn_id=f"{turn_id}-question")
        result = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": "yes", "session_id": session_id, "turn_id": turn_id},
        )
        payload = json.loads(result.stdout)
        self.assertIn(f"--session-id {session_id} --turn-id {turn_id}", payload["hookSpecificOutput"]["additionalContext"])
        receipt = read_json(receipt_path(self.control, session_id, turn_id))
        self.assertEqual(receipt["proposal_ids"], [proposal_id])

    def test_redaction(self) -> None:
        value = redact_text("Bearer abcdefghijklmnopqrstuvwxyz api_key=supersecret me@example.com")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", value)
        self.assertNotIn("supersecret", value)
        self.assertNotIn("me@example.com", value)

    def test_natural_approval_decisions_are_bounded(self) -> None:
        self.assertIs(parse_approval_decision("Аппрувлю эту правку"), True)
        self.assertIs(parse_approval_decision("yes!"), True)
        self.assertIs(parse_approval_decision("Нет"), False)
        self.assertIsNone(parse_approval_decision("yes, but change the target"))
        self.assertIsNone(parse_approval_decision("APPROVE P-20260802-01"))

    def test_parser_tolerates_unknown_and_truncated_records(self) -> None:
        path = self.write_session()
        parsed = parse_session(path, self.config)
        self.assertEqual(parsed["session_id"], "thr-test")
        self.assertEqual(parsed["malformed_records"], 1)
        self.assertEqual(parsed["unknown_event_counts"]["future_event"], 1)
        serialized = json.dumps(parsed)
        self.assertNotIn(TEST_SECRET, serialized)
        self.assertNotIn("me@example.com", serialized)
        self.assertTrue(parsed["errors"])

    def test_batch_is_incremental_and_retains_no_content(self) -> None:
        path = self.write_session()
        first = self.run_script("session_batch.py", "start", "--control-root", str(self.control))
        batch = json.loads(first.stdout)
        self.assertEqual(len(batch["sessions"]), 1)
        batch_state = next((runtime_dir(self.control) / "batches").glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("sk-", batch_state)
        findings = self.control / "runtime" / "drafts" / "findings.json"
        findings.parent.mkdir(parents=True, exist_ok=True)
        findings.write_text(json.dumps({"clusters": [], "created_proposals": []}), encoding="utf-8")
        self.run_script(
            "session_batch.py",
            "complete",
            "--control-root",
            str(self.control),
            "--batch-id",
            batch["batch_id"],
            "--findings",
            str(findings),
        )
        second = self.run_script("session_batch.py", "start", "--control-root", str(self.control))
        self.assertEqual(json.loads(second.stdout)["sessions"], [])

    def test_recent_sessions_can_be_reprocessed_after_completion(self) -> None:
        self.write_session()
        first = json.loads(self.run_script("session_batch.py", "start", "--control-root", str(self.control)).stdout)
        findings = self.control / "runtime" / "drafts" / "findings.json"
        findings.parent.mkdir(parents=True, exist_ok=True)
        findings.write_text(json.dumps({"clusters": [], "created_proposals": []}), encoding="utf-8")
        self.run_script(
            "session_batch.py",
            "complete",
            "--control-root",
            str(self.control),
            "--batch-id",
            first["batch_id"],
            "--findings",
            str(findings),
        )

        replay = self.run_script(
            "session_batch.py",
            "start",
            "--control-root",
            str(self.control),
            "--reprocess-days",
            "1",
        )
        payload = json.loads(replay.stdout)
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertEqual(payload["selection"]["mode"], "reprocess")
        self.assertEqual(payload["selection"]["days"], 1)
        findings.write_text(json.dumps({"clusters": [], "created_proposals": []}), encoding="utf-8")
        completed = json.loads(self.run_script(
            "session_batch.py",
            "complete",
            "--control-root",
            str(self.control),
            "--batch-id",
            payload["batch_id"],
            "--findings",
            str(findings),
        ).stdout)
        stored = read_json(Path(completed["findings"]))
        self.assertEqual(stored["selection"]["mode"], "reprocess")

    def test_reprocessing_window_excludes_older_sessions(self) -> None:
        path = self.write_session()
        old = time.time() - 2 * 86400
        os.utime(path, (old, old))
        result = self.run_script(
            "session_batch.py",
            "start",
            "--control-root",
            str(self.control),
            "--reprocess-days",
            "1",
        )
        self.assertEqual(json.loads(result.stdout)["sessions"], [])

    def test_reprocessing_rejects_a_different_pending_batch(self) -> None:
        self.write_session()
        self.run_script("session_batch.py", "start", "--control-root", str(self.control))
        result = self.run_script(
            "session_batch.py",
            "start",
            "--control-root",
            str(self.control),
            "--reprocess-days",
            "1",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different batch is pending", result.stderr)

    def test_settling_session_is_deferred(self) -> None:
        self.config["settle_seconds"] = 300
        (self.control / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.write_session()
        result = self.run_script("session_batch.py", "start", "--control-root", str(self.control))
        self.assertEqual(json.loads(result.stdout)["sessions"], [])

    def test_self_session_is_excluded(self) -> None:
        self.write_session(cwd=self.control)
        result = self.run_script("session_batch.py", "start", "--control-root", str(self.control))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(payload["skipped"][0]["reason"], "improver-session")

    def test_target_allowlist(self) -> None:
        allowed = self.project / "docs" / "guide.md"
        self.assertEqual(validate_target(allowed, self.config), allowed.resolve())
        with self.assertRaises(ValueError):
            validate_target(self.project / "src" / "app.py", self.config)
        with self.assertRaises(ValueError):
            validate_target(self.project / ".codex" / "config.toml", self.config)

    def test_remote_host_config_is_explicit_and_strict(self) -> None:
        self.config["remote_hosts"] = [{"id": "dev-1", "ssh_target": "dev.example", "worker_path": "/home/me/worker.py"}]
        self.assertEqual(remote_hosts(self.config)["dev-1"]["ssh_target"], "dev.example")
        self.config["remote_hosts"][0]["ssh_target"] = "-oProxyCommand=bad"
        with self.assertRaises(RuntimeError):
            remote_hosts(self.config)

    def test_ssh_discovery_uses_concrete_aliases_and_includes(self) -> None:
        ssh_dir = self.root / "ssh"
        ssh_dir.mkdir()
        included = ssh_dir / "included.conf"
        included.write_text("Host included-host\n  HostName included.example\nHost pattern-*\n", encoding="utf-8")
        config = ssh_dir / "config"
        config.write_text(
            f"Include {included}\nHost devbox alias-two\n  HostName dev.example\nHost *\n  ForwardAgent no\nHost !negated\n",
            encoding="utf-8",
        )
        self.assertEqual(concrete_aliases(config), ["included-host", "devbox", "alias-two"])

    def test_saved_remote_projects_are_feature_detected(self) -> None:
        state = self.root / "state.json"
        state.write_text(json.dumps({"remote-projects": [
            {"hostId": "remote-control:one", "remotePath": "/srv/project", "label": "Project"},
            {"hostId": "ignored", "remotePath": "relative"},
        ]}), encoding="utf-8")
        self.assertEqual(saved_remote_projects(state), [{"path": "/srv/project", "app_host_id": "remote-control:one", "label": "Project"}])

    def test_discovery_correlates_ssh_alias_with_saved_codex_project(self) -> None:
        state = self.root / "state.json"
        state.write_text(json.dumps({"remote-projects": [
            {"hostId": "remote-control:one", "remotePath": "/srv/project", "label": "Project"},
        ]}), encoding="utf-8")
        self.config["remote_auto_discovery"] = {
            "enabled": True,
            "ssh_config": str(self.root / "ssh-config"),
            "app_state": str(state),
        }
        with mock.patch("host_discovery.concrete_aliases", return_value=["devbox"]), mock.patch(
            "host_discovery.resolve_alias", return_value={"hostname": "dev.example", "user": "me", "port": "22"}
        ), mock.patch("host_discovery.probe", return_value={
            "home": "/home/me",
            "sessions_root": "/home/me/.codex/sessions",
            "sessions_readable": True,
            "matched_project_paths": ["/srv/project"],
        }):
            result = sync_discovered_hosts(self.control, self.config)
        self.assertEqual(len(result["discovered"]), 1)
        host_id = result["discovered"][0]["id"]
        self.assertTrue(host_id.startswith("ssh-"))
        self.assertEqual(remote_hosts(self.config)[host_id]["worker_config"]["allowed_roots"], ["/srv/project"])

    def test_ephemeral_worker_runs_without_remote_installation(self) -> None:
        remote_home = self.root / "ephemeral-home"
        sessions = remote_home / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        worker_root = remote_home / ".codex" / "codex-improver"
        config = {
            "schema_version": 2,
            "host_id": "ssh-test",
            "home": str(remote_home),
            "sessions_root": str(sessions),
            "worker_root": str(worker_root),
            "allowed_roots": [str(remote_home / "project")],
            "excluded_cwds": [str(worker_root)],
        }
        envelope = {
            "action": "diagnose",
            "source": (SCRIPTS / "remote_worker.py").read_text(encoding="utf-8"),
            "virtual_file": str(worker_root / "remote_worker.py"),
            "config": config,
            "request": {},
        }
        result = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP_CODE],
            input=json.dumps(envelope),
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["worker_version"], 2)

    def test_cursor_migrates_local_and_separates_remote_hosts(self) -> None:
        cursor = {"schema_version": 1, "processed": {"local.jsonl": {"size": 1, "mtime_ns": 2}}}
        self.assertIn("local.jsonl", processed_for(cursor, "local"))
        processed_for(cursor, "dev-1")["remote.jsonl"] = {"size": 3, "mtime_ns": 4}
        self.assertNotIn("remote.jsonl", processed_for(cursor, "local"))

    def test_batch_selection_round_robins_hosts(self) -> None:
        local_items = [{"host": "local", "path": f"l-{i}", "mtime": i, "fingerprint": {}} for i in range(8)]
        remote_items = [{"path": f"r-{i}", "mtime": i, "fingerprint": {}} for i in range(8)]
        self.config["max_sessions_per_run"] = 4
        self.config["remote_hosts"] = [{"id": "dev-1", "ssh_target": "dev.example", "worker_path": "/home/me/worker.py"}]
        with mock.patch("session_batch.discover_local", return_value=local_items), mock.patch(
            "session_batch.call_remote", return_value={"items": remote_items}
        ):
            selected, errors = discover_all(self.config, {"schema_version": 2, "hosts": {}})
        self.assertEqual([item["host"] for item in selected], ["local", "dev-1", "local", "dev-1"])
        self.assertEqual(errors, [])

    def test_remote_worker_redacts_before_returning_session(self) -> None:
        remote_home = self.root / "remote-home"
        session = remote_home / ".codex" / "sessions" / "rollout.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session_meta", "payload": {"session_id": "remote-thread", "cwd": str(remote_home / "projects"), "source": "vscode"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"token {TEST_SECRET} me@example.com"}]}},
        ]
        session.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
        discovered = json.loads(self.remote_worker("discover", {"processed": {}, "settle_seconds": 0, "bootstrap_days": 7, "limit": 8}).stdout)
        extracted = self.remote_worker("extract", {"items": discovered["items"]})
        self.assertNotIn(TEST_SECRET, extracted.stdout)
        self.assertNotIn("me@example.com", extracted.stdout)
        self.assertEqual(json.loads(extracted.stdout)["sessions"][0]["session_id"], "remote-thread")

    def test_remote_worker_can_reprocess_a_processed_recent_session(self) -> None:
        remote_home = self.root / "remote-home"
        session = remote_home / ".codex" / "sessions" / "rollout.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": "remote-thread", "cwd": str(remote_home / "projects")}}) + "\n",
            encoding="utf-8",
        )
        first = json.loads(self.remote_worker("discover", {
            "processed": {}, "settle_seconds": 0, "bootstrap_days": 7, "limit": 8,
        }).stdout)
        processed = {first["items"][0]["path"]: first["items"][0]["fingerprint"]}
        incremental = json.loads(self.remote_worker("discover", {
            "processed": processed, "settle_seconds": 0, "bootstrap_days": 7, "limit": 8,
        }).stdout)
        replay = json.loads(self.remote_worker("discover", {
            "processed": processed,
            "settle_seconds": 0,
            "bootstrap_days": 7,
            "limit": 8,
            "reprocess_since": time.time() - 86400,
        }).stdout)
        self.assertEqual(incremental["items"], [])
        self.assertEqual(len(replay["items"]), 1)

    def test_remote_worker_hash_bound_apply_and_stale_detection(self) -> None:
        target = self.root / "remote-home" / "projects" / "AGENTS.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("before\n", encoding="utf-8")
        snapshot = json.loads(self.remote_worker("inspect", {"path": str(target)}).stdout)
        desired = "after\n"
        request = {
            "proposal_id": "P-20260802-90",
            "changes": [{
                "path": str(target),
                "base_sha256": snapshot["sha256"],
                "desired_sha256": __import__("hashlib").sha256(desired.encode()).hexdigest(),
                "desired_content": desired,
                "mode": 0o644,
            }],
        }
        applied = json.loads(self.remote_worker("apply", request).stdout)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(target.read_text(encoding="utf-8"), desired)
        stale_request = dict(request)
        stale_request["proposal_id"] = "P-20260802-91"
        stale = json.loads(self.remote_worker("apply", stale_request).stdout)
        self.assertEqual(stale["status"], "stale")

    def test_remote_target_snapshot_uses_worker_and_keeps_host_binding(self) -> None:
        self.config["remote_hosts"] = [{"id": "dev-1", "ssh_target": "dev.example", "worker_path": "/home/me/worker.py"}]
        with mock.patch("proposal_tool.inspect_remote_target", return_value={"path": "/remote/AGENTS.md", "exists": False, "content": "", "sha256": None, "mode": 0o644}) as inspect:
            snapshot = target_snapshot(self.config, "dev-1", "/remote/AGENTS.md")
        inspect.assert_called_once_with(self.config, "dev-1", "/remote/AGENTS.md")
        self.assertFalse(snapshot["exists"])

    def test_general_feedback_records_cross_host_direction(self) -> None:
        target = self.project / "AGENTS.md"
        target.write_text("before\n", encoding="utf-8")
        draft = self.create_draft(target, "after\n")
        value = json.loads(draft.read_text(encoding="utf-8"))
        value.update({
            "feedback_scope": "general",
            "source_hosts": ["dev-1"],
            "source_session_ids": ["dev-1:session-a"],
        })
        draft.write_text(json.dumps(value), encoding="utf-8")
        created = self.run_script("proposal_tool.py", "create", "--control-root", str(self.control), "--draft", str(draft))
        manifest = json.loads(created.stdout)
        self.assertEqual(manifest["feedback_scope"], "general")
        self.assertEqual(manifest["source_hosts"], ["dev-1"])
        self.assertEqual(manifest["transfer_directions"], ["dev-1->local"])

    def test_remote_proposal_dispatches_only_frozen_host_bound_content(self) -> None:
        self.config["remote_hosts"] = [{"id": "dev-1", "ssh_target": "dev.example", "worker_path": "/home/me/worker.py"}]
        proposal_id = "P-20260802-92"
        directory = runtime_dir(self.control) / "proposals" / proposal_id
        (directory / "files").mkdir(parents=True)
        desired = "remote improvement\n"
        desired_hash = __import__("hashlib").sha256(desired.encode()).hexdigest()
        (directory / "files" / "001.txt").write_text(desired, encoding="utf-8")
        manifest = {
            "schema_version": 2,
            "id": proposal_id,
            "status": "pending",
            "target_host": "dev-1",
            "expiry_at": "2099-01-01T00:00:00Z",
            "changes": [{
                "path": "/home/me/projects/AGENTS.md",
                "base_sha256": None,
                "desired_sha256": desired_hash,
                "desired_content_file": "files/001.txt",
                "mode": 0o644,
            }],
        }
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        response = {"status": "applied", "changes": ["/home/me/projects/AGENTS.md"], "validation": [{"ok": True}], "backup": "/remote/backup"}
        with mock.patch("apply_proposals.call_remote", return_value=response) as remote_apply:
            result = apply_one(self.control, self.config, proposal_id)
        self.assertEqual(result["status"], "applied")
        call = remote_apply.call_args
        self.assertEqual(call.args[1:3], ("dev-1", "apply"))
        self.assertEqual(call.args[3]["changes"][0]["desired_content"], desired)
        self.assertEqual(read_json(directory / "manifest.json")["status"], "applied")

    def test_approved_proposal_applies_exact_content(self) -> None:
        target = self.project / "AGENTS.md"
        target.write_text("before\n", encoding="utf-8")
        draft = self.create_draft(target, "after\n")
        created = self.run_script("proposal_tool.py", "create", "--control-root", str(self.control), "--draft", str(draft))
        proposal_id = self.proposal_id(created.stdout)
        self.approve(proposal_id)
        applied = self.run_script(
            "apply_proposals.py",
            "--control-root",
            str(self.control),
            "--session-id",
            "session-a",
            "--turn-id",
            "turn-a",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(json.loads(applied.stdout)["results"][0]["status"], "applied")
        receipt = read_json(receipt_path(self.control, "session-a", "turn-a"))
        self.assertIsNotNone(receipt["consumed_at"])

    def test_task_bound_question_accepts_natural_yes_and_applies(self) -> None:
        target = self.project / "natural.md"
        target.write_text("before\n", encoding="utf-8")
        proposal_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(target, "after\n")),
            ).stdout
        )
        self.ask_approval(proposal_id)
        rendered = self.run_script(
            "approval_prompt.py",
            "--control-root",
            str(self.control),
            "--proposal-id",
            proposal_id,
        )
        self.assertIn("Reply yes or no", json.loads(rendered.stdout)["question"])

        prompt = "Аппрувлю эту правку"
        hook = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": prompt, "session_id": "session-a", "turn_id": "answer-turn"},
        )
        context = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("--session-id session-a --turn-id answer-turn", context)
        receipt = read_json(receipt_path(self.control, "session-a", "answer-turn"))
        self.assertEqual(receipt["proposal_ids"], [proposal_id])
        self.assertEqual(receipt["approval_kind"], "question-response")

        transcript = self.write_turn_transcript(prompt, "session-a", "answer-turn")
        direct = (
            f"{sys.executable} {SCRIPTS / 'apply_proposals.py'} "
            f"--control-root {self.control} --session-id session-a --turn-id answer-turn"
        )
        authorized = self.run_script(
            "hook_dispatch.py",
            "pre-tool",
            "--control-root",
            str(self.control),
            input_value={
                "session_id": "session-a",
                "turn_id": "answer-turn",
                "transcript_path": str(transcript),
                "tool_name": "Bash",
                "tool_input": {"command": direct},
            },
        )
        self.assertEqual(authorized.stdout, "")
        applied = self.run_script(
            "apply_proposals.py",
            "--control-root",
            str(self.control),
            "--session-id",
            "session-a",
            "--turn-id",
            "answer-turn",
        )
        self.assertEqual(json.loads(applied.stdout)["results"][0]["status"], "applied")
        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")

    def test_task_bound_question_rejects_on_natural_no(self) -> None:
        target = self.project / "rejected.md"
        target.write_text("before\n", encoding="utf-8")
        proposal_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(target, "after\n")),
            ).stdout
        )
        self.ask_approval(proposal_id)
        hook = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": "нет", "session_id": "session-a", "turn_id": "answer-turn"},
        )
        self.assertIn("must not be applied", json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"])
        manifest = read_json(runtime_dir(self.control) / "proposals" / proposal_id / "manifest.json")
        self.assertEqual(manifest["status"], "rejected")
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertFalse(receipt_path(self.control, "session-a", "answer-turn").exists())

    def test_natural_yes_without_task_bound_question_is_ignored(self) -> None:
        result = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": "yes", "session_id": "session-a", "turn_id": "answer-turn"},
        )
        self.assertEqual(result.stdout, "")
        self.assertFalse(receipt_path(self.control, "session-a", "answer-turn").exists())

    def test_natural_yes_cannot_approve_another_tasks_question(self) -> None:
        target = self.project / "cross-task.md"
        target.write_text("before\n", encoding="utf-8")
        proposal_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(target, "after\n")),
            ).stdout
        )
        self.ask_approval(proposal_id, session_id="session-a")
        result = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": "yes", "session_id": "session-b", "turn_id": "answer-turn"},
        )
        self.assertEqual(result.stdout, "")
        self.assertFalse(receipt_path(self.control, "session-b", "answer-turn").exists())

    def test_qualified_answer_cancels_question_instead_of_approving_later_yes(self) -> None:
        target = self.project / "qualified.md"
        target.write_text("before\n", encoding="utf-8")
        proposal_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(target, "after\n")),
            ).stdout
        )
        self.ask_approval(proposal_id)
        ambiguous = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={
                "prompt": "yes, but change the target",
                "session_id": "session-a",
                "turn_id": "ambiguous-turn",
            },
        )
        self.assertIn("question is cancelled", json.loads(ambiguous.stdout)["hookSpecificOutput"]["additionalContext"])
        question = read_json(question_path(self.control, "session-a"))
        self.assertEqual(question["status"], "cancelled")

        later = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": "yes", "session_id": "session-a", "turn_id": "later-turn"},
        )
        self.assertEqual(later.stdout, "")
        self.assertFalse(receipt_path(self.control, "session-a", "later-turn").exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    def test_removed_approval_command_cancels_active_question(self) -> None:
        target = self.project / "asked.md"
        target.write_text("before\n", encoding="utf-8")
        proposal_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(target, "after\n")),
            ).stdout
        )
        self.ask_approval(proposal_id)
        removed = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={
                "prompt": f"APPROVE {proposal_id}",
                "session_id": "session-a",
                "turn_id": "removed-command-turn",
            },
        )
        self.assertIn("question is cancelled", json.loads(removed.stdout)["hookSpecificOutput"]["additionalContext"])
        question = read_json(question_path(self.control, "session-a"))
        self.assertEqual(question["status"], "cancelled")
        self.assertFalse(receipt_path(self.control, "session-a", "removed-command-turn").exists())

    def test_hash_mismatch_marks_stale_without_writing(self) -> None:
        target = self.project / "AGENTS.md"
        target.write_text("before\n", encoding="utf-8")
        draft = self.create_draft(target, "after\n")
        created = self.run_script("proposal_tool.py", "create", "--control-root", str(self.control), "--draft", str(draft))
        proposal_id = self.proposal_id(created.stdout)
        target.write_text("user changed it\n", encoding="utf-8")
        self.approve(proposal_id)
        applied = self.run_script(
            "apply_proposals.py",
            "--control-root",
            str(self.control),
            "--session-id",
            "session-a",
            "--turn-id",
            "turn-a",
            check=False,
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "user changed it\n")
        self.assertEqual(json.loads(applied.stdout)["results"][0]["status"], "stale")

    def test_failed_skill_validation_rolls_back(self) -> None:
        target = self.project / ".agents" / "skills" / "bad-skill" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\nname: bad-skill\ndescription: valid\n---\n", encoding="utf-8")
        draft = self.create_draft(target, "not valid frontmatter\n")
        created = self.run_script("proposal_tool.py", "create", "--control-root", str(self.control), "--draft", str(draft))
        proposal_id = self.proposal_id(created.stdout)
        self.approve(proposal_id)
        applied = self.run_script(
            "apply_proposals.py",
            "--control-root",
            str(self.control),
            "--session-id",
            "session-a",
            "--turn-id",
            "turn-a",
            check=False,
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "---\nname: bad-skill\ndescription: valid\n---\n")
        self.assertEqual(json.loads(applied.stdout)["results"][0]["status"], "failed")

    def test_removed_approval_command_without_question_is_ignored(self) -> None:
        result = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": "APPROVE P-20260802-99", "session_id": "s", "turn_id": "t"},
        )
        self.assertEqual(result.stdout, "")
        self.assertFalse(receipt_path(self.control, "s", "t").exists())

    def test_pretool_rejects_removed_current_approval_flag(self) -> None:
        transcript = self.write_turn_transcript("yes", "s", "t")
        removed_command = (
            f"{sys.executable} {SCRIPTS / 'apply_proposals.py'} "
            f"--control-root {self.control} --from-current-approval"
        )
        result = self.run_script(
            "hook_dispatch.py",
            "pre-tool",
            "--control-root",
            str(self.control),
            input_value={
                "session_id": "s",
                "turn_id": "t",
                "transcript_path": str(transcript),
                "tool_name": "Bash",
                "tool_input": {"command": removed_command},
            },
        )
        self.assertIn("receipt-bound command issued after an active approval question", result.stdout)
        self.assertFalse(receipt_path(self.control, "s", "t").exists())

    def test_pretool_rejects_cross_task_transcript(self) -> None:
        transcript = self.write_turn_transcript("yes", "other-session", "t")
        direct = (
            f"{sys.executable} {SCRIPTS / 'apply_proposals.py'} "
            f"--control-root {self.control} --session-id s --turn-id t"
        )
        result = self.run_script(
            "hook_dispatch.py",
            "pre-tool",
            "--control-root",
            str(self.control),
            input_value={
                "session_id": "s",
                "turn_id": "t",
                "transcript_path": str(transcript),
                "tool_name": "Bash",
                "tool_input": {"command": direct},
            },
        )
        self.assertIn("another task", result.stdout)
        self.assertFalse(receipt_path(self.control, "s", "t").exists())

    def test_pretool_rejects_direct_apply_without_receipt(self) -> None:
        target = self.project / "direct.md"
        target.write_text("before\n", encoding="utf-8")
        proposal_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(target, "after\n")),
            ).stdout
        )
        transcript = self.write_turn_transcript("yes", "s", "t")
        direct = (
            f"{sys.executable} {SCRIPTS / 'apply_proposals.py'} "
            f"--control-root {self.control} --session-id s --turn-id t"
        )
        result = self.run_script(
            "hook_dispatch.py",
            "pre-tool",
            "--control-root",
            str(self.control),
            input_value={
                "session_id": "s",
                "turn_id": "t",
                "transcript_path": str(transcript),
                "tool_name": "Bash",
                "tool_input": {"command": direct},
            },
        )
        self.assertIn("matching unconsumed approval receipt", result.stdout)

    def test_pretool_allows_receipt_bound_direct_apply_after_question(self) -> None:
        target = self.project / "question.md"
        target.write_text("before\n", encoding="utf-8")
        proposal_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(target, "after\n")),
            ).stdout
        )
        self.ask_approval(proposal_id, session_id="s")
        prompt = "yes"
        transcript = self.write_turn_transcript(prompt, "s", "t")
        prompt_result = self.run_script(
            "hook_dispatch.py",
            "user-prompt",
            "--control-root",
            str(self.control),
            input_value={"prompt": prompt, "session_id": "s", "turn_id": "t"},
        )
        self.assertIn("receipt-bound command once", json.loads(prompt_result.stdout)["hookSpecificOutput"]["additionalContext"])
        direct = (
            f"{sys.executable} {SCRIPTS / 'apply_proposals.py'} "
            f"--control-root {self.control} --session-id s --turn-id t"
        )
        result = self.run_script(
            "hook_dispatch.py",
            "pre-tool",
            "--control-root",
            str(self.control),
            input_value={
                "session_id": "s",
                "turn_id": "t",
                "transcript_path": str(transcript),
                "tool_name": "Bash",
                "tool_input": {"command": direct},
            },
        )
        self.assertEqual(result.stdout, "")

    def test_assistant_approval_text_is_not_treated_as_user_approval(self) -> None:
        transcript = self.write_turn_transcript("Review the proposal", "s", "t", "yes")
        direct = (
            f"{sys.executable} {SCRIPTS / 'apply_proposals.py'} "
            f"--control-root {self.control} --session-id s --turn-id t"
        )
        result = self.run_script(
            "hook_dispatch.py",
            "pre-tool",
            "--control-root",
            str(self.control),
            input_value={
                "session_id": "s",
                "turn_id": "t",
                "transcript_path": str(transcript),
                "tool_name": "Bash",
                "tool_input": {"command": direct},
            },
        )
        self.assertIn("matching unconsumed approval receipt", result.stdout)

    def test_pretool_allows_drafts_and_denies_control_or_external_writes(self) -> None:
        allowed_patch = {
            "tool_name": "apply_patch",
            "tool_input": {"command": "*** Begin Patch\n*** Add File: runtime/drafts/a.json\n+{}\n*** End Patch"},
        }
        allowed = self.run_script(
            "hook_dispatch.py", "pre-tool", "--control-root", str(self.control), input_value=allowed_patch
        )
        self.assertEqual(allowed.stdout, "")
        denied_patch = {
            "tool_name": "apply_patch",
            "tool_input": {"command": "*** Begin Patch\n*** Update File: config.json\n+unsafe\n*** End Patch"},
        }
        denied = self.run_script(
            "hook_dispatch.py", "pre-tool", "--control-root", str(self.control), input_value=denied_patch
        )
        self.assertIn('"permissionDecision": "deny"', denied.stdout)
        external = {
            "tool_name": "Bash",
            "tool_input": {"command": f"cp file {self.project / 'AGENTS.md'}"},
        }
        denied_external = self.run_script(
            "hook_dispatch.py", "pre-tool", "--control-root", str(self.control), input_value=external
        )
        self.assertIn('"permissionDecision": "deny"', denied_external.stdout)

    def test_partial_multi_approval(self) -> None:
        first = self.project / "first.md"
        second = self.project / "second.md"
        first.write_text("one\n", encoding="utf-8")
        second.write_text("two\n", encoding="utf-8")
        first_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(first, "ONE\n")),
            ).stdout
        )
        second_id = self.proposal_id(
            self.run_script(
                "proposal_tool.py",
                "create",
                "--control-root",
                str(self.control),
                "--draft",
                str(self.create_draft(second, "TWO\n")),
            ).stdout
        )
        self.approve(first_id)
        self.run_script(
            "apply_proposals.py",
            "--control-root",
            str(self.control),
            "--session-id",
            "session-a",
            "--turn-id",
            "turn-a",
        )
        self.assertEqual(first.read_text(encoding="utf-8"), "ONE\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "two\n")
        second_manifest = read_json(runtime_dir(self.control) / "proposals" / second_id / "manifest.json")
        self.assertEqual(second_manifest["status"], "pending")


if __name__ == "__main__":
    unittest.main()
