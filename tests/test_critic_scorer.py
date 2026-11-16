"""Failing tests for the confirmed findings of the scorer / telemetry critic. Each test names its finding."""
import importlib.util
import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

from evals import score_pdf as S
from motherlode import telemetry

ROOT = Path(__file__).resolve().parent.parent


def test_finding1_metres_are_not_millions():
    # "m" after a number is read as million: 5,000 metres of drilling become 5 billion
    assert S.parse_number("5,000 m") != 5e9
    assert S.for_validator({"drilling_amount": "5,000 m"})["drilling_amount"] in (None, 5000)


@pytest.mark.parametrize("text", ["600,000 AUD", "600,000 A$", "600000 dollars"])
def test_finding2_number_followed_by_currency_is_read(text):
    assert S.parse_number(text) == 600000


@pytest.mark.parametrize("text", ["0.0", "A$0", "0.00"])
def test_finding3_string_zero_is_empty_like_number_zero(text):
    assert S.filled(0) is False
    assert S.filled(text) is False


@pytest.mark.parametrize("unit", ["square kilometre", "sq kilometres", "sq. kilometres", "square mile", "sq miles"])
def test_finding4_common_area_units_are_known(unit):
    assert S.hectares(1, unit) is not None


def test_finding5_out_of_range_integer_is_exportable_to_otlp():
    value = telemetry._value(10**30)
    assert not isinstance(value, int) or abs(value) < 2**63     # OTLP int_value is int64


def test_finding6_log_outside_a_span_has_no_fake_ids(tmp_path):
    handler = telemetry._JsonlLogHandler(tmp_path / "logs.jsonl")
    record = logging.LogRecord("motherlode.x", logging.WARNING, __file__, 1, "outside", None, None)
    handler.emit(record)
    entry = json.loads((tmp_path / "logs.jsonl").read_text())
    assert entry["trace_id"] in (None, "") or set(entry["trace_id"]) != {"0"}


def test_finding7_skipped_and_unreadable_cases_leave_a_span(tmp_path, monkeypatch):
    seen = []

    @contextmanager
    def fake_span(name, **attributes):
        seen.append((name, attributes.get("case_id")))
        yield telemetry.Span()

    monkeypatch.setattr(S.telemetry, "span", fake_span)
    cases = [{"id": "a", "file": "a.pdf", "analyst": {}, "written_in_document": []},
             {"id": "b", "file": "b.pdf", "analyst": {}, "written_in_document": []}]
    (tmp_path / "b.json").write_text("{not json")
    S.score(cases, tmp_path, tmp_path)
    ids = {case_id for name, case_id in seen if name == "score_case"}
    assert ids == {"a", "b"}, f"cases with no span: {ids ^ {'a', 'b'}}"


def test_finding8_traces_where_agree_does_not_match_disagree():
    spec = importlib.util.spec_from_file_location("traces", ROOT / "scripts" / "traces.py")
    traces = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(traces)
    span = {"name": "score_case", "attributes": {"field.x.verdict": "disagree"}}
    assert traces.matches(span, ["field.x.verdict=agree"]) is False


def test_finding9_ticker_with_company_name_in_brackets_matches():
    assert S.same_ticker("ASX:ERW (Errawarra Resources)", "ASX:ERW")


def test_finding10_directory_named_like_a_reply_does_not_stop_the_run(tmp_path):
    (tmp_path / "a.json").mkdir()
    cases = [{"id": "a", "file": "a.pdf", "analyst": {}, "written_in_document": []}]
    S.score(cases, tmp_path, tmp_path)


def test_finding11_huge_float_does_not_stop_the_run(tmp_path):
    # validate.py:199 round(inf) raises OverflowError for 1e308; one such reply aborts the whole scoring run
    cases = S.load_cases()[:1]
    reply = dict(cases[0]["analyst"], shares_amount=1e308)
    (tmp_path / f"{cases[0]['id']}.json").write_text(json.dumps(reply))
    S.score(cases, tmp_path)
