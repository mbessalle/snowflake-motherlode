"""Did a blind agent stay blind? List every tool call in its transcript that leaves its allowed folder or names a
forbidden file.

    python scripts/audit_agent_reads.py ALLOWED_DIR TRANSCRIPT.jsonl [...] [--forbid NAME ...]

A blind labeller works in a worktree that holds only its questions and texts. Isolation by folder is not
enforced by the agent runtime, so this reads what the agent actually did: every tool call's input (Bash
commands, file paths, search patterns) is checked for an absolute path outside ALLOWED_DIR and for the
forbidden names (by default the decision model's answers, cache, ledger and traces). Exit code 1 when any
call is suspect, so a calibration run can refuse labels from an agent that looked.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FORBIDDEN = ("judge_results", "judge_cache", "judge_ledger", "traces")
# an absolute path; not the tail of "$S/file", "${S}/file" or S+"/file" (a variable, resolved elsewhere in the
# command, where its own value is checked as a path)
PATH = re.compile(r"(?<![\w.$}-])(?<!\+\")(?<!\+')(?<!\+ \")(?<!\+ ')(/[\w./-]+)")


def strings(value) -> list[str]:
    """Every string inside a tool call's input, unescaped."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def tool_calls(transcript: Path) -> list[dict]:
    calls = []
    for line in transcript.read_text().splitlines():
        try:
            message = json.loads(line).get("message") or {}
        except json.JSONDecodeError:
            continue
        for part in message.get("content") or [] if isinstance(message.get("content"), list) else []:
            if part.get("type") == "tool_use":
                calls.append({"tool": part.get("name"), "input": part.get("input")})
    return calls


def suspect(call: dict, allowed: Path, forbidden: tuple[str, ...]) -> list[str]:
    """Why a call is suspect: each absolute path outside `allowed`, and each forbidden name it mentions."""
    text = "\n".join(strings(call["input"]))
    reasons = [f"names {name}" for name in forbidden if name in text]
    for path in PATH.findall(text):
        if path.startswith(("/dev/null", "/usr/", "/bin/", "/tmp/claude-")) or path == "/":
            continue
        resolved = Path(path)
        if not (resolved == allowed or allowed in resolved.parents):
            reasons.append(f"path outside: {path}")
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("allowed", type=Path)
    parser.add_argument("transcripts", nargs="+", type=Path)
    parser.add_argument("--forbid", action="append", default=list(FORBIDDEN))
    args = parser.parse_args()
    allowed, bad = args.allowed.resolve(), 0
    for transcript in args.transcripts:
        calls = tool_calls(transcript)
        flagged = [(c, suspect(c, allowed, tuple(args.forbid))) for c in calls]
        flagged = [(c, r) for c, r in flagged if r]
        print(f"{transcript.name}: {len(calls)} tool calls, {len(flagged)} suspect")
        for call, reasons in flagged:
            print(f"  {call['tool']}: {'; '.join(reasons)} | {json.dumps(call['input'])[:200]}")
        bad += len(flagged)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
