"""The decision-model judge without a network: answers are cached, cost is logged and capped, and the
blind-labeller audit flags an agent that looked outside its folder."""
import importlib.util
import json
from pathlib import Path

import pytest

from motherlode import judge

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(judge, "LEDGER", tmp_path / "ledger.jsonl")
    return tmp_path


def fake_post(calls):
    def post(body):
        calls.append(body)
        return {"answers": {"q": {"type": "noul", "noul": 0.9}}, "model": body["model"],
                "usage": {"input_tokens": 1000, "cost": 0.00004}}
    return post


def test_an_answer_is_cached_and_its_cost_logged(sandbox):
    calls = []
    question = {"q": judge.noul("Is it a royalty?", "yes", "no")}
    first = judge.ask({"passage": "a 2% NSR"}, question, post=fake_post(calls))
    second = judge.ask({"passage": "a 2% NSR"}, question, post=fake_post(calls))
    assert first == second == {"q": {"type": "noul", "noul": 0.9}}
    assert len(calls) == 1                                  # the second answer came from the cache
    assert judge.spent() == pytest.approx(0.00004)
    judge.ask({"passage": "another text"}, question, post=fake_post(calls))
    assert len(calls) == 2 and judge.spent() == pytest.approx(0.00008)


def test_the_spend_cap_stops_a_call_before_it_is_made(sandbox, monkeypatch):
    monkeypatch.setenv("JUDGE_SPEND_CAP_USD", "0.00005")
    calls = []
    question = {"q": judge.noul("Is it a royalty?", "yes", "no")}
    judge.ask({"passage": "one"}, question, post=fake_post(calls))
    judge.ask({"passage": "two"}, question, post=fake_post(calls))           # ledger 0.00008, over the cap now
    with pytest.raises(judge.SpendCapReached):
        judge.ask({"passage": "three"}, question, post=fake_post(calls))
    assert len(calls) == 2
    judge.ask({"passage": "one"}, question, post=fake_post(calls))           # a cached answer is still served


def test_questions_have_the_decisions_api_shape():
    assert judge.choice("Which?", {"a": "A", "b": "B"}) == {"type": "choice", "instructions": "Which?", "criteria": {"a": "A", "b": "B"}}
    assert judge.noul("Is it?", "yes", "no")["criteria"] == {"true": "yes", "false": "no"}


def test_the_audit_flags_reads_outside_the_folder_and_forbidden_names(tmp_path):
    spec = importlib.util.spec_from_file_location("audit", ROOT / "scripts" / "audit_agent_reads.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    allowed = tmp_path / "labelling"
    transcript = tmp_path / "t.jsonl"
    lines = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"cd {allowed} && cat questions_A.jsonl"}}]}},
             {"message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/home/x/repo/evals/judge_results.jsonl"}}]}},
             {"message": {"content": [{"type": "text", "text": "never open judge_results"}]}}]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    calls = audit.tool_calls(transcript)
    assert len(calls) == 2                                   # text in a message is not a tool call
    assert audit.suspect(calls[0], allowed, audit.FORBIDDEN) == []
    reasons = audit.suspect(calls[1], allowed, audit.FORBIDDEN)
    assert "names judge_results" in reasons and any(r.startswith("path outside") for r in reasons)
