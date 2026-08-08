# Setup and upgrade

## Initial setup

1. Resolve `<skill-root>` as the directory containing `SKILL.md`.
2. Use an explicitly supplied control-project path, otherwise default to `$HOME/projects/codex-improver`. Do not ask the user to choose a path for the default installation.
3. Run the bundled installer with absolute paths. Use `$HOME/projects` as the default project root. If the current repository is outside that tree, add its repository root separately. Never use `/` or the complete home directory. Add every selected root with a separate `--project-root` argument:

   ```text
   python3 <skill-root>/scripts/install.py --control-root <control-root> --project-root <projects-root>
   ```

4. Run diagnostics from `<control-root>/libexec/diagnose.py` before declaring setup complete. Report only a compact success or actionable error; do not make the user inspect generated files.
5. When setup was triggered implicitly by the first review, continue directly into that review. The plugin is usable without MCP configuration, hooks, a restart, or a scheduled task.
6. Create a standalone local scheduled task only when the user explicitly asks for scheduled reviews. Use the generated one-line prompt at `<control-root>/automation-prompt.md` and the portable source specification at `<control-root>/scheduled-task.spec.toml`, select the control project, and use the user's requested cadence. If none is supplied, recommend daily at 12:45 in the user's local timezone with a high-reasoning model. Keep the workflow in this skill rather than expanding it into the task prompt. Treat the Codex app as the runtime source of truth; do not copy the portable specification into Codex's private automation state.

Codex currently installs a skill-only plugin without running project code. The automatic initialization above therefore happens on first use rather than at plugin-install time. The installer does not write Codex's private automation state directly. It generates the prompt and project configuration, while the app creates a scheduled task through its supported automation interface only when requested.

When the user also requests a recurring audit of persistent global Codex context, create a separate standalone task rather than expanding the session-review task. Use `<control-root>/global-context-automation-prompt.md` and `<control-root>/global-context-scheduled-task.spec.toml`; the generated default is daily at 13:15 local time. The audit is read-only, excludes session history, and never applies its suggestions.

## Upgrade

Run the same installer with `--upgrade`. Preserve `config.json`, `runtime/`, proposals, findings, and backups. Refresh only managed scripts and control-project policy files. The upgrade backs up and removes obsolete hook files from earlier versions. Review any reported configuration migration before enabling new roots or host capabilities.

## Standalone installation from a clone

Run the repository-level `scripts/install.py`. It installs the standalone skill under `$HOME/.agents/skills/codex-improver` and creates the control project. Plugin installations already provide the skill and should run the skill-local installer instead.
