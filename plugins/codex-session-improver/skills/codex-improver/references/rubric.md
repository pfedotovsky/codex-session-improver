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

## Balanced proposal threshold

Create a proposal when either:

1. the same underlying cause appears in at least two independent main sessions; or
2. one session contains a strong explicit correction or severe failure, and the proposed change is concrete, low-risk, and directly testable.

Treat every root-cause explanation as a hypothesis. Prefer the smallest change that prevents recurrence. Do not create a durable rule from weak single-session evidence.

## Ranking

Rank candidates by expected impact, evidence strength, recurrence, implementation cost, and regression risk. Create no more than three proposals per daily run.

Evidence may be combined across hosts when it demonstrates the same root cause, but every resulting proposal must target exactly one host. If the same change belongs on multiple hosts, create separate proposals so approval, hashes, validation, and rollback remain independent.

Classify transferability explicitly:

- `host-specific` findings stay on the evidence host;
- `general` findings may target the Mac or any compatible discovered/configured remote host, regardless of evidence origin.

For cross-host proposals, require destination compatibility and a concrete validation. Adapt the improvement to existing destination content. Do not propagate repository-specific commands, absolute paths, credentials, access assumptions, local tool availability, or organization-specific policy unless independently valid at the destination.

## Target boundary

Automatically applicable changes are limited to:

- personal `~/.codex/AGENTS.md`;
- non-system personal skills below `~/.codex/skills/`;
- project `AGENTS.md` files;
- project-local skills below `.agents/skills/` or `.codex/skills/`;
- Markdown documentation inside a known project root.

The same boundary applies independently on every configured remote host. Never automatically change source code, credentials, Codex configuration, hooks, session data, the remote worker, system skills, plugin caches, MCP configuration, or binaries.
