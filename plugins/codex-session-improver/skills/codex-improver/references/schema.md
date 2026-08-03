# Proposal interfaces

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
  "changes": [
    {
      "path": "/absolute/allowed/path.md",
      "new_content": "Complete desired UTF-8 file content"
    }
  ]
}
```

`target_host` defaults to `local` and `feedback_scope` defaults to `host-specific` for compatibility. Use one to eight target files, all on that host. `source_hosts` records evidence origin and may differ from `target_host` for general feedback. Remote paths are absolute paths on the remote host. Do not include secrets or verbatim transcript dumps. `proposal_tool.py` reads targets through the deterministic local or remote inspector and computes proposal IDs, transfer directions, expiry, hashes, operations, patches, and validation commands.

## Stored manifest

The proposal directory contains `manifest.json`, `change.patch`, and numbered desired-content files. The manifest's `target_host`, `source_hosts`, `feedback_scope`, and `transfer_directions` provide the cross-host audit trail. `target_host` is immutable and binds the patch, hashes, validation, backup, and application receipt to one machine. Status is one of `pending`, `approved`, `applied`, `rejected`, `stale`, or `failed`.

## Approval question

After proposal creation, register the exact set in the current task with:

```text
python3 <control-root>/libexec/approval_prompt.py --control-root <control-root> --proposal-id P-YYYYMMDD-NN [--proposal-id P-YYYYMMDD-NN ...]
```

The `PreToolUse` hook validates that every ID is pending and unexpired, then stores one active question block bound to the task. The agent asks one numbered yes/no question per proposal. On the next turn, `UserPromptSubmit` accepts only one bounded decision per question, resolves proposal IDs from that task-bound block, creates a short-lived one-time approval receipt for the approved subset, and marks the rejected subset as rejected. Any incomplete, reordered, or qualified response cancels the question block without changing proposal status. A bare yes/no without an active single-proposal question has no effect. Proposal IDs in user-authored commands, assistant messages, tool output, historical turns, or another task never authorize application. Receipts are consumed once and expire after ten minutes.
