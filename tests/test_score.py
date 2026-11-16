"""The scorer, on a small synthetic case in CI and on the original pipeline's saved replies when they are present."""
import json
import os
from pathlib import Path

import pytest

from evals import score

ORIGINAL_CACHE = Path(os.environ.get(
    "MOTHERLODE_ORIGINAL_CACHE", Path.home() / "github-mbessalle" / "motherlode-data-pipeline" / "evals" / ".cache"))

TEXT = "The Company will pay $250,000 in cash and the vendor retains a 2% NSR royalty over the 4,857 hectare property."
CASE = {"id": "1", "project": "Acme", "analyst": {"cash_payments_value": 250000, "nsr_percent": 2, "project_area": 5000}}


def write(directory: Path, name: str, content) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content if isinstance(content, str) else json.dumps(content))


def test_counts_grounding_retry_and_analyst_agreement(tmp_path):
    write(tmp_path / "texts", "1.txt", TEXT)
    write(tmp_path / "replies" / "attempt1", "1.json", {"cash_payments_value": 250000, "nsr_percent": 3, "project_area": 4857})
    write(tmp_path / "replies" / "attempt2", "1.json", {"cash_payments_value": 250000, "nsr_percent": 2, "project_area": 4857})

    first_only = score.score([CASE], tmp_path / "texts", tmp_path / "replies")
    assert (first_only["first"]["grounded"], first_only["first"]["checked"], first_only["first"]["records_passed"]) == (2, 3, 0)
    assert first_only["final"] is None and first_only["retried"] == 0

    summary = score.score([CASE], tmp_path / "texts", tmp_path / "replies", retry=True)
    assert (summary["final"]["grounded"], summary["final"]["checked"], summary["final"]["records_passed"]) == (3, 3, 1)
    assert (summary["retried"], summary["retried_now_passing"]) == (1, 1)
    # cash and NSR agree with the analyst; the area (4,857 against 5,000) does not
    assert summary["agreement_total"] == {"both": 3, "agree": 2}
    assert "**3 of 3 (100%)**" in score.markdown(summary, "synthetic")


def test_cases_without_text_or_extraction_are_reported_not_counted(tmp_path):
    write(tmp_path / "texts", "1.txt", TEXT)
    summary = score.score([CASE, {**CASE, "id": "2"}], tmp_path / "texts", tmp_path / "replies")
    assert summary["records"] == 0 and summary["skipped"] == ["1", "2"]


@pytest.mark.skipif(not (ORIGINAL_CACHE / "extractions" / "gpt-6.1-terra" / "attempt1").is_dir(),
                    reason="the original pipeline's eval cache is not on this machine")
def test_reproduces_the_original_pipelines_published_numbers():
    """Same cases, same saved replies: the original README published 122 / 126 (first attempt, 30 records
    passed) and 123 / 126 after the retry (31). This test scores without a judge, so no total shared between
    projects counts (the split critic of 16 Nov 2026 found the old word rule wrong on 5 of the 9 splits it
    accepted); with the decision model judging the splits the numbers are higher (scripts/triage_splits.py).
    The shares value stated in the news (shares_value_scraped) is now checked too: 3 more values, all found.

    The splits among these replies, as the split critic read them in the documents:
      83978 shares 4,731,835 = 14,195,506 / 3: shared by Murphy Lake North, Darby and Packrat (an earlier note
            here called it wrong; the critic read the document, and it is right; the analyst keeps the total)
      39689 cash 500,000 = 1,500,000 / 3 and 65011 shares 500,000 = 1,500,000 / 3: shared by three projects
      35924 exploration 200,000 = 1,000,000 / 5: shared by five projects; the analyst agrees
      75845 cash 1,400,000 = 7,000,000 / 5: not shared; the 7,000,000 is an expenditure, not cash"""
    summary = score.score(score.load_cases(), ORIGINAL_CACHE / "texts", ORIGINAL_CACHE / "extractions" / "gpt-6.1-terra", retry=True)
    assert summary["records"] == 33 and summary["skipped"] == []
    assert (summary["first"]["grounded"], summary["first"]["checked"], summary["first"]["records_passed"]) == (116, 129, 26)
    assert (summary["final"]["grounded"], summary["final"]["checked"], summary["final"]["records_passed"]) == (117, 129, 27)
    assert summary["first"]["fields"]["shares_value_scraped"]["FOUND"] == 3
    assert (summary["retried"], summary["retried_now_passing"]) == (3, 1)
    assert summary["agreement_total"] == {"both": 108, "agree": 83}
