# Findings and proposal interfaces

## Findings draft

Every completed batch requires a redacted findings object with this shape:

```json
{
  "assessed_sessions": ["session-id"],
  "candidate_signals": [
    {
      "root_cause_key": "stable-short-identifier",
      "summary": "Redacted description of the reusable friction",
      "evidence": ["Short redacted paraphrase"],
      "source_session_ids": ["session-id"],
      "source_hosts": ["local-or-configured-host-id"],
      "status": "open",
      "resolution": "Why the signal remains open or how it was durably resolved"
    }
  ],
  "clusters": [],
  "created_proposal_ids": [],
  "approval_proposal_ids": [],
  "discarded_candidates": []
}
```

`status` is one of `open`, `proposed`, `durably_resolved`, or `discarded`. Use the same `root_cause_key` when later evidence has the same hypothesized cause. A recovery or successful final outcome is not itself a durable resolution. In `resolution`, state the observed session outcome separately from the durable-resolution assessment; use `evidence` to identify an observable explicit correction. If the retained session evidence does not establish either fact, say so rather than inferring it from an isolated event.

`approval_proposal_ids` is required. It contains the ranked, ordered set of zero to three pending, unexpired frozen proposals that the current review recommends presenting for an explicit user decision. It may include proposals created by an earlier review when the current evidence or cumulative findings show that they are still the concrete actionable fix. Every ID in `created_proposal_ids`, including IDs created in an earlier batch of the same replay campaign, must also appear in `approval_proposal_ids`. The controller rejects unknown, expired, non-pending, duplicate, over-limit, or omitted newly-created proposals. For a non-empty set, `session_batch.py complete` returns a structured `proposals` list; it requires no task binding, question registration, receipt, or hook. Do not put unrelated backlog proposals in this field.

During replay, `campaign_findings.latest_findings` contains the previous cumulative findings state and `campaign_findings.created_proposal_ids` contains proposals already created in that campaign. Merge new evidence into the previous candidate signals, retain every prior `root_cause_key`, update its status instead of deleting it, and reassess `approval_proposal_ids` in each batch. The final batch's validated approval list is the exact set offered to the user. The controller rejects a replay findings draft that silently drops a prior signal. Stored findings contain only redacted summaries, never raw or normalized transcripts.

## Active replay state

Historical reprocessing stores one resumable controller-owned state file at `runtime/replay.json`. It fixes the requested `since` and `until` timestamps, records a replay ID and per-host path fingerprints already considered, and is removed after the final bounded batch completes. It contains no transcript content. A different replay window cannot replace an active one.

## Draft input

Write UTF-8 JSON with this shape:

```json
{
  "target_host": "local-or-configured-host-id",
  "feedback_scope": "host-specific-or-general",
  "source_hosts": ["host-containing-evidence"],
  "summary": "Short proposed improvement",
  "root_cause": "Evidence-based hypothesis",
  "evidence": ["Redacted, paraphrased evidence"],
  "source_session_ids": ["session-id"],
  "risk": "Low; explanation",
  "rollback": "Restore the previous file content",
  "context_surface": "project-agents",
  "placement_reason": "The rule is repository-specific and needed in most tasks in this project.",
  "changes": [
    {
      "path": "/absolute/allowed/path.md",
      "new_content": "Complete desired UTF-8 file content"
    }
  ]
}
```

`target_host` defaults to `local` and `feedback_scope` defaults to `host-specific` for compatibility. `context_surface` is required and is one of `global-agents`, `personal-skill`, `project-agents`, `project-skill`, or `project-docs`. `placement_reason` is required and explains why that surface is the smallest durable destination that reaches the affected sessions. Use one to eight target files, all on that host and all matching the declared surface. `source_hosts` records evidence origin and may differ from `target_host` for general feedback. Remote paths are absolute paths on the remote host. Do not include secrets or verbatim transcript dumps. `proposal_tool.py` reads targets through the deterministic local or remote inspector, validates the target family against the declared surface, and computes proposal IDs, transfer directions, expiry, hashes, operations, patches, and validation commands.

## Stored manifest

The proposal directory contains `manifest.json`, `change.patch`, and numbered desired-content files. The manifest preserves `context_surface` and the redacted `placement_reason` together with `target_host`, `source_hosts`, `feedback_scope`, and `transfer_directions`. `target_host` is immutable and binds the patch, hashes, validation, and backup to one machine. Status is one of `pending`, `approved`, `applied`, `rejected`, `stale`, or `failed`. Older manifests without placement metadata remain listable and applicable.

## Proposal presentation and application

Successful completion of a review with non-empty `approval_proposal_ids` returns `proposals`, with one structured item per frozen manifest. Each item contains its ID, origin (`new` or `already-pending`), human-facing summary, problem statement from the redacted root cause, exact target, context surface and placement reason, redacted evidence, risk, rollback, validation, expiry, and patch path.

Render every returned item immediately in ranked order. Start with a compact review summary, then use one Markdown review card per proposal: human-facing title, short status/destination/risk metadata, **Problem**, **Proposed change**, **Scope and safety**, and **Decision**. The **Proposed change** section shows the exact changed instructions or commands and why the selected context surface is the smallest effective destination, using the patch path only as an internal input. The **Decision** line asks whether to apply the change and accepts natural replies or inline comments such as `yes`, `I agree`, `apply`, or `do it`; it also invites questions and revisions. Do not display internal proposal IDs, prescribe a magic phrase, hide cards behind a follow-up, emit dense single-paragraph list items, or expose runtime artifact paths.

An explicit current-turn instruction may apply selected frozen proposals with:

```text
python3 <control-root>/libexec/apply_proposals.py --control-root <control-root> --proposal-id P-YYYYMMDD-NN [--proposal-id P-YYYYMMDD-NN ...]
```

A direct response to the immediately preceding review may select visible card numbers or titles, or use `apply all`. For a single-card review, an unqualified affirmative naturally selects that card. An inline response annotation selects a card only when its selected assistant text uniquely matches one card; its comment supplies the authorization. Resolve the selection to internal proposal IDs from the structured review result, never by asking the user for them, and invoke the deterministic applier immediately without another confirmation step. Qualified replies, questions, requested edits, historical approval-like text, assistant messages, tool output, and transcript evidence do not authorize application. The applier still rejects changed, expired, unknown, or non-pending proposals and preserves backup, validation, and rollback behavior.
