"""Triage for a review of the split check: every split candidate in the data, with the decision model's answer.

    python scripts/triage_splits.py OUT.jsonl

Runs validate.resolve_splits with judge.judge_split over the original pipeline's 33 saved extractions
(first attempts) and over the analyst's values on the 236 source-PDF cases, and writes one line per candidate:
where it comes from, the field, the value, the total, n, the probability and the resulting status. A critic
then reads these and a few unflagged values, not every document. Cached answers cost nothing; new ones cost
about 0.00006 USD each (motherlode.judge).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from motherlode import judge, telemetry  # noqa: E402
from motherlode.validate import resolve_splits  # noqa: E402


def candidates(source: str, case_id: str, project: str | None, extraction: dict, text: str) -> list[dict]:
    out = []

    def ask(p, field, value, candidate):
        probability = judge.judge_split(p, field, value, candidate)
        out.append({"source": source, "case_id": case_id, "project": p, "field": field, "value": value,
                    "total": candidate.total, "n": candidate.n, "p_shared": probability})
        return probability

    report = resolve_splits(extraction, text, project, ask)
    for row in out:
        row["status"] = report.fields[row["field"]].status
    return out


def main() -> None:
    out_path = Path(sys.argv[1])
    telemetry.setup("triage_splits")
    rows = []
    import test_score  # the original pipeline's cache location

    cache = test_score.ORIGINAL_CACHE
    for case in map(json.loads, (ROOT / "evals" / "cases.jsonl").read_text().splitlines()):
        reply = cache / "extractions" / "gpt-6.1-terra" / "attempt1" / f"{case['id']}.json"
        text_file = cache / "texts" / f"{case['id']}.txt"
        if reply.exists() and text_file.exists():
            rows += candidates("original 33, model reply", case["id"], case.get("project"), json.loads(reply.read_text()),
                               " ".join(text_file.read_text().split()))
    for case in map(json.loads, (ROOT / "evals" / "pdf_cases.jsonl").read_text().splitlines()):
        text = " ".join((ROOT / "data" / "source_text" / f"{Path(case['file']).stem}.txt").read_text().split())
        rows += candidates("236 cases, analyst value", case["id"], case["project_name"], case["analyst"], text)
    out_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    flagged = [r for r in rows if r["p_shared"] is not None and 0.2 <= r["p_shared"] < 0.8]
    print(f"{len(rows)} split candidates; accepted {sum(r['status'] == 'DERIVED' for r in rows)}; "
          f"unsure (0.2-0.8) {len(flagged)}; ledger {judge.spent():.4f} USD")
    telemetry.shutdown()


if __name__ == "__main__":
    main()
