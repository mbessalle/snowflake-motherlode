"""Which PDF of a project announces the deal? Asked of the decision model, for every case with several PDFs.

    python scripts/choose_pdf.py ask        # evals/pdf_choice.jsonl: the model's pick and the count rule's pick
    python scripts/choose_pdf.py score LABELS.jsonl     # both against hand-picked choices

The case builder used to choose by counting: the PDF with the most analyst values written in it, then the one
that gives the news date most often, then the longest. That is a stand-in for a question of meaning (is this
announcement about this buyer acquiring this project?), and it fails when the analyst's values are wrong or
missing (9 cases have none in any PDF) or when another announcement repeats the numbers. Here the model reads
the start of every PDF of the project and picks one, or none. The count rule's pick is kept beside it as a
hint and for comparison; scripts/build_pdf_cases.py uses the model's pick when this file has one.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_pdf_cases as builder  # noqa: E402

from motherlode import judge, telemetry  # noqa: E402

OUT = ROOT / "evals" / "pdf_choice.jsonl"
START = 1500        # characters of each PDF the model reads: the headline, parties and terms are at the start


def question(case: dict, files: list[str]) -> tuple[dict, dict]:
    a = case["analyst"]
    texts = {f"p{i}": " ".join((builder.TEXT_CACHE / f"{Path(f).stem}.txt").read_text().split())[:START]
             for i, f in enumerate(files)}
    state = {"project": case["project_name"], "buyer": a.get("owner_name"), "buyer_ticker": a.get("owner_ticker"),
             "news_date": str(a.get("news_date"))[:10], "announcements": texts}
    options = {key: f"The announcement {key} ({Path(files[int(key[1:])]).name}) announces this deal."
               for key in texts}
    options["none"] = "None of these announcements is about this deal."
    return state, {"q": judge.choice(
        "Which announcement in `announcements` announces the deal in which the company in `buyer` acquires, "
        "options, earns into or farms into the project in `project`, around `news_date`? A later notice that the "
        "deal completed counts if it gives the terms; a report that only mentions the deal does not.", options)}


def ask_case(case: dict, files: list[str]) -> dict:
    state, q = question(case, files)
    answer = judge.ask(state, q, label=f"pdf_choice:{case['row_id']}").get("q") or {}
    pick = answer.get("choice")
    ps = sorted((answer.get("probabilities") or {}).values(), reverse=True) + [0, 0]
    return {"row_id": case["row_id"], "case_id": case["id"], "files": files, "count_rule": case["file"],
            "model": None if pick in (None, "none") else files[int(pick[1:])], "model_none": pick == "none",
            "p": (answer.get("probabilities") or {}).get(pick), "margin": round(ps[0] - ps[1], 3)}


def ask() -> None:
    telemetry.setup("choose_pdf")
    cases = [json.loads(line) for line in builder.CASES.read_text().splitlines()]
    inventory = [json.loads(line) for line in builder.INVENTORY.read_text().splitlines()]
    work = []
    for case in cases:
        files = [e["file"] for e in inventory if e["project"] == case["project"] and "duplicate_of" not in e
                 and e["kind"] != "unreadable"]
        if len(files) > 1:
            work.append((case, files))
    before = judge.spent()
    with telemetry.span("choose_pdf", input_cases=len(work)):
        with ThreadPoolExecutor(8) as pool:
            rows = list(pool.map(telemetry.carry(lambda w: ask_case(*w)), work))
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    same = sum(r["model"] == r["count_rule"] for r in rows)
    print(f"{len(rows)} cases with several PDFs: model and count rule agree on {same}; model says none for "
          f"{sum(r['model_none'] for r in rows)}; spent {judge.spent() - before:.4f} USD")
    telemetry.shutdown()


def score(labels_path: Path) -> None:
    rows = {r["row_id"]: r for r in map(json.loads, OUT.read_text().splitlines())}
    labels = [json.loads(line) for line in labels_path.read_text().splitlines() if line.strip()]
    model = count = n = 0
    for label in labels:
        r = rows.get(label["row_id"])
        if r is None or label.get("file") == "unsure":
            continue
        n += 1
        truth = label.get("file")              # a file name, or None for "no PDF announces this deal"
        model += (r["model"] == truth)
        count += (r["count_rule"] == truth)
    print(f"{n} hand-picked cases: model right {model}, count rule right {count}")


def prepare(worktrees: list[Path], controls: int = 8) -> None:
    """Blind labelling: every case where the model and the count rule disagree, plus a few agreements as
    controls, split over the worktrees. Each gets only the deal facts and the PDF texts, never a pick."""
    import random
    import shutil

    rows = [json.loads(line) for line in OUT.read_text().splitlines()]
    cases = {c["row_id"]: c for c in map(json.loads, builder.CASES.read_text().splitlines())}
    disagree = [r for r in rows if r["model"] != r["count_rule"]]
    agree = [r for r in rows if r["model"] == r["count_rule"]]
    random.Random(7).shuffle(agree)
    picked = disagree + agree[:controls]
    random.Random(8).shuffle(picked)
    for i, worktree in enumerate(worktrees):
        mine = picked[i::len(worktrees)]
        folder = worktree / "label"
        (folder / "texts").mkdir(parents=True, exist_ok=True)
        lines = []
        for r in mine:
            a = cases[r["row_id"]]["analyst"]
            lines.append({"row_id": r["row_id"], "project": cases[r["row_id"]]["project_name"], "buyer": a.get("owner_name"),
                          "buyer_ticker": a.get("owner_ticker"), "news_date": str(a.get("news_date"))[:10],
                          "files": r["files"]})
            for f in r["files"]:
                shutil.copy(builder.TEXT_CACHE / f"{Path(f).stem}.txt", folder / "texts" / f"{Path(f).stem}.txt")
        (folder / "deals.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        print(f"{worktree}: {len(lines)} deals")


LABELS = ROOT / "evals" / "pdf_labels.jsonl"


def finalize(labels_path: Path) -> None:
    """The final pick per case, in three tiers like the cell check:
      - the model is asked without a 'none' option (41 of 66 blind labels right, against 34 with it and 25 for
        the count rule; its 'none' answers were right only 4 times in 19);
      - where the model and the count rule agree, that pick stands (right in 7 of 8 controls);
      - where they disagree, the case went to tier 3, the blind labellers (two claude -p sessions in their own
        worktrees, audited), and their pick is used; a label of null means no PDF of the project announces this
        deal, and the case is flagged instead (`announcement_found` false)."""
    labels = {x["row_id"]: x for x in map(json.loads, labels_path.read_text().splitlines()) if x.get("row_id")}
    LABELS.write_text("".join(json.dumps(x) + "\n" for x in labels.values()))
    cases = {c["row_id"]: c for c in map(json.loads, builder.CASES.read_text().splitlines())}
    rows = [json.loads(line) for line in OUT.read_text().splitlines()]
    for r in rows:
        state, q = question(cases[r["row_id"]], r["files"])
        q["q"]["criteria"].pop("none")
        answer = judge.ask(state, q, label=f"pdf_choice_no_none:{r['row_id']}").get("q") or {}
        r["model_no_none"] = r["files"][int(answer["choice"][1:])] if answer.get("choice") else None
        label = labels.get(r["row_id"], {}).get("file", "missing")
        if label not in ("missing", "unsure"):
            r["final"], r["final_by"] = label, "tier 3 (blind label)"
        elif r["model_no_none"] == r["count_rule"]:
            r["final"], r["final_by"] = r["count_rule"], "model and count rule agree"
        else:
            r["final"], r["final_by"] = r["model_no_none"], "model"
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    by: dict[str, int] = {}
    for r in rows:
        by[r["final_by"]] = by.get(r["final_by"], 0) + 1
    changed = sum(r["final"] != r["count_rule"] for r in rows)
    print(f"{len(rows)} cases: {by}; the PDF changes for {changed}; no announcing PDF: "
          f"{sum(r['final'] is None for r in rows)}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["ask"]:
        ask()
    elif sys.argv[1:2] == ["finalize"]:
        finalize(Path(sys.argv[2]))
    elif sys.argv[1:2] == ["prepare"]:
        prepare([Path(p) for p in sys.argv[2:]])
    elif sys.argv[1:2] == ["score"]:
        score(Path(sys.argv[2]))
    else:
        raise SystemExit(__doc__)
