"""The 236-case scorer: every field has a rule, each comparison accepts what it should and nothing more,
and scoring the analyst's own values gives full agreement while wrong values give none."""
import json
from pathlib import Path

import pytest

from evals import score_pdf
from motherlode.fields import extracted_fields

ROOT = Path(__file__).resolve().parent.parent


def test_every_extracted_field_has_exactly_one_scoring_rule():
    assert set(score_pdf.RULES) == {f.name for f in extracted_fields()}
    kinds = {kind for kind, _, _ in score_pdf.RULES.values()}
    assert kinds <= {score_pdf.SCRAPED, score_pdf.CALCULATED, score_pdf.MARKET, score_pdf.LOCATION, score_pdf.TEXT,
                     score_pdf.INPUT, score_pdf.NO_TRUTH}
    for name, (kind, compare, note) in score_pdf.RULES.items():
        assert compare or kind == score_pdf.NO_TRUTH or name in ("project_area", "project_area_unit"), name
        assert note or kind == score_pdf.TEXT, name          # every rule that is not obvious says how it compares


@pytest.mark.parametrize("compare, a, b, expected", [
    (score_pdf.same_company, "Errawarra Resources Ltd", "ERRAWARRA RESOURCES LIMITED", True),
    (score_pdf.same_company, "Western Yilgarn NL", "Western Yilgarn", True),
    (score_pdf.same_company, "Errawarra Resources Ltd", "Taruga Minerals Limited", False),
    (score_pdf.same_ticker, "ASX:MM8", "ASX.MM8", True),
    (score_pdf.same_ticker, "ASX:MM8", "MM8", True),
    (score_pdf.same_ticker, "ASX:MM8", "TSXV:MM8", False),
    (score_pdf.same_ticker, "ASX:MM8", "ASX:MM9", False),
    (score_pdf.same_state, "WA", "Western Australia", True),
    (score_pdf.same_state, "nsw", "New South Wales", True),
    (score_pdf.same_state, "WA", "South Australia", False),
    (score_pdf.same_set, "Ag, Au,Pb", "Pb,Au,Ag", True),
    (score_pdf.same_set, "Ag,Au", "Ag", False),
    (score_pdf.same_date, "2025-03-23T00:00:00", "2025-03-23", True),
    (score_pdf.same_date, "2025-03-24", "2025-03-23", False),
    (score_pdf.same_place, -21.10, -21.14, True),
    (score_pdf.same_place, -21.10, -21.20, False),
    (score_pdf.same_number, 1_000_000, 1_009_000, True),
    (score_pdf.same_number, 1_000_000, 1_130_000, False),
])
def test_comparisons(compare, a, b, expected):
    assert compare(a, b) is expected


def test_an_area_is_compared_in_hectares():
    assert score_pdf.hectares(45.5, "km2") == pytest.approx(4550)
    assert score_pdf.hectares(45.5, "km²") == pytest.approx(4550)
    assert score_pdf.hectares(18000, "Ha") == 18000
    assert score_pdf.hectares(10, None) == 10
    assert score_pdf.hectares(10, "furlongs") is None
    analyst = {"project_area": 4550, "project_area_unit": "Ha"}
    assert score_pdf.agrees("project_area", {"project_area": 45.5, "project_area_unit": "km2"}, analyst)
    assert not score_pdf.agrees("project_area", {"project_area": 45.5, "project_area_unit": "ha"}, analyst)


CASE = {"id": "acme_1-7", "file": "acme_1.pdf", "buyer_in_document": True, "written_in_document": ["nsr_percent"],
        "derived_in_document": {"cash_payments_value": "sum"},
        "analyst": {"nsr_percent": 2, "cash_payments_value": 1250000, "owner_name": "Acme Gold Ltd", "deal_type": "Project Acquisition",
                    "exp_comit_value": 500000}}
TEXT = "Acme Gold Limited pays $250,000 now and $1,000,000 later. The vendor keeps a 2% NSR."


def run(tmp_path, reply, case=CASE):
    (tmp_path / "texts").mkdir(exist_ok=True)
    (tmp_path / "replies").mkdir(exist_ok=True)
    (tmp_path / "texts" / "acme_1.txt").write_text(TEXT)
    (tmp_path / "replies" / f"{case['id']}.json").write_text(json.dumps(reply))
    return score_pdf.score([case], tmp_path / "replies", tmp_path / "texts")["fields"]


def test_the_analysts_own_values_agree_and_wrong_ones_do_not(tmp_path):
    right = run(tmp_path, {"database_updates": CASE["analyst"]})
    assert right["nsr_percent"]["agree"] == 1 and right["nsr_percent"]["in_document_agree"] == 1
    assert right["cash_payments_value"]["in_document"] == 1 and right["cash_payments_value"]["grounded"] == 1
    assert right["owner_name"]["agree"] == 1
    assert right["exp_comit_value"]["in_document"] == 0               # the analyst's value is not in the PDF
    assert right["deal_type"]["both"] == 0 and right["deal_type"]["model_only"] == 1     # not compared

    wrong = run(tmp_path, {"nsr_percent": 3, "cash_payments_value": 1_400_000, "owner_name": "Other Metals Ltd"})
    assert wrong["nsr_percent"]["agree"] == 0 and wrong["nsr_percent"]["in_document_agree"] == 0
    assert wrong["cash_payments_value"]["grounded"] == 0 and wrong["cash_payments_value"]["grounding_checked"] == 1
    assert wrong["owner_name"]["agree"] == 0
    assert wrong["exp_comit_value"]["analyst_only"] == 1               # the model left out a value the analyst has


def test_a_case_whose_pdf_does_not_name_the_buyer_is_left_out_of_the_in_document_measure(tmp_path):
    fields = run(tmp_path, CASE["analyst"], {**CASE, "buyer_in_document": False})
    assert fields["nsr_percent"]["agree"] == 1 and fields["nsr_percent"]["in_document"] == 0


def test_case_ids_are_unique_and_every_case_says_whether_its_pdf_names_the_buyer():
    cases = score_pdf.load_cases()
    assert len(cases) == 236 and len({c["id"] for c in cases}) == 236
    assert all(isinstance(c["buyer_in_document"], bool) for c in cases)
    assert all(c["id"].startswith(Path(c["file"]).stem + "-") for c in cases)
