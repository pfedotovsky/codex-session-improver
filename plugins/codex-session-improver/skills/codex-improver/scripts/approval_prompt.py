#!/usr/bin/env python3
"""Render the deterministic question for one task-bound proposal set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from improver_lib import ensure_runtime, load_manifest, now, parse_iso, print_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--proposal-id", action="append", required=True)
    args = parser.parse_args()

    control_root = args.control_root.resolve()
    ensure_runtime(control_root)
    proposal_ids = list(dict.fromkeys(args.proposal_id))
    if not 1 <= len(proposal_ids) <= 3:
        raise RuntimeError("An approval question requires one to three proposals")

    summaries = []
    for proposal_id in proposal_ids:
        _, manifest = load_manifest(control_root, proposal_id)
        if manifest.get("status") != "pending" or parse_iso(manifest["expiry_at"]) <= now():
            raise RuntimeError(f"Proposal is not pending and unexpired: {proposal_id}")
        summaries.append({"id": proposal_id, "summary": str(manifest.get("summary", ""))[:500]})

    if len(proposal_ids) == 1:
        question = (
            "Reply yes to apply it or no to reject it. "
            f"Would you like to apply the frozen proposal {proposal_ids[0]}?"
        )
    else:
        example = ", ".join(
            f"{index} {'yes' if index < len(proposal_ids) else 'no'}"
            for index in range(1, len(proposal_ids) + 1)
        )
        questions = [
            f"{index}. Would you like to apply the frozen proposal {proposal_id}?"
            for index, proposal_id in enumerate(proposal_ids, 1)
        ]
        question = (
            f"Answer each question independently in one reply (for example: {example}).\n"
            + "\n".join(questions)
        )
    print_json(
        {
            "proposal_ids": proposal_ids,
            "summaries": summaries,
            "question": question,
        }
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
