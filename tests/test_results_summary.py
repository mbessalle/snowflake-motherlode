"""The committed counts (evals/results_summary.json) must equal what the per-deal result files give.

The per-deal files are a client's data and are not published, so in CI only the summary's shape is checked;
on the machine that holds them, the summary is regenerated and compared byte for byte."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import summarize_results as s  # noqa: E402

SUMMARY = json.loads((ROOT / "evals" / "results_summary.json").read_text())


def test_summary_holds_counts_only():
    assert set(SUMMARY) == {"note", "cases", "cell_check", "tier3_pilot", "pdf_choice", "pdf_set",
                            "judge_questions", "judge_labels", "judge_calibration"}
    cells = SUMMARY["cell_check"]
    assert sum(cells["verdicts"].values()) == cells["cells"]
    pilot = SUMMARY["tier3_pilot"]
    assert pilot["analyst_right"] + pilot["analyst_wrong"] + pilot["undecidable"] == pilot["cells"]
    assert sum(SUMMARY["pdf_choice"]["decided_by"].values()) == SUMMARY["pdf_choice"]["deals_with_several_pdfs"]
    assert "/home/" not in json.dumps(SUMMARY)


def test_readme_quotes_the_summary():
    readme = (ROOT / "README.md").read_text()
    cells = SUMMARY["cell_check"]
    assert f"{cells['cells']:,} values checked" in readme
    for verdict, label in [("confirmed", "Confirmed in the document"), ("contradicted", "Contradicted, with a proposed value")]:
        assert f"| {label} | {cells['verdicts'][verdict]} |" in readme
    choice = SUMMARY["pdf_choice"]
    assert f"| Count rule | {choice['count_rule_right']} of {choice['labelled']} |" in readme
    assert f"on {SUMMARY['pdf_set']['pdfs_labelled']} PDFs" in readme


def test_readme_example_table_matches_the_sample_rows():
    readme = (ROOT / "README.md").read_text()
    for line in (ROOT / "evals" / "sample_rows.jsonl").read_text().splitlines():
        r = json.loads(line)
        analyst = f"{r['analyst_value']:,.2f}" if r["field"] == "share_price" else f"{r['analyst_value']:,.0f}"
        tier3 = f"{r['tier3_value']:,.2f}" if r["field"] == "share_price" else f"{r['tier3_value']:,.0f}"
        row = next((l for l in readme.splitlines() if l.startswith(f"| {r['project']} |")), None)
        assert row, r["project"]
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[2] == analyst and cells[3] == r["tier2_verdict"] and cells[4] == r["tier2_proposal"], row
        assert cells[5].startswith(f"{tier3}:"), row
        assert ("analyst right" in cells[5]) == r["analyst_right"], row
        assert ("proposal right" in cells[5]) == bool(r["tier2_proposal_right"]), row


@pytest.mark.skipif(not s.private_files_present(), reason="per-deal result files absent (client data, not published)")
@pytest.mark.parametrize("script", ["summarize_results.py", "sample_rows.py"])
def test_published_files_match_the_per_deal_files(script):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--check"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
