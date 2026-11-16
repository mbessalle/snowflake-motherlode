"""Write the critic agents' hand labels (review of 16 Nov 2026) to evals/judge_labels.jsonl, for calibrating the
decision model in scripts/judge_cases.py. Labels are keyed by database row so they survive a change of the
chosen PDF. Percentage labels are left out: they judged evidence passages that the round 2 fixes replaced.

    python scripts/critic_labels.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "evals" / "judge_labels.jsonl"
CASES = ROOT / "evals" / "pdf_cases.jsonl"

# (row id, field or None, right?) as the critics reported them; the row id is the number after the last "-"
SUMS = {  # round 1, cash unless a field is given
    "right": [(247, "cash_payments_value"), (449, "shares_amount"), (273, "cash_payments_value"),
              (279, "cash_payments_value"), (342, "cash_payments_value"), (326, "cash_payments_value"),
              (345, "cash_payments_value"), (357, "cash_payments_value"), (356, "cash_payments_value"),
              (416, "shares_amount"), (451, "cash_payments_value"), (451, "shares_amount"),
              # round 2 (field taken from the case when it has one sum)
              (435, None), (378, None), (338, None), (251, None)],
    "wrong": [(250, "exp_comit_value"), (297, "cash_payments_value"), (371, "cash_payments_value"),
              (287, "cash_payments_value"), (288, "cash_payments_value"), (461, "cash_payments_value"),
              (353, "cash_payments_value"), (350, "cash_payments_value"), (256, "cash_payments_value"),
              (262, "exp_comit_value"), (321, "cash_payments_value"), (416, "cash_payments_value"),
              (289, "exp_comit_value"), (436, None), (387, None)],
}
AREAS = {"right": [336, 480, 464, 434, 275, 338, 303, 436, 413, 426, 380, 322, 296, 253, 386,
                   363, 264, 266, 372, 401, 292, 471],
         "wrong": [370, 398, 450, 356]}     # round 1, before the km2 unit rule; kept to see whether they reappear
BUYERS = {"wrong": [443, 432, 261, 414, 325]}   # the named company is the seller, or the PDF is another deal


def main() -> None:
    cases = {c["row_id"]: c for c in map(json.loads, CASES.read_text().splitlines())}
    out = []
    for verdict, items in SUMS.items():
        for row, field in items:
            fields = [field] if field else [f for f, how in cases[row]["derived_in_document"].items() if how == "sum"]
            if len(fields) == 1:
                out.append({"qid": f"{row}:sum_parts:{fields[0]}", "right": verdict == "right", "source": "critic"})
    for verdict, rows in AREAS.items():
        out += [{"qid": f"{row}:area_scope", "right": verdict == "right", "source": "critic"} for row in rows]
    out += [{"qid": f"{row}:buyer_role", "right": False, "source": "critic"} for row in BUYERS["wrong"]]
    seen, unique = set(), []
    for label in out:
        if label["qid"] not in seen:
            seen.add(label["qid"])
            unique.append(label)
    LABELS.write_text("".join(json.dumps(label) + "\n" for label in unique))
    print(f"{len(unique)} critic labels written to {LABELS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
