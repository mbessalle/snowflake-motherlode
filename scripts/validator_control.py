"""How strict is the validator on real documents? Two numbers, before and after any change to motherlode/validate.py.

    python scripts/validator_control.py

  invented  round amounts that no deal in the set uses (175,000, 300,000 ...), offered as a cash payment for
            every case text: how many does validate() accept (FOUND or DERIVED)? Lower is better.
  real      every analyst value of a checked field, offered for its own case text: how many does validate()
            accept? Higher is better, but a value the text does not support should not count either.

Uses the cached text layers in data/source_text/ and evals/pdf_cases.jsonl; no model, no network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.score_pdf import case_text  # noqa: E402
from motherlode.validate import CHECKED_FIELDS, DERIVED, EMPTY, FOUND, validate  # noqa: E402

INVENTED = (175_000, 300_000, 750_000, 1_500_000, 2_500_000, 4_000_000, 325_000)


def main() -> None:
    cases = [json.loads(line) for line in (ROOT / "evals" / "pdf_cases.jsonl").read_text().splitlines() if line.strip()]
    texts = {c["id"]: case_text(c, ROOT / "data" / "source_text") for c in cases}
    print("invented cash payments accepted (FOUND / DERIVED of", len(cases), "texts):")
    for value in INVENTED:
        statuses = [validate({"cash_payments_value": value}, texts[c["id"]]).fields["cash_payments_value"].status
                    for c in cases]
        print(f"  {value:>10,}  {statuses.count(FOUND):>3} / {statuses.count(DERIVED):>3}")
    print("analyst values accepted, per field (FOUND + DERIVED of checked):")
    for name in CHECKED_FIELDS:
        statuses = [validate({name: c["analyst"][name]}, texts[c["id"]]).fields[name].status
                    for c in cases if c["analyst"].get(name) not in (None, 0)]
        checked = [s for s in statuses if s != EMPTY]
        if checked:
            print(f"  {name:<28} {checked.count(FOUND):>3} + {checked.count(DERIVED):>3} of {len(checked):>3}")


if __name__ == "__main__":
    main()
