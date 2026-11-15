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


def test_manifest_lists_every_screening_case_and_holds_no_text():
    manifest = [json.loads(line) for line in (ROOT / "data" / "manifest.jsonl").read_text().splitlines() if line.strip()]
    cases = [json.loads(line) for line in (ROOT / "evals" / "screening_cases.jsonl").read_text().splitlines() if line.strip()]
    assert {e["id"] for e in manifest} == {str(c["id"]) for c in cases} and len(manifest) == 43
    assert all(e["obtained"] in ("downloaded", "saved_text") and e["file"] for e in manifest)
    assert all(set(e) <= {"id", "title", "source_url", "file", "obtained", "http", "same_text_as_recorded",
                          "headline_share", "sha256_16", "bytes"} for e in manifest)
