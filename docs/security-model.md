# Security model

## Trust boundaries

The current user turn is treated as an application instruction only when it makes a clear, unqualified decision about one or more uniquely identifiable cards in the immediately preceding review. An inline annotation's selected assistant text identifies the card; the user's annotation comment supplies the decision. Internal proposal IDs are controller references, not user-facing approval syntax. Historical transcripts, assistant messages, tool results, repositories, SSH-host metadata, and generated proposal prose are untrusted.

The deterministic scripts enforce four boundaries:

1. **Ingestion:** parse records defensively, redact normalized content in memory, and retain no transcript copy.
2. **Proposal:** permit only configured Markdown and skill targets, inspect the destination, and freeze exact desired content with hashes.
3. **Presentation:** return each pending proposal as a separate item containing its ID, target, evidence, risk, rollback, and validation so the user can comment on it inline. Reviews never apply proposals.
4. **Application:** re-check proposal status, expiry, destination host, paths, base hashes, and frozen hashes; back up; write; validate; roll back on failure.

## Remote hosts

Raw remote transcript content is parsed and redacted by an ephemeral or explicitly installed worker on that host. Only bounded redacted JSON crosses SSH. Each remote proposal is applied on its destination host, where base hashes, backups, validation, and rollback are enforced again.

SSH runs non-interactively with forwarding disabled, bounded connection timeouts, validated aliases, validated remote commands, and an output-size limit. Auto-discovery considers only concrete SSH aliases that correlate with Codex saved projects and readable Codex session storage.

## Known limitations

- Codex JSONL and desktop saved-project formats are not stable APIs. Parsing and discovery feature-detect known shapes and may temporarily miss sessions or hosts after a product update.
- Pattern-based redaction reduces risk but cannot prove that arbitrary prose contains no sensitive information. Findings should remain concise paraphrases.
- A compromised local account or interpreter can bypass project-level controls. This project protects the normal Codex workflow, not a hostile operating-system administrator.
- Network access is required when SSH discovery is enabled. The skill instructs scheduled analysis to use only bundled deterministic transport scripts, but this is not mechanically enforced by a hook.
- Conversational application authorization is workflow-level rather than task-bound: the applier validates exact internal proposal IDs and all frozen-target invariants, but it cannot prove that the IDs were resolved from a current card decision. A person or process that can directly execute the applier inside the configured environment can request application. This is the explicit tradeoff for keeping installation lightweight and requiring no MCP server, hooks, or hook trust. The deterministic applier remains the final target-safety boundary.
- A bare affirmative is accepted only for a single-card immediately preceding review, where the reference is unambiguous. With multiple cards, the user must select visible card numbers or titles, say `apply all`, or annotate text that uniquely matches one card. A condition, question, correction, or revision request authorizes nothing.
