# Setup and upgrade

## Initial setup

1. Resolve `<skill-root>` as the directory containing `SKILL.md`.
2. Ask for the intended control-project path only when the user has not supplied one. Default to `$HOME/projects/codex-improver`.
3. Run the bundled installer with absolute paths. Add each repository parent that proposals may target with a separate `--project-root` argument:

   ```text
   python3 <skill-root>/scripts/install.py --control-root <control-root> --project-root <projects-root>
   ```

4. Report the generated files. The improver installs no hooks and requires no hook-trust onboarding.
5. Create a standalone local scheduled task through the Codex app automation capability. Use the generated one-line prompt at `<control-root>/automation-prompt.md` and the portable source specification at `<control-root>/scheduled-task.spec.toml`, select the control project, and use the user's requested cadence. If none is supplied, recommend daily at 12:45 in the user's local timezone with a high-reasoning model. Keep the workflow in this skill rather than expanding it into the task prompt. Treat the Codex app as the runtime source of truth; do not copy the portable specification into Codex's private automation state.
6. Run diagnostics from `<control-root>/libexec/diagnose.py` before declaring setup complete.

The installer does not write Codex's private automation state directly. It generates the prompt and project configuration, while the app creates the scheduled task through its supported automation interface.

## Upgrade

Run the same installer with `--upgrade`. Preserve `config.json`, `runtime/`, proposals, findings, and backups. Refresh only managed scripts and control-project policy files. The upgrade backs up and removes obsolete hook files from earlier versions. Review any reported configuration migration before enabling new roots or host capabilities.

## Standalone installation from a clone

Run the repository-level `scripts/install.py`. It installs the standalone skill under `$HOME/.agents/skills/codex-improver` and creates the control project. Plugin installations already provide the skill and should run the skill-local installer instead.
