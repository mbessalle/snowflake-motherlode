"""The parts of the scripts that decide something, without an account: the agent answer check, the question
set, the smoke-test list, the cost plan and the downloaded-page identity check."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent, smoke, run_pipeline, fetch_documents = load("agent"), load("smoke_test"), load("run_pipeline"), load("fetch_documents")


@pytest.mark.parametrize("answer, truth, expected", [
    ("There are 31 deals in the table.", 31, True),
    ("The largest cash payment was CAD 1,250,000.", 1250000.0, True),
    ("The largest payment was 1.25 million Canadian dollars.", 1250000.0, True),
    ("The highest royalty is 2.5%.", 2.5, True),
    ("There are 13 deals.", 31, False),
    ("I could not find that.", 31, False),
    ("In 2023 there were several deals.", 20, False),       # 2023 must not count as containing 20
    ("None found.", None, False),
])
def test_answer_passes_only_if_it_contains_the_number_sql_computed(answer, truth, expected):
    assert agent.answer_contains(answer, truth) is expected


def test_answer_text_is_collected_from_a_nested_response():
    response = {"role": "assistant", "content": [{"type": "thinking", "thinking": {"text": "plan"}},
                                                 {"type": "text", "text": "There are 31 deals."}]}
    assert agent.texts(response) == ["plan", "There are 31 deals."]


def test_agent_questions_have_a_read_only_truth_query_on_the_deal_table():
    questions = [json.loads(line) for line in (ROOT / "evals" / "agent_questions.jsonl").read_text().splitlines() if line.strip()]
    assert len(questions) == 10 and len({q["id"] for q in questions}) == 10
    for q in questions:
        assert q["truth_sql"].startswith("SELECT ") and "MOTHERLODE.CORE.DEALS" in q["truth_sql"]
        assert not any(word in q["truth_sql"].upper() for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", ";"))


def test_smoke_test_covers_every_service_the_project_uses():
    names = " | ".join(name for name, _, _ in smoke.checks("a.html", "b.pdf"))
    for service in ("AI_COMPLETE", "AI_CLASSIFY", "AI_FILTER", "AI_EXTRACT on text", "AI_COUNT_TOKENS", "Cortex Agents",
                    "AI_PARSE_DOCUMENT on a staged HTML", "AI_EXTRACT on a staged PDF", "usage view"):
        assert service in names
    assert len(smoke.checks(None, None)) == 10


def test_run_plan_is_higher_for_a_dearer_extractor_and_zero_parse_for_text(tmp_path, monkeypatch):
    monkeypatch.setattr(run_pipeline, "DOCUMENTS", tmp_path)
    (tmp_path / "1.txt").write_text("x" * 8000)
    (tmp_path / "2.html").write_text("x" * 8000)
    text_only = run_pipeline.planned_usd([{"file": "1.txt"}], "claude-haiku-4-5")
    with_parse = run_pipeline.planned_usd([{"file": "2.html"}], "claude-haiku-4-5")
    assert with_parse > text_only > 0
    assert run_pipeline.planned_usd([{"file": "1.txt"}], "claude-sonnet-5") > text_only
    assert run_pipeline.slug("AI_COMPLETE claude-sonnet-5") == "ai-complete-claude-sonnet-5"


def test_a_downloaded_page_must_carry_the_headline():
    title = "Thunderbird Completes Acquisition of Option on Big Onion Copper-Molybdenum Property"
    assert fetch_documents.headline_share(title, "Thunderbird ... Big Onion copper molybdenum ...") == 1.0
    assert fetch_documents.headline_share(title, "Please enable JavaScript and cookies to continue") == 0.0


def test_a_pdf_is_digital_scanned_or_mixed_by_the_text_on_its_pages():
    inventory = load("inventory_pdfs")
    assert inventory.classify([1800, 2400, 950]) == "digital"
    assert inventory.classify([0, 12, 0]) == "scanned"                  # a few stray characters are not a text layer
    assert inventory.classify([1500, 0, 0]) == "mixed"                  # a typed cover page on a scan
    assert inventory.classify([]) == "unreadable"
    assert inventory.classify([inventory.MIN_CHARS_PER_PAGE]) == "digital"


def test_identical_files_are_marked_as_copies_of_the_first_one():
    inventory = load("inventory_pdfs")
    entries = [{"project": "yalgoo", "file": "yalgoo_451.pdf", "sha256_16": "aa"},
               {"project": "yalgoo", "file": "yalgoo_452.pdf", "sha256_16": "aa"},
               {"project": "wudinna", "file": "wudinna_9.pdf", "sha256_16": "bb"}]
    inventory.mark_duplicates(entries)
    assert "duplicate_of" not in entries[0] and "duplicate_of" not in entries[2]
    assert entries[1]["duplicate_of"] == "yalgoo/yalgoo_451.pdf"


def test_the_case_document_is_the_pdf_with_the_most_scraped_values_written_in_it():
    cases = load("build_pdf_cases")
    row = {"nsr_percent": 2, "interest_acquired_percent": 80, "cash_payments_value": 250000, "share_price": 0.03,
           "aggregate_deal_value": 750000, "shares_amount": None}
    terms = "The Company will pay $250,000 in cash for an 80% interest. The vendor retains a 2% NSR royalty."
    notice = "Completion of due diligence on the project. The Company holds an 80% interest."
    assert cases.written_fields(row, terms) == ["nsr_percent", "interest_acquired_percent", "cash_payments_value"]
    assert cases.written_fields(row, notice) == ["interest_acquired_percent"]
    # share price and the aggregate are never looked for in the text: one is market data, the other is calculated
    assert "share_price" not in cases.SCRAPED and "aggregate_deal_value" not in cases.SCRAPED
    chosen = cases.choose([("a_1.pdf", cases.written_fields(row, notice), len(notice)), ("a_2.pdf", cases.written_fields(row, terms), len(terms))])
    assert chosen == ("a_2.pdf", ["nsr_percent", "interest_acquired_percent", "cash_payments_value"])
    assert cases.choose([]) is None
    assert cases.choose([("short.pdf", [], 10), ("long.pdf", [], 900)])[0] == "long.pdf"      # a tie goes to the longer text


def test_a_value_not_written_as_such_is_derived_by_the_extraction_rules():
    cases = load("build_pdf_cases")
    text = ("The Company will pay $250,000 on signing and $1,000,000 on a decision to mine, and will spend "
            "$2,000,000 across the Bingara and Nundle projects. The tenements cover 45.5 km2.")
    assert cases.derived_by_rule("cash_payments_value", 1250000, text) == "sum"        # tranches summed
    assert cases.derived_by_rule("project_area", 4550, text) == "km2 to ha"
    assert cases.derived_by_rule("cash_payments_value", 777000, text) is None
    assert cases.derived_by_rule("nsr_percent", 2, text) is None                        # no rule for a percentage


# Findings of the critic agent (review of 16 Nov 2026), one test each.

def test_a_split_between_projects_is_no_longer_derived():                       # finding 3
    cases = load("build_pdf_cases")
    text = "The resource is 1,005,000 ounces at 6.1g/t Au. A further 1,005,000 ounces are inferred."
    assert cases.derived_by_rule("exp_comit_value", 502500, text) is None


def test_a_sum_uses_only_amounts_of_the_fields_kind():                          # finding 2
    cases = load("build_pdf_cases")
    text = "A resource of 370,000 tonnes and 3,650,000 tonnes. Map sheet 1:250,000. The Company pays $100,000."
    assert cases.derived_by_rule("cash_payments_value", 4020000, text) is None       # tonnes are not money
    assert cases.derived_by_rule("cash_payments_value", 350000, text) is None        # nor is a map scale


@pytest.mark.parametrize("text", ["within a 12 months period", "16 km of strike length", "anomalies above 12 ppm",
                                  "located 12km west of town", "a resource of 12koz"])
def test_an_area_needs_a_km2_unit(text):                                        # finding 4
    assert load("build_pdf_cases").derived_by_rule("project_area", 1200, text) is None


@pytest.mark.parametrize("text", ["covering approximately 12km²", "an area of 12 km2", "12 sq km of tenements",
                                  "12 square kilometres", "12km 2 of leases"])
def test_an_area_in_km2_is_derived(text):
    assert load("build_pdf_cases").derived_by_rule("project_area", 1200, text) == "km2 to ha"


def test_the_buyer_must_be_named_in_full_or_by_its_ticker():                    # finding 9
    cases = load("build_pdf_cases")
    assert not cases.buyer_named("West Coast Silver Ltd.", "Acquisition of the Challenger West project")
    assert not cases.buyer_named("New Frontier Minerals Limited", "located in New South Wales")
    assert cases.buyer_named("West Coast Silver Ltd.", "West Coast Silver Limited acquires ...")
    assert cases.buyer_named("New Frontier Minerals Limited", "NFM announces ...", "ASX:NFM")
    assert not cases.buyer_named("New Frontier Minerals Limited", "nfm in lower case is a word", "ASX:NFM")


def test_calculated_fields_are_checked_by_the_analysts_formulas():
    cases = load("build_pdf_cases")
    good = {"cash_payments_value": 250000, "exp_comit_value": 100000, "shares_value": 60000, "aggregate_deal_value": 410000,
            "shares_amount": 2000000, "share_price": 0.03}
    assert cases.formula_checks(good) == {"aggregate": True, "shares_value": True}
    assert cases.formula_checks({**good, "aggregate_deal_value": 999999})["aggregate"] is False
    assert cases.formula_checks({"nsr_percent": 2}) == {}                 # nothing to check without the inputs
    # a shares value is either stated in the announcement or computed from the number of shares and a price
    text = "Consideration is 2,000,000 shares with a deemed value of $75,000."
    assert cases.shares_value_source({"shares_value": 75000, "shares_amount": 2000000, "share_price": 0.03}, text) == "stated"
    assert cases.shares_value_source({"shares_value": 60000, "shares_amount": 2000000, "share_price": 0.03}, text) == "computed"
    assert cases.shares_value_source({"shares_value": 75000, "shares_amount": 2000000, "share_price": 0.0375}, text) == "both"
    assert cases.shares_value_source({"shares_value": 91000, "shares_amount": 2000000, "share_price": 0.03}, text) == "unexplained"
    assert cases.shares_value_source({"shares_amount": 2000000}, text) is None
    assert cases.normalise("Yarri / Bunjarra (West)") == "yarri_bunjarra_west"


def test_a_converted_value_is_compared_with_the_amount_times_the_rate_of_the_day():
    conversion = load("check_conversion")
    rates = {"2025-03-07": {"USD": 0.63}, "2025-03-10": {"USD": 0.628}}
    assert conversion.rate_on(rates, "2025-03-09") == 0.63               # a Sunday uses the Friday before
    assert conversion.rate_on(rates, "2025-03-10T00:00:00") == 0.628
    assert conversion.classify(1000.0, 1000.0, 0.63) == "same"
    assert conversion.classify(1000.0, 632.0, 0.63) == "within 2%"
    assert conversion.classify(1000.0, 655.0, 0.63) == "within 5%"
    assert conversion.classify(1000.0, 700.0, 0.63) == "further off"


def test_manifest_lists_every_screening_case_and_holds_no_text():
    manifest = [json.loads(line) for line in (ROOT / "data" / "manifest.jsonl").read_text().splitlines() if line.strip()]
    cases = [json.loads(line) for line in (ROOT / "evals" / "screening_cases.jsonl").read_text().splitlines() if line.strip()]
    assert {e["id"] for e in manifest} == {str(c["id"]) for c in cases} and len(manifest) == 43
    assert all(e["obtained"] in ("downloaded", "saved_text") and e["file"] for e in manifest)
    assert all(set(e) <= {"id", "title", "source_url", "file", "obtained", "http", "same_text_as_recorded",
                          "headline_share", "sha256_16", "bytes"} for e in manifest)
