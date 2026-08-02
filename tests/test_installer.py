from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "scripts" / "install.py"


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.control = self.home / "projects" / "codex-improver"
        self.projects = self.home / "projects"
        self.environment = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / ".codex"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--control-root",
                str(self.control),
                "--project-root",
                str(self.projects),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=check,
            env=self.environment,
        )

    def test_dry_run_writes_nothing(self) -> None:
        result = self.run_installer("--dry-run")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "dry-run")
        self.assertFalse(self.control.exists())
        self.assertFalse((self.home / ".agents" / "skills" / "codex-improver").exists())

    def test_install_creates_private_control_project_and_skill(self) -> None:
        result = self.run_installer()
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "installed")
        self.assertTrue(payload["hook_trust_required"])
        self.assertTrue((self.control / "libexec" / "approval_prompt.py").is_file())
        self.assertTrue((self.control / "libexec" / "apply_proposals.py").is_file())
        self.assertTrue((self.control / ".codex" / "hooks.json").is_file())
        self.assertTrue((self.control / "automation-prompt.md").is_file())
        self.assertTrue((self.control / "scheduled-task.spec.toml").is_file())
        automation_prompt = (self.control / "automation-prompt.md").read_text(encoding="utf-8")
        self.assertEqual(
            automation_prompt,
            "Use `$codex-improver` to run the next safety-gated review of settled local and remote Codex sessions. "
            "Analyze only; do not apply proposals.\n",
        )
        self.assertEqual(payload["automation_spec"], str(self.control.resolve() / "scheduled-task.spec.toml"))
        task_spec = (self.control / "scheduled-task.spec.toml").read_text(encoding="utf-8")
        self.assertIn(f'project = "{self.control.resolve()}"', task_spec)
        config = json.loads((self.control / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["remote_hosts"], [])
        self.assertEqual(config["control_root"], str(self.control.resolve()))
        hook = json.loads((self.control / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        command = hook["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(str(self.control / "libexec" / "hook_dispatch.py"), command)
        prompt_command = hook["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("user-prompt", prompt_command)
        self.assertIn(str(self.control / "libexec" / "hook_dispatch.py"), prompt_command)
        skill = self.home / ".agents" / "skills" / "codex-improver"
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertFalse((skill / "scripts" / "tests").exists())

    def test_upgrade_preserves_config_and_runtime(self) -> None:
        self.run_installer()
        config_path = self.control / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["custom_test_value"] = "preserve-me"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(config_path, 0o644)
        marker = self.control / "runtime" / "findings" / "keep.json"
        marker.write_text("{}", encoding="utf-8")
        previous_spec = self.control / "scheduled-task.spec.toml"
        previous_spec.write_text("old task spec\n", encoding="utf-8")
        result = self.run_installer("--upgrade")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "upgraded")
        updated = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["custom_test_value"], "preserve-me")
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(marker.is_file())
        self.assertNotEqual(previous_spec.read_text(encoding="utf-8"), "old task spec\n")
        self.assertIsNotNone(payload["managed_backup"])
        self.assertIsNotNone(payload["standalone_skill_backup"])

    def test_rejects_home_as_project_root(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--control-root",
                str(self.control),
                "--project-root",
                str(self.home),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=self.environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be narrower", result.stderr)


if __name__ == "__main__":
    unittest.main()
