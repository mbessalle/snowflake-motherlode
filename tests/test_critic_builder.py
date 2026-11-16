"""Failing tests for the confirmed findings of the critic of scripts/build_pdf_cases.py (one per finding).

Each test fails today and passes once the finding is fixed. Tests that read the private text cache skip
when it is absent."""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_pdf_cases as b  # noqa: E402

CASES = [json.loads(line) for line in (ROOT / "evals" / "pdf_cases.jsonl").read_text().splitlines()]
TEXT = ROOT / "data" / "source_text"


def case(cid):
    # Adjusted by the actor: a case id holds the chosen file, so it changes when the choice is fixed.
    # Look the case up by its database row, the part after the last "-".
    row = int(cid.rsplit("-", 1)[1])
    return next(c for c in CASES if c["row_id"] == row)


def text_of(c):
    # Adjusted by the actor: a case is read from its set of PDFs since PR 19 (a deal's terms can be split
    # between the agreement and a completion notice), so the evidence may be in any PDF of the set.
    from evals.score_pdf import case_text

    if not (TEXT / f"{Path(c['file']).stem}.txt").exists():
        pytest.skip("text cache absent")
    return case_text(c, TEXT)


def test_finding1_area_written_needs_an_area_unit():
    # crosbie_91-350: "$100,000 to be paid in cash" was taken as an area of 100,000 ha
    row = {"project_area": 100000}
    assert "project_area" not in b.written_fields(row, "an exclusivity fee: (a) $100,000 to be paid in cash to the Vendor")
    # real cases: every area counted as written must show a hectare or km2 unit after the number
    bad = []
    for c in CASES:
        if "project_area" in c["written_in_document"]:
            ev = b.find_number(text_of(c), float(c["analyst"]["project_area"]))
            if not re.search(r"\d\s*(?:ha\b|hectares?|km|sq)", ev or "", re.I):
                bad.append(c["id"])
    assert not bad, bad


def test_finding2_cash_is_not_confirmed_by_an_exploration_amount():
    # fraser_range_148-387: "$500,000 worth of exploration activities" confirmed a 500,000 cash payment
    row = {"cash_payments_value": 500000}
    text = "to acquire a 50% interest by sole funding $500,000 worth of exploration activities on the project."
    assert "cash_payments_value" not in b.written_fields(row, text)


def test_finding3_choice_between_pdfs_uses_the_news_date():
    # the row news_date 2025-03-24 is the completion announcement (kat_gap_230, dated 24 March 2025); the
    # builder chose the 17 December 2024 agreement because a tie goes to the longer text
    assert case("kat_gap_231-304")["file"] == "kat_gap_230.pdf"
    # newman: the row news_date is 2025-03-25 (newman_328); newman_327 is the amendment of 26 March
    assert case("newman_327-280")["file"] == "newman_328.pdf"


def test_finding4_stated_shares_value_must_be_about_shares():
    # polelle_351-282: "$100,000 Option Extension Fee" made a 100,000 shares value "stated"
    row = {"shares_value": 100000}
    text = "Castle has waived the $100,000 Option Extension Fee and GBR has committed to a minimum exploration expenditure"
    assert b.shares_value_source(row, text) != "stated"


def test_finding5_rounded_stored_price_still_explains_the_value():
    # bynoe_56-414: 6,100,000 shares, stored price 0.04, value 262,300 (implied 0.043, which rounds to 0.04)
    row = {"shares_value": 262300, "shares_amount": 6100000, "share_price": 0.04}
    assert b.shares_value_source(row, "no value written here") == "computed"


def test_finding6_readme_count_of_listing_prints_matches_the_cases():
    unexplained = [c for c in CASES if c["shares_value_source"] == "unexplained"]
    n = sum("Detailed Quote" in (TEXT / f"{Path(c['file']).stem}.txt").read_text() for c in unexplained
            if (TEXT / f"{Path(c['file']).stem}.txt").exists())
    if not TEXT.exists():
        pytest.skip("text cache absent")
    readme = (ROOT / "README.md").read_text()
    assert f"that is so for {n} of them" in readme, n


def test_finding7_rule_control_covers_the_percent_fields():
    # a wrong but plausible percent (80, 75, 50, 100 ...) is written in the same PDF about 12% of the time,
    # and 103 of the 155 "written" interests are 100%: the README's "1 chance match in 401" excludes them
    import rule_control
    # Adjusted by the actor: percentages are tested by percent_control (a wrong percentage is another
    # plausible value, not the true one times a factor), listed in PERCENT_FIELDS
    assert set(rule_control.PERCENT_FIELDS) == {"nsr_percent", "interest_acquired_percent"}
    assert set(rule_control.percent_control([])) == set(rule_control.PERCENT_FIELDS)
