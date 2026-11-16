"""Critic findings on motherlode/validate.py. Each test fails on the commit under review."""
import pytest

from motherlode.validate import DERIVED, MONEY, NOT_FOUND, find_number, validate, written_amounts


# Finding 1: a number written with trailing zeros is not found
@pytest.mark.parametrize("text,value,kwargs", [
    ("issued at $0.10 per share", 0.1, {}),
    ("issued at $0.50 per share", 0.5, {}),
    ("for A$7.0 million (Purchase Price)", 7_000_000, {"kind": "money"}),
    ("the acquisition is $14.0m, comprising", 14_000_000, {"kind": "money"}),
    ("a payment of $2.50 million", 2_500_000, {"kind": "money"}),
    ("a 5.50% royalty", 5.5, {"percent": True}),
])
def test_finding_1_trailing_zero_decimals_are_the_same_number(text, value, kwargs):
    assert find_number(text, value, **kwargs)


# Finding 2: a price written $0.10 makes shares_value fail even though shares x price is stated
def test_finding_2_price_with_trailing_zero_supports_shares_value():
    text = "The Company issues 2,000,000 shares at $0.10 per share."
    report = validate({"shares_amount": 2_000_000, "share_price": 0.1, "shares_value": 200_000}, text)
    assert report.passed, report.not_found


# Finding 3: quantities with a physical unit (written with a thousands separator) are summed as money
def test_finding_3_tonnes_and_ounces_are_not_money_amounts():
    text = "Resource 5,000,000 tonnes and 3,000,000 oz Au."
    assert written_amounts(text, only=MONEY) == [] and written_amounts(text, exclude="shares") == []
    assert validate({"cash_payments_value": 8_000_000}, text).fields["cash_payments_value"].status == NOT_FOUND


# Finding 4: the metres rule accepts any matching number, not only metres
def test_finding_4_metres_rule_needs_metres():
    report = validate({"exp_comit_value": 2_500_000}, "The Company holds 10,000 shares.")
    assert report.fields["exp_comit_value"].status == NOT_FOUND


# Finding 5: a royalty or interest percentage is matched to any percentage in the text
def test_finding_5_nsr_percent_needs_royalty_context():
    report = validate({"nsr_percent": 1}, "Cu 0.8% Zn 1.0% Pb")
    assert report.fields["nsr_percent"].status == NOT_FOUND


def test_finding_5b_percentage_is_not_percent():
    assert find_number("2 percentage points", 2, percent=True) is None


# Finding 6: page numbers/table numbers make a fabricated sum DERIVED
def test_finding_6_unrelated_numbers_do_not_explain_a_payment():
    text = "Table 1: holders 100,000 and 200,000 of the register."
    assert validate({"cash_payments_value": 300_000}, text).fields["cash_payments_value"].status != DERIVED
