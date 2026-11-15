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
    """Same cases, same saved replies, same validator: the numbers in the original README must come out."""
    summary = score.score(score.load_cases(), ORIGINAL_CACHE / "texts", ORIGINAL_CACHE / "extractions" / "gpt-6.1-terra", retry=True)
    assert summary["records"] == 33 and summary["skipped"] == []
    assert (summary["first"]["grounded"], summary["first"]["checked"], summary["first"]["records_passed"]) == (122, 126, 30)
    assert (summary["final"]["grounded"], summary["final"]["checked"], summary["final"]["records_passed"]) == (123, 126, 31)
    assert (summary["retried"], summary["retried_now_passing"]) == (3, 1)
    assert summary["agreement_total"] == {"both": 108, "agree": 83}
