# Security model

## Trust boundaries

The user-authored current approval turn is trusted only for exact proposal IDs. Historical transcripts, assistant messages, tool results, repositories, SSH-host metadata, and generated proposal prose are untrusted.

The deterministic scripts enforce four boundaries:

1. **Ingestion:** parse records defensively, redact normalized content in memory, and retain no transcript copy.
2. **Proposal:** permit only configured Markdown and skill targets, inspect the destination, and freeze exact desired content with hashes.
3. **Approval:** accept only a complete `APPROVE P-...` current-turn prompt, bind it to the current task and turn, and issue a short-lived one-time receipt.
4. **Application:** re-check proposal status, expiry, destination host, paths, base hashes, and frozen hashes; back up; write; validate; roll back on failure.

## Remote hosts

Raw remote transcript content is parsed and redacted by an ephemeral or explicitly installed worker on that host. Only bounded redacted JSON crosses SSH. Each remote proposal is applied on its destination host, where base hashes, backups, validation, and rollback are enforced again.

SSH runs non-interactively with forwarding disabled, bounded connection timeouts, validated aliases, validated remote commands, and an output-size limit. Auto-discovery considers only concrete SSH aliases that correlate with Codex saved projects and readable Codex session storage.

## Known limitations

- Codex JSONL and desktop saved-project formats are not stable APIs. Parsing and discovery feature-detect known shapes and may temporarily miss sessions or hosts after a product update.
- Pattern-based redaction reduces risk but cannot prove that arbitrary prose contains no sensitive information. Findings should remain concise paraphrases.
- A compromised local account or interpreter can bypass project-level controls. This project protects the normal Codex workflow, not a hostile operating-system administrator.
- Network access is required when SSH discovery is enabled. The project hook denies direct network commands from scheduled analysis and permits remote access only through bundled scripts.
