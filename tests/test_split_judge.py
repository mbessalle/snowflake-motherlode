"""A total shared between projects with no project word next to it: the validator proposes it, a judge reads
it, and an accepted split supports the value and the aggregate built on it. No network: the judge is a fake."""
from motherlode import judge
from motherlode.process import STORED, STORED_PARTIAL, process
from motherlode.validate import DERIVED, NOT_FOUND, resolve_splits, validate

# the wording of Newmont's Telfer and Havieron announcement
TEXT = ("Newmont expects to receive gross proceeds of up to $475 million, which includes: Cash consideration of "
        "$207.5 million, due upon closing. Equity consideration of $167.5 million in the form of Greatland shares, "
        "to be issued upon closing. Telfer and Havieron are being sold to Greatland.")
HALF = {"shares_value": 83_750_000}


def test_a_split_with_no_project_word_is_a_candidate_not_support():
    report = validate(HALF, TEXT)
    assert report.fields["shares_value"].status == NOT_FOUND
    candidate = report.candidates["shares_value"]
    assert (candidate.total, candidate.n) == (167_500_000, 2) and "Telfer and Havieron" in candidate.passage


def test_the_judge_decides_and_is_asked_only_about_candidates():
    asked = []

    def yes(project, field, value, candidate):
        asked.append((project, field, value, candidate.n))
        return 0.9

    report = resolve_splits(HALF, TEXT, "Telfer", yes)
    assert report.fields["shares_value"].status == DERIVED
    assert report.fields["shares_value"].evidence == "167,500,000 split across 2 projects (judged)"
    assert asked == [("Telfer", "shares_value", 83_750_000, 2)]
    assert resolve_splits(HALF, TEXT, "Telfer", lambda *a: 0.2).fields["shares_value"].status == NOT_FOUND
    asked.clear()
    resolve_splits({"shares_value": 167_500_000}, TEXT, "Telfer", yes)       # written in full: nothing to ask
    assert asked == []


def test_with_a_judge_even_a_split_next_to_a_project_word_is_judged():
    # the wording of Kat Gap's sale: one project's licences for A$7,000,000; half of it is not this project's value
    text = ("Sale Assets: Mining lease M74/249, Exploration licence E74/467. Consideration: A$7,000,000 payable "
            "within 5 business days.")
    half = {"cash_payments_value": 3_500_000}
    assert validate(half, text).fields["cash_payments_value"].status == DERIVED          # the word rule accepts it
    assert resolve_splits(half, text, "Kat Gap", lambda *a: 0.1).fields["cash_payments_value"].status == NOT_FOUND
    assert resolve_splits(half, text, "Kat Gap", lambda *a: 0.9).fields["cash_payments_value"].status == DERIVED


def test_an_accepted_split_supports_the_aggregate_too():
    extraction = {**HALF, "cash_payments_value": 103_750_000, "aggregate_deal_value": 187_500_000}
    assert not validate(extraction, TEXT).passed
    report = resolve_splits(extraction, TEXT, "Telfer", lambda *a: 0.9)
    # the cash payment is half of the written $207.5 million too, so both halves are candidates the judge accepts
    assert report.fields["cash_payments_value"].status == DERIVED and report.fields["shares_value"].status == DERIVED
    assert report.fields["aggregate_deal_value"].status == DERIVED and report.passed


def test_the_pipeline_uses_the_judge_when_it_has_one():
    def extract(text, project, feedback):
        return dict(HALF)

    assert process(TEXT, "Telfer", extract).outcome == STORED_PARTIAL                       # withheld without a judge
    assert process(TEXT, "Telfer", extract, judge_split=lambda *a: 0.9).outcome == STORED  # stored with one


def test_the_question_sent_to_the_decision_model(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(judge, "LEDGER", tmp_path / "ledger.jsonl")
    sent = []

    def post(body):
        sent.append(body)
        return {"answers": {"split": {"type": "noul", "noul": 0.83}}, "usage": {"cost": 0.00003}}

    candidate = validate(HALF, TEXT).candidates["shares_value"]
    assert judge.judge_split("Telfer", "shares_value", 83_750_000, candidate, post=post) == 0.83
    body = sent[0]
    assert body["model"] == judge.MODEL and body["questions"]["split"]["type"] == "noul"
    assert body["state"]["total"] == "167,500,000" and body["state"]["projects_sharing_it"] == 2
    assert "Telfer and Havieron" in body["state"]["passage"]
