# Security policy

## Reporting a vulnerability

Do not include real transcripts, credentials, hostnames, private paths, or runtime approval/application artifacts in a public issue. Use GitHub's private vulnerability reporting for this repository. If that is unavailable, open a minimal public issue asking the maintainer to enable a private reporting channel.

## Security-sensitive boundaries

Treat these changes as security-sensitive and require focused review:

- transcript parsing, redaction, or retained evidence;
- approval parsing, task/turn binding, receipts, or hook rewriting;
- target validation, writable roots, frozen hashes, backups, or rollback;
- SSH discovery, transport commands, remote worker configuration, or payload limits;
- supported target types or validation commands.

Never attach raw session fixtures copied from a real Codex home. Build synthetic fixtures with fake hosts, paths, users, and credentials.
