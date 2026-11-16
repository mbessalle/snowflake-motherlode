"""How often do the case builder's derivation rules match a value by chance?

    python scripts/rule_control.py [FACTOR ...]        (default 1.13 0.87)

For each case, every analyst value of a scraped field is multiplied by a factor that makes it wrong, and the
same tests the builder uses (written in the PDF, a sum of two or three amounts of the same kind, an area in
km2) are run on the wrong value. Every match is a chance match. Uses the cached PDF text layers in
data/source_text/, so it needs no PDFs and no model. The README's chance column comes from this script.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_pdf_cases as builder  # noqa: E402

from evals.score_pdf import case_text  # noqa: E402
from motherlode.validate import FIELD_KIND, find_number  # noqa: E402

FIELDS = ("cash_payments_value", "exp_comit_value", "shares_amount", "project_area")
PERCENT_FIELDS = ("nsr_percent", "interest_acquired_percent")      # tested by percent_control


def control(cases: list[dict], factor: float) -> dict[str, dict[str, int]]:
    """Per field: values tested, wrong values found written, found as a sum, found as an area in km2."""
    out = {f: {"tested": 0, "written": 0, "sum": 0, "km2 to ha": 0} for f in FIELDS}
    for case in cases:
        text = case_text(case, builder.TEXT_CACHE)
        for name in FIELDS:
            value = builder.number(case["analyst"].get(name))
            if value is None:
                continue
            wrong = value * factor
            out[name]["tested"] += 1
            if find_number(text, wrong, kind=FIELD_KIND.get(name)):
                out[name]["written"] += 1
                continue
            found = builder.derive(name, wrong, text)
            if found:
                out[name][found[0]] += 1
    return out


# Common deal percentages. A wrong percentage is not the true one times a factor (113% is no interest) but
# another plausible value, so the control offers each of these that differs from the analyst's value.
PLAUSIBLE_PERCENT = (0.5, 1, 1.5, 2, 2.5, 3, 5, 10, 15, 20, 25, 30, 40, 49, 50, 51, 60, 65, 70, 75, 80, 85, 90, 100)


def percent_control(cases: list[dict]) -> dict[str, dict[str, int]]:
    """Per percentage field: wrong plausible values offered, and how many were found written near a word of
    the field's kind in the case PDF (the same test the builder uses for a written value)."""
    out = {}
    for name in builder.PERCENT:
        tried = found = 0
        for case in cases:
            value = builder.number(case["analyst"].get(name))
            if value is None:
                continue
            text = case_text(case, builder.TEXT_CACHE)
            for wrong in PLAUSIBLE_PERCENT:
                if wrong != value:
                    tried += 1
                    found += builder.evidence(name, wrong, text) is not None
        out[name] = {"tested": tried, "written": found}
    return out


def main() -> None:
    factors = [float(f) for f in sys.argv[1:]] or [1.13, 0.87]
    cases = [json.loads(line) for line in builder.CASES.read_text().splitlines() if line.strip()]
    for factor in factors:
        print(f"values x {factor}:")
        for name, c in control(cases, factor).items():
            print(f"  {name:<20} tested {c['tested']:>3}; chance matches: written {c['written']}, "
                  f"sum {c['sum']}, km2 to ha {c['km2 to ha']}")
    print("percentages: other plausible values offered instead of the analyst's")
    for name, c in percent_control(cases).items():
        print(f"  {name:<28} tested {c['tested']:>5}; found written {c['written']} "
              f"({100 * c['written'] / max(1, c['tested']):.1f}%)")


if __name__ == "__main__":
    main()
