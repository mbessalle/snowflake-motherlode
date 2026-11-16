"""Critic review of the split check (PR 14, PR 15). Each test fails on the code under review and names one
confirmed finding. Texts are short synthetic copies of the wording found in review/texts."""
from motherlode.validate import validate


def _candidates(report, name):
    c = report.candidates.get(name)
    return [] if c is None else (list(c) if isinstance(c, (list, tuple)) else [c])


# Finding 1: split_candidate ignores FIELD_SKIP (not_cash). A cash payment's direct check refuses
# "$1,000,000 of exploration commitments" and "A$600,000 in Shares", but both become cash split candidates
# (Redcastle, Hakes Find in evals/split_triage.jsonl), so only the judge stands between them and DERIVED.
def test_cash_candidate_is_not_an_exploration_amount():
    text = ("Mawson has met the obligations to achieve $1,000,000 of exploration commitments over a 5-year "
            "period on the Redcastle tenements.")
    report = validate({"cash_payments_value": 250000}, text, split_rule=False)
    assert _candidates(report, "cash_payments_value") == []


def test_cash_candidate_is_not_a_shares_amount():
    text = "The Company will provide the applicants with A$600,000 in Shares for the settlement over the Emerald tenements."
    report = validate({"cash_payments_value": 150000}, text, split_rule=False)
    assert _candidates(report, "cash_payments_value") == []


# Finding 2: the word rule (the default, process() without judge_split) has the same gap: both totals above
# are accepted as DERIVED cash splits because "tenements" stands nearby. On the 10 triage candidates it
# accepts 9, and 5 of those are not shared (Redcastle, Hakes Find, Speewah, Ida Holmes shares_value, plus
# the Kat Gap control).
def test_word_rule_does_not_split_an_exploration_amount_into_cash():
    text = ("Mawson has met the obligations to achieve $1,000,000 of exploration commitments over a 5-year "
            "period on the Redcastle tenements.")
    assert validate({"cash_payments_value": 250000}, text).fields["cash_payments_value"].status == "NOT_FOUND"


def test_word_rule_does_not_split_a_single_project_price():
    text = ("Tivan signed a binding term sheet to acquire 100% of the issued capital of Speewah Mining Pty Ltd, the "
            "owner of the Speewah Project, for total consideration of $20 million.")
    assert validate({"cash_payments_value": 4000000}, text).fields["cash_payments_value"].status == "NOT_FOUND"


# Finding 3: split_candidate stops at the first n (2..6) whose total is written. When x*2 is written for some
# other reason, the real total x*3 is never proposed, so the judge can only say no and the true split is lost.
def test_every_written_total_is_proposed_not_only_the_first_n():
    text = ("SubCo may buy down 1% of the royalty for C$1,000,000. The optionee must incur $1,500,000 of "
            "expenditures on the three Option Projects (Alpha, Beta and Gamma).")
    report = validate({"exp_comit_value": 500000}, text, split_rule=False)
    assert 3 in [c.n for c in _candidates(report, "exp_comit_value")]
