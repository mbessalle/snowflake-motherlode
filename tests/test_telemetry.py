"""Tracing: each step's inputs and outputs reach the spans file, logs carry the id of the span that wrote
them, and long or structured values are stored safely."""
import json
import logging

import pytest

from motherlode import telemetry
from motherlode.validate import validate

pytest.importorskip("opentelemetry.sdk")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    # OpenTelemetry allows one global provider per process, so the whole module shares one run
    return telemetry.setup("test", tmp_path_factory.mktemp("traces"))


def spans(run):
    from opentelemetry import trace

    trace.get_tracer_provider().force_flush()
    with open(run["spans"]) as f:
        return [json.loads(line) for line in f]


def test_a_span_records_its_inputs_and_outputs_and_nests(run):
    with telemetry.span("outer", input_reply={"nsr_percent": 2, "owner": None}) as outer:
        with telemetry.span("inner", step=1) as inner:
            inner.set(output_ok=True, output_list=[1, 2])
        outer.set(output_text="x" * (telemetry.MAX_TEXT + 10))
    found = {s["name"]: s for s in spans(run) if s["name"] in ("outer", "inner")}
    assert json.loads(found["outer"]["attributes"]["input_reply"]) == {"nsr_percent": 2, "owner": None}
    assert found["outer"]["attributes"]["output_text"].endswith(f"[{telemetry.MAX_TEXT + 10} chars]")
    assert found["inner"]["attributes"]["output_ok"] is True and found["inner"]["attributes"]["output_list"] == "[1, 2]"
    assert found["inner"]["parent_id"] == found["outer"]["span_id"]
    assert found["inner"]["trace_id"] == found["outer"]["trace_id"]


def test_the_validator_span_holds_each_fields_value_status_and_evidence(run):
    validate({"nsr_percent": 2, "cash_payments_value": 999999}, "The vendor keeps a 2% NSR royalty.")
    span = [s for s in spans(run) if s["name"] == "validate"][-1]["attributes"]
    assert span["field.nsr_percent.status"] == "FOUND" and "2% NSR" in span["field.nsr_percent.evidence"]
    assert span["field.cash_payments_value.status"] == "NOT_FOUND"
    assert span["output_passed"] is False and "cash_payments_value" in span["output_not_found"]


def test_a_log_line_carries_the_id_of_its_span_and_appears_on_the_span(run):
    with telemetry.span("logging step") as s:
        logging.getLogger("motherlode.test").warning("something to look at")
        trace_id, span_id = s.ids
    with open(run["logs"]) as f:
        entry = [json.loads(line) for line in f][-1]
    assert entry["message"] == "something to look at" and entry["level"] == "WARNING"
    assert (entry["trace_id"], entry["span_id"]) == (trace_id, span_id)
    span = [x for x in spans(run) if x["name"] == "logging step"][-1]
    assert span["events"][0]["attributes"]["message"] == "something to look at"


def test_without_opentelemetry_a_span_does_nothing(monkeypatch):
    monkeypatch.setattr(telemetry, "trace", None)
    with telemetry.span("ignored", x=1) as s:
        s.set(y={"a": 1}).event("e")
        assert s.ids is None
