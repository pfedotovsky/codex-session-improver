# Proposal interfaces

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

## Approval command

The only accepted form is:

```text
APPROVE P-YYYYMMDD-NN [P-YYYYMMDD-NN ...]
```

The entire user prompt must match. IDs may refer only to pending, unexpired proposals. When the deterministic approval request reaches `PreToolUse`, the hook reads only the current turn's user-authored transcript message, verifies the task and turn IDs, creates the receipt, and rewrites the request to the bound applier command. Approval-like text in assistant messages, tool output, historical turns, or another task is ignored. Receipts are consumed once and expire after ten minutes.
