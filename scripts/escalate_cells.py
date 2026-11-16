"""Tier 3 of the cell check: hand the escalated cells of a few cases to a `claude -p` session in its own worktree.

    python scripts/escalate_cells.py prepare WORKTREE [--cases N] [--seed S]   # the worktree's review/ folder
    python scripts/escalate_cells.py collect WORKTREE                           # tiers 2 and 3 compared

prepare picks N cases among those with escalated cells, leaving out cases whose PDF does not show the
analyst's buyer as the buyer (a wrong document is one finding per case, not one per cell), and copies in only
what the reviewer needs: the escalated cells, the full texts of every PDF of those projects, and each case's
analyst row. The worktree itself must already exist (git worktree add) at a commit that holds
.claude/agents/cell-reviewer.md. Then run, in the worktree:

    claude -p --agent cell-reviewer --allowedTools "Bash Read Grep Glob Write" "Review the cells in review/."

collect reads review/REVIEW.jsonl and prints how often the analyst and the tier 2 proposal were right.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_pdf_cases as builder  # noqa: E402

CELLS = ROOT / "evals" / "cell_checks.jsonl"
JUDGE = ROOT / "evals" / "judge_results.jsonl"


def prepare(worktree: Path, n: int, seed: int) -> None:
    cells = [json.loads(line) for line in CELLS.read_text().splitlines()]
    buyer = {r["row_id"]: (r["answer"].get("probabilities") or {}).get("buyer", 0)
             for r in map(json.loads, JUDGE.read_text().splitlines()) if r["question"] == "buyer_role"}
    escalated = [c for c in cells if c["escalate"] and buyer.get(c["row_id"], 1) >= 0.5]
    rows = sorted({c["row_id"] for c in escalated})
    random.Random(seed).shuffle(rows)
    chosen = set(rows[:n])
    cases = {c["row_id"]: c for c in map(json.loads, builder.CASES.read_text().splitlines())}
    inventory = [json.loads(line) for line in builder.INVENTORY.read_text().splitlines()]
    review = worktree / "review"
    (review / "texts").mkdir(parents=True, exist_ok=True)
    info = {}
    for row in chosen:
        case = cases[row]
        files = [e["file"] for e in inventory if e["project"] == case["project"] and "duplicate_of" not in e]
        for f in files:
            shutil.copy(builder.TEXT_CACHE / f"{Path(f).stem}.txt", review / "texts" / f"{Path(f).stem}.txt")
        info[case["id"]] = {"project_name": case["project_name"], "pdfs": files, "case_pdf": case["file"],
                            "analyst_row": case["analyst"]}
    mine = [c for c in escalated if c["row_id"] in chosen]
    (review / "cells.jsonl").write_text("".join(json.dumps(c) + "\n" for c in mine))
    (review / "cases.json").write_text(json.dumps(info, indent=1))
    print(f"{len(mine)} cells in {len(chosen)} cases, {len(list((review / 'texts').iterdir()))} texts -> {review}")


def collect(worktree: Path) -> None:
    rows = [json.loads(line) for line in (worktree / "review" / "REVIEW.jsonl").read_text().splitlines() if line.strip()]
    tier2 = {(c["case_id"], c["field"]): c for c in map(json.loads, (worktree / "review" / "cells.jsonl").read_text().splitlines())}
    analyst = {k: sum(1 for r in rows if r.get("analyst_right") is k) for k in (True, False, None)}
    proposals = [r for r in rows if tier2.get((r["case_id"], r["field"]), {}).get("proposal")]
    right = sum(1 for r in proposals if r.get("proposal_right") is True)
    verdicts = {}
    for r in rows:
        v = tier2.get((r["case_id"], r["field"]), {}).get("verdict")
        verdicts.setdefault(v, {True: 0, False: 0, None: 0})[r.get("analyst_right")] += 1
    print(f"{len(rows)} cells reviewed: analyst right {analyst[True]}, wrong {analyst[False]}, undecidable {analyst[None]}")
    print(f"tier 2 proposals right: {right} of {len(proposals)}")
    for v, c in verdicts.items():
        print(f"  tier 2 said {v}: analyst right {c[True]}, wrong {c[False]}, undecidable {c[None]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("prepare", "collect"))
    parser.add_argument("worktree", type=Path)
    parser.add_argument("--cases", type=int, default=10)
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args()
    prepare(args.worktree, args.cases, args.seed) if args.command == "prepare" else collect(args.worktree)


if __name__ == "__main__":
    main()
