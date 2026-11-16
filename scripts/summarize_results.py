"""Write evals/results_summary.json: the counts the README quotes, computed from the per-deal result files.

The per-deal files (the 236 cases, the 1,180 cell verdicts, the decision model's answers, the PDF choices and
the labels) hold values from a client's deal table, so they stay on the machine that made them and are not
published. This summary holds counts only. With the private files present,
`python scripts/summarize_results.py --check` regenerates it and fails if the committed copy differs
(tests/test_results_summary.py runs that check whenever the files are there).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
OUT = EVALS / "results_summary.json"
PRIVATE = ["pdf_cases", "cell_checks", "cell_review_pilot", "judge_results", "judge_labels", "pdf_choice",
           "pdf_labels", "pdf_set", "pdf_set_labels", "split_labels", "split_triage"]
SET_THRESHOLD = 0.3


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (EVALS / f"{name}.jsonl").read_text().splitlines() if line.strip()]


def private_files_present() -> bool:
    return all((EVALS / f"{n}.jsonl").exists() for n in PRIVATE)


def cases() -> dict:
    rows = load("pdf_cases")
    return {"cases": len(rows), "with_announcing_pdf": sum(r.get("announcement_found") is True for r in rows)}


def cells() -> dict:
    rows = load("cell_checks")
    return {"cells": len(rows), "cases": len({r["row_id"] for r in rows}),
            "verdicts": dict(sorted(Counter(r["verdict"] for r in rows).items())),
            "escalated": sum(bool(r.get("escalate")) for r in rows)}


def tier3_pilot() -> dict:
    rows = load("cell_review_pilot")
    decided = [r for r in rows if r.get("analyst_right") is not None]
    proposals = [r for r in rows if r.get("proposal_right") is not None]
    return {"cells": len(rows), "cases": len({r["case_id"] for r in rows}),
            "analyst_right": sum(r["analyst_right"] is True for r in decided),
            "analyst_wrong": sum(r["analyst_right"] is False for r in decided),
            "undecidable": len(rows) - len(decided),
            "proposals_judged": len(proposals), "proposals_right": sum(r["proposal_right"] is True for r in proposals)}


def pdf_choice() -> dict:
    rows = {r["row_id"]: r for r in load("pdf_choice")}
    labelled = [(rows[label["row_id"]], label.get("file")) for label in load("pdf_labels")
                if label["row_id"] in rows and label.get("file") != "unsure"]
    return {"deals_with_several_pdfs": len(rows),
            "decided_by": dict(sorted(Counter(r["final_by"] for r in rows.values()).items())),
            "labelled": len(labelled),
            "count_rule_right": sum(r["count_rule"] == truth for r, truth in labelled),
            "model_with_none_right": sum(r["model"] == truth for r, truth in labelled),
            "model_without_none_right": sum(r["model_no_none"] == truth for r, truth in labelled),
            "no_announcing_pdf": sum(truth is None for _, truth in labelled)}


def pdf_set() -> dict:
    rows = {r["row_id"]: r for r in load("pdf_set")}
    agree = n = 0
    for label in load("pdf_set_labels"):
        r = rows.get(label["row_id"])
        if r is None or label.get("unsure"):
            continue
        truth = set(label["files_with_terms"])
        for f, p in r["p_gives_terms"].items():
            n += 1
            agree += ((p >= SET_THRESHOLD) == (f in truth))
    return {"deals": len(rows), "using_several_pdfs": sum(len(r["set"]) > 1 for r in rows.values()),
            "threshold": SET_THRESHOLD, "pdfs_labelled": n, "model_agrees_with_label": agree}


def judge_calibration() -> list[str]:
    """The decision model against the blind labels only (the README's table), at its threshold of 0.5."""
    lines = []
    for source in ("blind labeller A", "blind labeller B"):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "judge_cases.py"), "calibrate",
                              "--source-filter", source], capture_output=True, text=True, check=True, cwd=ROOT,
                             env={**__import__("os").environ, "MOTHERLODE_TRACES": "0"}).stdout
        lines += [f"{source}: {line.strip()}" for line in out.splitlines()
                  if line.strip() and not line.startswith(("telemetry", "labels used"))
                  and ("labelled" in line or "threshold 0.5" in line or "no current question" in line)]
    return lines


def summary() -> dict:
    return {"note": "Counts only; the per-deal files are a client's data and are not published.",
            "cases": cases(), "cell_check": cells(), "tier3_pilot": tier3_pilot(), "pdf_choice": pdf_choice(),
            "pdf_set": pdf_set(), "judge_questions": len(load("judge_results")),
            "judge_labels": len(load("judge_labels")), "judge_calibration": judge_calibration()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed summary differs")
    args = parser.parse_args()
    if not private_files_present():
        sys.exit("the per-deal result files are not on this machine; nothing to summarize")
    text = json.dumps(summary(), indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if OUT.read_text() != text:
            sys.exit("evals/results_summary.json is out of date: run scripts/summarize_results.py")
        print("evals/results_summary.json matches the per-deal files")
        return
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
