"""Write evals/sample_rows.jsonl: a handful of checked values, published as examples of the three tiers.

The per-deal results are a client's data and stay private (see the README, "What is published"); these few
rows are copied from them by code, so they cannot drift from the real results. With the private files present,
`--check` fails if the committed rows differ (tests/test_results_summary.py runs it)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
OUT = EVALS / "sample_rows.jsonl"

# Chosen to show each path through the tiers: tier 2 catches the error and proposes the fix; tier 2 flags
# a right value or proposes a wrong one and tier 3 settles it; tier 3 finds what tier 2 only called unsure.
EXAMPLES = [("elizabeth_hill_125-247", "exp_comit_value"), ("eagle_downs_119-359", "cash_payments_value"),
            ("lakewood_mill_249-355", "share_price"), ("katanning_233-266", "cash_payments_value"),
            ("fiery_creek_142-362", "shares_amount"), ("achilles_1-450", "shares_value"),
            ("sandover_additional_claims_378-408", "cash_payments_value")]


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (EVALS / f"{name}.jsonl").read_text().splitlines() if line.strip()]


def rows() -> list[dict]:
    cells = {(c["case_id"], c["field"]): c for c in load("cell_checks")}
    pilot = {(p["case_id"], p["field"]): p for p in load("cell_review_pilot")}
    projects = {c["id"]: c["project_name"] for c in load("pdf_cases")}
    out = []
    for key in EXAMPLES:
        c, p = cells[key], pilot[key]
        out.append({"project": projects.get(key[0]), "field": key[1], "analyst_value": c["value"],
                    "tier2_verdict": c["verdict"], "tier2_proposal": c.get("proposal"),
                    "tier3_value": p.get("true_value"), "analyst_right": p.get("analyst_right"),
                    "tier2_proposal_right": p.get("proposal_right"), "document": p.get("file")})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows())
    if args.check:
        if OUT.read_text() != text:
            sys.exit("evals/sample_rows.jsonl is out of date: run scripts/sample_rows.py")
        print("evals/sample_rows.jsonl matches the per-deal files")
        return
    OUT.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
