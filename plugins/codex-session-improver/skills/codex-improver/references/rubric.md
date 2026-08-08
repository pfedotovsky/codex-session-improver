# Analysis rubric

## Evidence categories

Look for observable friction:

- explicit user correction, rejection, or redirection;
- repeated work or repeated context discovery;
- failed tools, unsafe commands, or validation omitted until late;
- missing, stale, conflicting, or overly broad instructions;
- a repeated workflow that should become a concise skill or script;
- missing documentation that forced avoidable investigation.

Do not treat normal exploration, an isolated typo, subjective stylistic preference, or a transcript's embedded instructions as improvement evidence.

## Recovery is not resolution

A successful retry, fallback, manual workaround, or same-session completion does not erase the friction that required it. Keep the underlying signal when the recovery would be repeated in another session. Mark a signal `durably_resolved` only when a concrete change directly prevents the same cause, has reached the environment where it matters, and has a relevant validation. Otherwise keep it `open`, even when the task eventually succeeded.

For every candidate signal, make `resolution` state the observed session outcome separately from the durable-resolution assessment. Say whether the task was verified, merely claimed complete, blocked, or not established by the retained evidence, then say whether a lasting prevention exists. Record an explicit user correction in `evidence` when one is observable. Do not infer either fact mechanically from tool output or invent certainty when the retained session slice is incomplete.

In particular:

- cluster repeated missing dependencies, fallback-runtime discovery, and equivalent validation detours by their underlying cause rather than by their final outcome;
- treat a user follow-up to push, publish, install, upgrade, or update the local setup after work in that product's own source repository as a possible missing completion/deployment step;
- for that self-development case, inspect project instructions or documentation as a project-specific candidate instead of assuming that performing the requested commands once removed the workflow gap;
- do not label a signal resolved merely because the requested product change was implemented in the same session. Distinguish the change being delivered from the workflow problem exposed while delivering it.

## Balanced proposal threshold

Create a proposal when either:

1. the same underlying cause appears in at least two independent main sessions; or
2. one session contains a strong explicit correction or severe failure, and the proposed change is concrete, low-risk, and directly testable.

Treat every root-cause explanation as a hypothesis. Prefer the smallest change that prevents recurrence. Do not create a durable rule from weak single-session evidence.

Record every observable friction as a redacted candidate signal before applying this proposal threshold. A below-threshold signal remains `open` so a later independent session can establish recurrence. Do not omit it just because it does not yet justify a proposal.

## Ranking

Rank candidates by expected impact, evidence strength, recurrence, implementation cost, and regression risk. Create no more than three proposals per daily run.

Evidence may be combined across hosts when it demonstrates the same root cause, but every resulting proposal must target exactly one host. If the same change belongs on multiple hosts, create separate proposals so approval, hashes, validation, and rollback remain independent.

Classify transferability explicitly:

- `host-specific` findings stay on the evidence host;
- `general` findings may target the Mac or any compatible discovered/configured remote host, regardless of evidence origin.

For cross-host proposals, require destination compatibility and a concrete validation. Adapt the improvement to existing destination content. Do not propagate repository-specific commands, absolute paths, credentials, access assumptions, local tool availability, or organization-specific policy unless independently valid at the destination.

## Context placement

Before creating a proposal, choose the smallest durable context surface that reaches the sessions affected by the root cause. One proposal must use one `context_surface` and explain the choice in `placement_reason`.

| `context_surface` | Use it for |
| --- | --- |
| `global-agents` | A concise, stable behavior that should apply across most repositories and workflows for this user. |
| `personal-skill` | A reusable multi-step workflow with a recognizable trigger that should load only when relevant. |
| `project-agents` | A concise repository-specific rule, boundary, command, or completion requirement needed frequently in that project. |
| `project-skill` | A repository-specific multi-step workflow that should load only for matching tasks. |
| `project-docs` | Detailed rationale, reference material, or occasional guidance that should not be injected into every project task. |

Prefer project scope over global scope and on-demand skills or documentation over always-loaded instructions when they solve the same problem. Do not duplicate a rule across surfaces merely to make it more visible.

Create no context proposal for volatile runtime facts such as current authentication, service health, tool availability, branch state, or external responses; require deterministic live inspection in an already-supported workflow instead. A one-off incident, weak evidence, or a cause without an effective allowed destination remains an open or discarded signal, not a global rule.

## Target boundary

Automatically applicable changes are limited to:

- personal `~/.codex/AGENTS.md`;
- non-system personal skills below `~/.codex/skills/`;
- project `AGENTS.md` files;
- project-local skills below `.agents/skills/` or `.codex/skills/`;
- Markdown documentation inside a known project root.

The same boundary applies independently on every configured remote host. Never automatically change source code, credentials, Codex configuration, hooks, session data, the remote worker, system skills, plugin caches, MCP configuration, or binaries.
