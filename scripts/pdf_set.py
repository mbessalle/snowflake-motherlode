"""Which PDFs of a project give terms of the deal? Not one PDF: a deal's terms can be split between the
agreement and a later completion notice, so each case gets a set of documents.

    python scripts/pdf_set.py ask                       # evals/pdf_set.jsonl: P(gives terms) per PDF
    python scripts/pdf_set.py prepare WORKTREE ...      # blind labelling of a sample, split over worktrees
    python scripts/pdf_set.py score LABELS.jsonl        # the model's yes/no against the labels, per PDF
    python scripts/pdf_set.py finalize LABELS.jsonl     # the set per case, used by build_pdf_cases.py

For every case whose project has several PDFs, the decision model answers one yes/no question per PDF: does
this announcement give terms (price, payments, shares, interest, royalty, commitments) of the deal in which
the buyer acquires the project around the news date? It reads the start of the PDF and up to two passages
that name the project, since a completion notice can state the terms further down.

The set is the PDFs answered yes, always with the PDF chosen as announcing the deal (choose_pdf.py) first.
As with every judgment here, it is measured against blind labels before it is used, and on the labelled cases
the labels decide.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_pdf_cases as builder  # noqa: E402

from motherlode import judge, telemetry  # noqa: E402

OUT = ROOT / "evals" / "pdf_set.jsonl"
LABELS = ROOT / "evals" / "pdf_set_labels.jsonl"
CHOICE = ROOT / "evals" / "pdf_choice.jsonl"
START, PASSAGE = 2000, 400


def text_of(file: str) -> str:
    return " ".join((builder.TEXT_CACHE / f"{Path(file).stem}.txt").read_text().split())


def excerpt(text: str, project: str) -> dict:
    """The start of the text and up to two passages further on that name the project."""
    words = [w for w in re.split(r"\W+", project) if len(w) > 2]
    pattern = r"\b" + r"\W+".join(map(re.escape, words)) + r"\b" if words else None
    passages = []
    if pattern:
        for m in re.finditer(pattern, text[START:], re.I):
            at = START + m.start()
            if all(abs(at - p) > 2 * PASSAGE for p in passages):
                passages.append(at)
            if len(passages) == 2:
                break
    return {"start": text[:START], "passages_naming_the_project": [text[max(0, p - PASSAGE):p + PASSAGE] for p in passages]}


def question(case: dict, file: str) -> tuple[dict, dict]:
    a = case["analyst"]
    state = {"project": case["project_name"], "buyer": a.get("owner_name"), "buyer_ticker": a.get("owner_ticker"),
             "news_date": str(a.get("news_date"))[:10], "announcement": excerpt(text_of(file), case["project_name"])}
    return state, {"q": judge.noul(
        "Does `announcement` give terms of the deal in which the company in `buyer` acquires, options, earns into or "
        "farms into the project in `project`, around `news_date`: the price, payments, shares, interest, royalty or "
        "spending commitments?",
        "Yes: it states some of this deal's terms, as the announcement of the deal, an amendment, or a completion "
        "notice that repeats or adds terms.",
        "No: it is about another deal or another project, or it only mentions this deal without giving its terms "
        "(a quarterly report, a presentation, exploration results).")}


def cases_with_several() -> list[tuple[dict, list[str]]]:
    rows = {r["row_id"]: r for r in map(json.loads, CHOICE.read_text().splitlines())}
    cases = [json.loads(line) for line in builder.CASES.read_text().splitlines()]
    return [(c, rows[c["row_id"]]["files"]) for c in cases if c["row_id"] in rows]


def ask() -> None:
    telemetry.setup("pdf_set")
    work = cases_with_several()
    before = judge.spent()

    def one(item):
        case, files = item
        ps = {}
        for f in files:
            state, q = question(case, f)
            ps[f] = (judge.ask(state, q, label=f"pdf_set:{case['row_id']}:{f}").get("q") or {}).get("noul")
        return {"row_id": case["row_id"], "case_id": case["id"], "primary": case["file"], "p_gives_terms": ps}

    with telemetry.span("pdf_set", input_cases=len(work)):
        with ThreadPoolExecutor(8) as pool:
            rows = list(pool.map(telemetry.carry(one), work))
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sizes = [sum(1 for f, p in r["p_gives_terms"].items() if (p or 0) >= 0.5 or f == r["primary"]) for r in rows]
    print(f"{len(rows)} cases, {sum(len(r['p_gives_terms']) for r in rows)} PDFs asked; set size: "
          f"1 for {sizes.count(1)}, 2 for {sizes.count(2)}, 3+ for {sum(s >= 3 for s in sizes)}; "
          f"spent {judge.spent() - before:.4f} USD")
    telemetry.shutdown()


def prepare(worktrees: list[Path], controls: int = 10) -> None:
    """Blind labelling: every case where the model puts more than one PDF in the set, or answers yes for a PDF
    other than the primary, plus single-PDF sets as controls. The labellers get the deal facts and the texts,
    never a method's answer or the primary pick."""
    rows = [json.loads(line) for line in OUT.read_text().splitlines()]
    cases = {c["row_id"]: c for c in map(json.loads, builder.CASES.read_text().splitlines())}
    multi = [r for r in rows if sum((p or 0) >= 0.5 for f, p in r["p_gives_terms"].items() if f != r["primary"])]
    single = [r for r in rows if r not in multi]
    random.Random(11).shuffle(single)
    picked = multi + single[:controls]
    random.Random(12).shuffle(picked)
    for i, worktree in enumerate(worktrees):
        folder = worktree / "label"
        (folder / "texts").mkdir(parents=True, exist_ok=True)
        lines = []
        for r in picked[i::len(worktrees)]:
            a = cases[r["row_id"]]["analyst"]
            files = sorted(r["p_gives_terms"])
            lines.append({"row_id": r["row_id"], "project": cases[r["row_id"]]["project_name"], "buyer": a.get("owner_name"),
                          "buyer_ticker": a.get("owner_ticker"), "news_date": str(a.get("news_date"))[:10], "files": files})
            for f in files:
                shutil.copy(builder.TEXT_CACHE / f"{Path(f).stem}.txt", folder / "texts" / f"{Path(f).stem}.txt")
        (folder / "deals.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        print(f"{worktree}: {len(lines)} deals")


def score(labels_path: Path) -> None:
    rows = {r["row_id"]: r for r in map(json.loads, OUT.read_text().splitlines())}
    labels = [json.loads(line) for line in labels_path.read_text().splitlines() if line.strip()]
    for threshold in (0.3, 0.5, 0.7):
        agree = total = yes_right = yes_total = 0
        for label in labels:
            truth = set(label.get("files_with_terms") or [])
            if label.get("unsure"):
                continue
            for f, p in rows[label["row_id"]]["p_gives_terms"].items():
                said = (p or 0) >= threshold
                total += 1
                agree += said == (f in truth)
                yes_total += said
                yes_right += said and f in truth
        print(f"threshold {threshold}: model agrees with the labels on {agree} of {total} PDFs; "
              f"of its yes answers, {yes_right} of {yes_total} right")
    sets = sum(1 for lab in labels if not lab.get("unsure"))
    exact = sum(1 for lab in labels if not lab.get("unsure") and
                {f for f, p in rows[lab["row_id"]]["p_gives_terms"].items() if (p or 0) >= 0.5} == set(lab.get("files_with_terms") or []))
    print(f"whole sets equal to the label at 0.5: {exact} of {sets}")


def finalize(labels_path: Path, threshold: float = 0.3) -> None:
    """The set per case: the labelled set where a blind label exists; otherwise the PDFs the model answers yes
    for at `threshold`. 0.3, measured on 212 labelled PDFs (16 Nov 2026): it agrees with the labels on 184 and
    its yes answers are right 155 times in 167, against 176 and 146 of 157 at 0.5. The primary PDF (the one
    announcing the deal) is always in the set and always first."""
    labels = {x["row_id"]: x for x in map(json.loads, labels_path.read_text().splitlines()) if x.get("row_id")}
    LABELS.write_text("".join(json.dumps(x) + "\n" for x in labels.values()))
    rows = [json.loads(line) for line in OUT.read_text().splitlines()]
    for r in rows:
        label = labels.get(r["row_id"])
        if label and not label.get("unsure"):
            chosen, r["set_by"] = set(label.get("files_with_terms") or []), "tier 3 (blind label)"
        else:
            chosen, r["set_by"] = {f for f, p in r["p_gives_terms"].items() if (p or 0) >= threshold}, "model"
        r["set"] = [r["primary"]] + sorted(chosen - {r["primary"]})
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} cases; sets with more than one PDF: {sum(len(r['set']) > 1 for r in rows)}; "
          f"by label {sum(r['set_by'].startswith('tier') for r in rows)}")


if __name__ == "__main__":
    command = sys.argv[1:2]
    if command == ["ask"]:
        ask()
    elif command == ["prepare"]:
        prepare([Path(p) for p in sys.argv[2:]])
    elif command == ["score"]:
        score(Path(sys.argv[2]))
    elif command == ["finalize"]:
        finalize(Path(sys.argv[2]))
    else:
        raise SystemExit(__doc__)
