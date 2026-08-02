# Remote hosts and feedback transfer

The Mac is the only review and approval point. Remote transcript discovery, parsing, and redaction happen on the source host. Remote application verifies frozen hashes, creates a host-local backup, validates the exact approved content, and rolls back failures. Raw transcript or normalized unredacted content must never cross SSH.

## Discovery

Mirror Codex SSH discovery:

- read concrete aliases from `~/.ssh/config`, including `Include` files;
- ignore wildcard, negated, and pattern-only aliases;
- resolve aliases with `ssh -G`;
- correlate them with remote projects saved by the Codex app;
- perform a bounded non-interactive read-only probe for `~/.codex/sessions` and saved project paths;
- never use `known_hosts`, transcript text, prompt history, or an app display name as a transport address.

Explicit `remote_hosts` entries use their installed worker. Newly discovered hosts use the same bundled worker ephemerally over SSH, so analysis does not install files or persist code remotely. Discovery retains only connection aliases, opaque app host IDs, project paths, worker configuration, availability, and timestamps.

Adding a new physical machine normally requires only: add a concrete SSH alias, verify key-based non-interactive SSH, save at least one remote project in Codex, and use it long enough to create `~/.codex/sessions`. The next review discovers it automatically.

## Feedback transfer

Classify each finding before choosing targets:

- `host-specific`: depends on that host's repository, paths, tools, policies, or environment; do not propagate.
- `general`: a durable behavior, reusable workflow, user preference, safety rule, or skill improvement that is applicable on another host.

General evidence can flow in every direction: remote → local, local → remote, or remote → remote. Evidence origin never forces the target host. Inspect the destination file and adapt paths or wording to that host. Never copy whole `AGENTS.md` or skill directories blindly.

A remote Codex session reads the remote host's `~/.codex/AGENTS.md`, personal skills, and repository-local guidance. Changing the Mac copy alone does not change remote behavior. Every destination gets a separate frozen proposal with its own target host, base hashes, diff, validation, rollback, and proposal ID. Approval of one destination never implies approval of another.
