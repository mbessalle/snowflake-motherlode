"""Every way an announcement can leave the extract, validate, retry, quarantine loop."""
from motherlode import process
from motherlode.process import NO_DATA, REVIEW_ONLY, STORED, STORED_PARTIAL

TEXT = ("The Company may exercise the Option by paying $250,000 in cash and issuing 2,000,000 common shares. "
        "The vendor retains a 2% NSR royalty and the Company will earn a 100% interest.")
GOOD = {"project_name": "Acme", "cash_payments_value": 250000, "shares_amount": 2000000, "nsr_percent": 2}
INVENTED = {**GOOD, "shares_amount": 3500000}


def scripted(*replies):
    """An extract function that returns the given replies in order and records what it was asked."""
    calls = []

    def extract(text, project_name, feedback):
        calls.append(feedback)
        return replies[len(calls) - 1]

    extract.calls = calls
    return extract


def test_supported_extraction_is_stored_on_the_first_attempt():
    extract = scripted(GOOD)
    result = process.process(TEXT, "Acme", extract)
    assert (result.outcome, result.attempts, result.withheld) == (STORED, 1, {})
    assert result.row == {"project_name": "Acme", "cash_payments_value": 250000.0, "shares_amount": 2000000.0, "nsr_percent": 2.0}
    assert extract.calls == [None] and not result.needs_review


def test_unsupported_value_gets_one_retry_that_names_it():
    extract = scripted(INVENTED, GOOD)
    result = process.process(TEXT, "Acme", extract)
    assert (result.outcome, result.attempts) == (STORED, 2)
    assert extract.calls[0] is None and "shares_amount: 3.5e+06" in extract.calls[1]
    assert result.first_report["not_found"] == ["shares_amount"] and result.report["passed"]


def test_still_unsupported_value_is_withheld_and_the_rest_is_stored():
    result = process.process(TEXT, "Acme", scripted(INVENTED, INVENTED))
    assert (result.outcome, result.attempts) == (STORED_PARTIAL, 2)
    assert result.withheld == {"shares_amount": 3500000} and result.needs_review
    assert "shares_amount" not in result.row and result.row["cash_payments_value"] == 250000.0


def test_malformed_record_is_not_written_at_all():
    bad = {**GOOD, "cash_payments_currency": "dollars"}
    result = process.process(TEXT, "Acme", scripted(bad, bad))
    assert result.outcome == REVIEW_ONLY and result.row is None and result.needs_review
    assert any(issue.startswith("cash_payments_currency") for issue in result.report["schema_issues"])


def test_empty_reply_stops_without_writing():
    result = process.process(TEXT, "Acme", scripted({}))
    assert (result.outcome, result.attempts, result.row) == (NO_DATA, 1, None)


def test_attempt_limit_is_respected():
    extract = scripted(INVENTED, INVENTED, GOOD)
    assert process.process(TEXT, "Acme", extract, max_attempts=1).attempts == 1
    assert len(extract.calls) == 1


def test_an_update_never_erases_a_stored_value():
    merged = process.merge_without_nulls({"nsr_percent": 2.0, "owner_name": "Acme"}, {"nsr_percent": None, "owner_name": "", "share_price": 0.25})
    assert merged == {"nsr_percent": 2.0, "owner_name": "Acme", "share_price": 0.25}
