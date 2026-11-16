"""The validator decides what reaches the database, so each rule is pinned by a case that would break it."""
import pytest

from motherlode.validate import DERIVED, EMPTY, FOUND, NOT_FOUND, find_number, sum_of_amounts, validate, written_amounts

OPTION = (
    "Vancouver, May 31, 2023. The Company may exercise the Option by paying $250,000 in cash on closing and "
    "$250,000 on the first anniversary, issuing 2,000,000 common shares at a deemed price of $0.25 per share, "
    "and incurring $1,000,000 of expenditures on the five Option Projects. The vendor retains a 2% NSR royalty. "
    "The property covers 4,857 hectares and the Company will earn a 100% interest."
)


def status(extraction, text=OPTION):
    return {name: check.status for name, check in validate(extraction, text).fields.items()}


def test_values_written_in_the_article_are_found():
    s = status({"shares_amount": 2000000, "share_price": 0.25, "nsr_percent": 2, "interest_acquired_percent": 100,
                "project_area": 4857})
    assert {s[k] for k in ("shares_amount", "share_price", "nsr_percent", "interest_acquired_percent", "project_area")} == {FOUND}


def test_evidence_snippet_is_kept():
    check = validate({"shares_amount": 2000000}, OPTION).fields["shares_amount"]
    assert "2,000,000 common shares" in check.evidence


def test_invented_number_is_not_found_and_blocks():
    report = validate({"shares_amount": 3500000, "nsr_percent": 2}, OPTION)
    assert report.fields["shares_amount"].status == NOT_FOUND
    assert report.not_found == ["shares_amount"] and not report.passed
    assert "shares_amount: 3.5e+06" in report.feedback()


def test_empty_fields_do_not_block():
    report = validate({"shares_amount": None, "cash_payments_value": "", "nsr_percent": 0}, OPTION)
    assert set(status({"shares_amount": None}).values()) == {EMPTY}
    assert report.passed and report.checked == 0


def test_cash_as_sum_of_tranches_is_derived():
    check = validate({"cash_payments_value": 500000}, OPTION).fields["cash_payments_value"]
    assert check.status == DERIVED and check.evidence == "sum of 250,000 + 250,000"


def test_an_amount_is_reused_only_if_written_twice():
    once = "The Company will pay $250,000 in cash and issue shares."
    assert status({"cash_payments_value": 500000}, once)["cash_payments_value"] == NOT_FOUND


def test_commitment_shared_across_projects_is_derived():
    check = validate({"exp_comit_value": 200000}, OPTION).fields["exp_comit_value"]
    assert check.status == DERIVED and check.evidence == "1,000,000 split across 5 projects"


def test_shares_value_is_shares_times_price_only_with_supported_inputs():
    good = status({"shares_amount": 2000000, "share_price": 0.25, "shares_value": 500000})
    assert good["shares_value"] in (DERIVED, FOUND)
    # 3,000,000 shares are not in the article, so their product is not accepted either
    bad = status({"shares_amount": 3000000, "share_price": 0.25, "shares_value": 750000})
    assert bad["shares_value"] == NOT_FOUND


def test_aggregate_is_sum_of_supported_parts():
    extraction = {"cash_payments_value": 500000, "exp_comit_value": 200000, "shares_amount": 2000000, "share_price": 0.25,
                  "shares_value": 500000, "aggregate_deal_value": 1200000}
    report = validate(extraction, OPTION)
    assert report.fields["aggregate_deal_value"].status == DERIVED and report.passed


def test_aggregate_with_unsupported_part_is_not_accepted():
    extraction = {"cash_payments_value": 777000, "aggregate_deal_value": 777000}
    assert status(extraction)["aggregate_deal_value"] == NOT_FOUND


def test_drilling_metres_convert_at_250_usd():
    text = "The Company has committed to 4,000 metres of drilling in the first year."
    check = validate({"exp_comit_value": 1000000}, text).fields["exp_comit_value"]
    assert check.status == DERIVED and "4,000 metres" in check.evidence


@pytest.mark.parametrize("text, value, percent, expected", [
    ("a payment of $1.5 million on closing", 1500000, False, True),
    ("a payment of US$250K on signing", 250000, False, True),
    ("a two percent net smelter royalty", 2, True, True),
    ("a 2.0% NSR", 2, True, True),
    ("issued 12,000,000 shares", 2000000, False, False),   # must not match inside a longer number
    ("priced at $0.255 per share", 0.25, False, False),
    ("in fiscal 2023 the company", 2023, False, True),      # written, but see written_amounts for sums
    ("holds a 20% interest", 2, True, False),
    ("can acquire a seventy-five percent interest", 75, True, True),
    ("up to a one-hundred percent interest", 100, True, True),
    ("a fifty one per cent stake", 51, True, True),
    ("a seventy-five percent interest", 5, True, False),      # "five percent" inside "seventy-five percent"
])
def test_number_matching(text, value, percent, expected):
    assert bool(find_number(text, value, percent)) is expected


def test_years_and_bare_numbers_are_not_amounts():
    text = "In 2023 and 2024 the Company spent $1,950 and 2,024 dollars, then 3 million more over 2025."
    assert written_amounts(text) == [1950.0, 2024.0, 3000000.0]


def test_sums_never_mix_dollars_and_shares():
    text = "The Company will pay $1,000,000 and issue 2,000,000 common shares and 500,000 warrants."
    assert written_amounts(text, exclude="shares") == [1000000.0]
    assert written_amounts(text, exclude="money") == [500000.0, 2000000.0]
    # 3,000,000 shares would only be reachable by adding dollars to shares
    assert status({"shares_amount": 3000000}, text)["shares_amount"] == NOT_FOUND
    assert status({"shares_amount": 2500000}, text)["shares_amount"] == DERIVED


def test_schema_issues_block_even_when_values_are_grounded():
    report = validate({"nsr_percent": 2, "cash_payments_currency": "dollars", "news_date": "31/05/2023",
                       "shares_amount": "two million", "interest_acquired_percent": 150}, OPTION)
    assert not report.passed
    fields = sorted(issue.split(":")[0] for issue in report.schema_issues)
    assert fields == ["cash_payments_currency", "interest_acquired_percent", "news_date", "shares_amount"]
    assert any("less than or equal to 100" in issue for issue in report.schema_issues)


def test_shares_value_from_a_price_the_article_states():
    text = "The Company issued 14,195,506 common shares at a deemed price of $0.1278 per share."
    report = validate({"shares_amount": 14195506, "shares_value": 1814185.67}, text)
    assert report.fields["shares_value"].status == DERIVED and "0.1278" in report.fields["shares_value"].evidence
    # a value that needs a price nobody wrote stays unsupported
    assert validate({"shares_amount": 14195506, "shares_value": 5000000}, text).fields["shares_value"].status == NOT_FOUND


def test_a_share_count_is_not_evidence_for_a_cash_payment():
    text = ("Consideration is 2,000,000 common shares issuable in four tranches of 500,000 shares each, "
            "plus a cash payment of $150,000 on closing.")
    report = validate({"cash_payments_value": 500000, "exp_comit_value": 2000000, "shares_amount": 2000000}, text)
    assert report.fields["cash_payments_value"].status == NOT_FOUND     # 500,000 is only written as shares
    assert report.fields["exp_comit_value"].status == NOT_FOUND         # 2,000,000 is only written as shares
    assert report.fields["shares_amount"].status == FOUND
    assert validate({"cash_payments_value": 150000}, text).fields["cash_payments_value"].status == FOUND
    assert validate({"shares_amount": 150000}, text).fields["shares_amount"].status == NOT_FOUND   # $150,000 is money


def test_the_fast_sum_check_gives_the_same_answer_as_trying_every_combination():
    """sum_of_amounts replaced the original's search over every combination; the yes or no must not change."""
    import itertools
    import random

    rng = random.Random(7)
    for _ in range(2000):
        amounts = [rng.choice((5000, 10000, 25000, 50000, 100000, 125000, 250000)) for _ in range(rng.randint(0, 8))]
        target = rng.choice((15000, 30000, 75000, 150000, 175000, 300000, 375000, 400000, 999999))
        for terms in (2, 3):
            candidates = [a for a in amounts if a < target]
            brute = any(abs(sum(combo) - target) <= 0.5 for n in range(2, terms + 1)
                        for combo in itertools.combinations(candidates, n))
            found = sum_of_amounts(target, amounts, terms)
            assert (found is not None) is brute
            if found:
                assert abs(sum(found) - target) <= 0.5 and 2 <= len(found) <= terms


@pytest.mark.parametrize("text, value, kind, expected", [
    # found by the critic in the source PDFs: metres read as millions
    ("results including 6m @ 4.79% Cu, 0.36g/t Au from 1m", 6_000_000, "shares", False),
    ("sampled at intervals (0.1m to 3.1m)", 100_000, "money", False),
    ("a horizontal accuracy of 0.3m", 300_000, "money", False),
    ("a resource of 2.3 million ounces of gold", 2_300_000, "money", False),
    ("an exploration target of 1.2 billion tonnes", 1_200_000_000, "money", False),
    # a unit with a money or share context still counts
    ("a payment of $6m in cash", 6_000_000, "money", True),
    ("the issue of 6m shares to the vendor", 6_000_000, "shares", True),
    ("A$2.5 million on completion", 2_500_000, "money", True),
    ("spending 2.5 million dollars", 2_500_000, "money", True),
    ("a C$250k option payment", 250_000, "money", True),
    ("an enterprise value of $1.2bn", 1_200_000_000, "money", True),
    ("valued at 1.2 billion dollars", 1_200_000_000, "money", True),
    ("an acquisition for US$1.25 billion", 1_250_000_000, "money", True),
])
def test_a_million_thousand_or_billion_unit_counts_only_as_money_or_shares(text, value, kind, expected):
    assert bool(find_number(text, value, kind=kind)) is expected


def test_a_stated_shares_value_split_between_projects_is_derived_and_the_scraped_one_is_checked():
    # wording of the Bonnie Vale announcement; the deal covers two projects
    text = ("Bonnie Vale and Kunanalling projects, revised terms: the Company may issue the $1.1 million of share "
            "consideration at any time.")
    for field in ("shares_value", "shares_value_scraped"):
        assert validate({field: 1_100_000}, text).fields[field].status == FOUND
        check = validate({field: 550_000}, text).fields[field]
        assert check.status == DERIVED and "split across 2 projects" in check.evidence
        assert validate({field: 777_000}, text).fields[field].status == NOT_FOUND


def test_written_amounts_skip_metres_and_physical_quantities():
    text = "6m @ 4.79% Cu, $1.5m in cash, 2 million tonnes, 3 million shares and a $1.2bn market value"
    assert written_amounts(text) == [1_500_000.0, 3_000_000.0, 1_200_000_000.0]
    assert validate({"shares_amount": 6_000_000}, "6m @ 4.79% Cu").fields["shares_amount"].status == NOT_FOUND
