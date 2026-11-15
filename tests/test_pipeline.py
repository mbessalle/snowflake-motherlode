"""The Snowpark steps end to end against a stand-in session that records every statement.

This checks the wiring (which statement gets which parameters, what is written where, in which order).
It cannot check that Snowflake accepts the statements; that is what scripts/smoke_test.py is for.
"""
import json

from motherlode import cortex, ddl, pipeline

TEXT = ("Acme Gold Corp. has entered into an option agreement to acquire a 100% interest in the Big Onion property. "
        "The Company will pay $250,000 in cash and issue 2,000,000 common shares. The vendor retains a 2% NSR royalty.")
TRIAGE = {"is_project_deal": True, "project_name": "Big Onion", "reason": "Option over a named property."}
GOOD = {"project_name": "Big Onion", "cash_payments_value": 250000, "shares_amount": 2000000, "nsr_percent": 2,
        "cash_payments_currency": "CAD", "deal_type": "option agreement"}


def completion(body: dict) -> str:
    return json.dumps({"choices": [{"messages": json.dumps(body)}], "usage": {"prompt_tokens": 900, "completion_tokens": 300}})


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def collect(self):
        return self.rows


class FakeSession:
    """Answers AI statements from a script and SELECTs from `pending`; records everything else."""

    def __init__(self, extractions, pending=()):
        self.extractions, self.pending, self.calls = list(extractions), list(pending), []

    def sql(self, statement, params=None):
        params = params or []
        self.calls.append((statement, params))
        if statement == cortex.PARSE_SQL:
            return Rows([(json.dumps({"content": TEXT, "metadata": {"pageCount": 2}}),)])
        if statement == cortex.COMPLETE_SQL:
            if "You screen mining-industry announcements" in params[1]:
                return Rows([(completion(TRIAGE),)])
            return Rows([(completion({"reasoning_and_acknowledgement": {}, "database_updates": self.extractions.pop(0)}),)])
        if statement == cortex.DEAL_TYPE_SQL:
            return Rows([('{"labels": ["option or earn-in"]}',)])
        if statement == cortex.CONTINENT_SQL:
            return Rows([('{"labels": ["North America"]}',)])
        if statement == cortex.EXTRACT_TEXT_SQL:
            return Rows([(json.dumps({"response": {k: str(v) for k, v in self.extractions.pop(0).items()}}),)])
        if statement.startswith("SELECT d.DOC_ID"):
            return Rows(self.pending[:params[0]])
        return Rows([])

    def written(self, table: str) -> list[list]:
        return [params for statement, params in self.calls if statement.startswith(f"INSERT INTO MOTHERLODE.{table} ")]


def test_one_document_goes_from_file_to_the_deal_table():
    session = FakeSession([GOOD], pending=[("7", "7.html", "Acme options Big Onion", "acme gold corp")])
    summary = pipeline.process_new_announcements(session, "claude-sonnet-5", "claude-haiku-4-5", 5)
    assert "1 documents; stored=1" in summary

    assert session.calls[1] == (cortex.PARSE_SQL, ["@MOTHERLODE.RAW.ANNOUNCEMENTS", "7.html"])
    assert session.written("RAW.DOCUMENT_TEXT") == [["7", TEXT, 2, "AI_PARSE_DOCUMENT LAYOUT"]]
    assert session.written("CORE.SCREENING") == [["7", "AI_COMPLETE claude-haiku-4-5", True, "Big Onion", True,
                                                   "Option over a named property.", "option or earn-in", "North America"]]
    (attempt,) = session.written("CORE.EXTRACTION_ATTEMPTS")
    assert attempt[:3] == ["7", "AI_COMPLETE claude-sonnet-5", 1] and attempt[4:] == [900, 300]
    (deal,) = session.written("CORE.DEALS")
    assert deal[:6] == ["7", "North America", "option or earn-in", "AI_COMPLETE claude-sonnet-5", 1, "stored"]
    assert json.loads(deal[6]) == {"project_name": "Big Onion", "cash_payments_value": 250000.0, "shares_amount": 2000000.0,
                                   "nsr_percent": 2.0, "cash_payments_currency": "CAD", "deal_type": "option agreement"}
    assert session.written("CORE.REVIEW_QUEUE") == []


def test_unsupported_value_is_retried_then_withheld_and_queued():
    invented = {**GOOD, "shares_amount": 3500000}
    session = FakeSession([invented, invented])
    screening = {"project_name": "Big Onion", "continent": "North America", "deal_category": "option or earn-in"}
    result = pipeline.extract_and_store(session, "7", TEXT, screening, "claude-sonnet-5")
    assert (result.outcome, result.attempts) == ("stored_partial", 2)
    assert [a[2] for a in session.written("CORE.EXTRACTION_ATTEMPTS")] == [1, 2]
    retry_prompt = [p[1] for s, p in session.calls if s == cortex.COMPLETE_SQL][1]
    assert "Correction required" in retry_prompt and "shares_amount: 3.5e+06" in retry_prompt
    (queued,) = session.written("CORE.REVIEW_QUEUE")
    assert json.loads(queued[2]) == {"shares_amount": 3500000}
    (deal,) = session.written("CORE.DEALS")
    assert deal[5] == "stored_partial" and "shares_amount" not in json.loads(deal[6])


def test_ai_extract_answers_are_typed_and_get_a_single_attempt():
    session = FakeSession([{"project_name": "Big Onion", "cash_payments_value": "$250,000", "nsr_percent": "2%", "share_price": "None"}])
    result = pipeline.extract_and_store(session, "7", TEXT, {"project_name": "Big Onion"}, pipeline.AI_EXTRACT)
    assert (result.outcome, result.attempts) == ("stored", 1)
    statement, params = next(c for c in session.calls if c[0] == cortex.EXTRACT_TEXT_SQL)
    assert params[0] == TEXT and "Answer only for the project 'Big Onion'" in json.loads(params[1])["nsr_percent"]
    assert json.loads(session.written("CORE.DEALS")[0][6]) == {"project_name": "Big Onion", "cash_payments_value": 250000.0, "nsr_percent": 2.0}


def test_a_document_that_is_not_a_deal_stops_before_extraction():
    session = FakeSession([GOOD], pending=[("8", "8.html", "Drill results", "acme")])
    TRIAGE["is_project_deal"] = False
    try:
        summary = pipeline.process_new_announcements(session, "claude-sonnet-5", "claude-haiku-4-5", 5)
    finally:
        TRIAGE["is_project_deal"] = True
    assert "not_a_project_deal=1" in summary
    assert session.written("CORE.DEALS") == [] and session.written("CORE.EXTRACTION_ATTEMPTS") == []
    assert not any(s == cortex.DEAL_TYPE_SQL for s, _ in session.calls)      # no classification spend on a non-deal


def test_the_document_limit_reaches_the_query():
    session = FakeSession([], pending=[])
    pipeline.process_new_announcements(session, "claude-sonnet-5", "claude-haiku-4-5", 3)
    assert next(p for s, p in session.calls if s.startswith("SELECT d.DOC_ID")) == [3]


def test_project_name_check_ignores_case_and_punctuation():
    assert pipeline.named_in("the Big-Onion copper property", "Big Onion")
    assert not pipeline.named_in(TEXT, "Elko") and not pipeline.named_in(TEXT, None)


def test_insert_statement_has_one_cast_per_field_and_seven_parameters():
    statement = ddl.insert_deal_sql()
    assert statement.count("?") == 7 and statement.count("v:") == 36
    assert "v:news_date::DATE" in statement and "v:cash_payments_currency::VARCHAR(3)" in statement
