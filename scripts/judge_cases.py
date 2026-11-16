"""Have a decision model judge what code cannot: the meaning of the evidence behind each case value.

    python scripts/judge_cases.py questions            # build the questions from the cases (no model, no cost)
    python scripts/judge_cases.py ask [--limit N]      # ask Jev; cached, capped at JUDGE_SPEND_CAP_USD
    python scripts/judge_cases.py calibrate            # compare the answers with hand labels

The case builder finds candidates with exact code: a number written in the PDF, a sum of written amounts, an
area in km2. Whether that evidence means what the field needs is a judgment, so four questions, one per kind,
go to motherlode.judge. Each one restates a rule of the original pipeline's extraction rules
(docs/extraction-rules/core-logic.md and edge-cases.md in the old repository):

  buyer_role     choice   Is the analyst's buyer the buyer in this announcement, the seller, another party,
                          or is there no deal over this project?  (entity resolution: identify the BUYER)
  area_scope     choice   Is this area this project's own, a total for several projects, or not its area?
                          (rule 1: the Bingara 100 km2 of a 150 km2 total)
  sum_parts      noul     Are all these amounts parts of the same kind of consideration for this project?
                          (aggregate all payment components, ignore legacy terms, fees are not payments)
  percent_role   noul     Is this percentage the interest acquired (or the royalty) in this deal?
                          (farm-in: final potential interest; NSR acquired or retained)

Files:
  data/judge_questions.jsonl   the questions with their text passages (git-ignored: document text)
  evals/judge_results.jsonl    the answers and probabilities, no text (committed)
  evals/judge_labels.jsonl     hand labels from the critic agents' reports, for calibration (committed)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_pdf_cases as builder  # noqa: E402

from evals.score_pdf import case_text, company_core, listings  # noqa: E402
from motherlode import judge, telemetry  # noqa: E402
from motherlode.validate import FIELD_KIND, find_number  # noqa: E402

QUESTIONS = ROOT / "data" / "judge_questions.jsonl"
RESULTS = ROOT / "evals" / "judge_results.jsonl"
LABELS = ROOT / "evals" / "judge_labels.jsonl"
FIELD_WORDS = {"cash_payments_value": "cash payment", "exp_comit_value": "exploration expenditure commitment",
               "shares_amount": "number of consideration shares", "nsr_percent": "royalty",
               "interest_acquired_percent": "interest acquired"}
HEAD = 2500          # characters of the start of the announcement given with every question: the deal summary
PASSAGE = 450        # characters on each side of a piece of evidence


def text_of(case: dict) -> str:
    """Every PDF of the case's set, the announcing one first (evals.score_pdf.case_text)."""
    return case_text(case, builder.TEXT_CACHE)


def around(text: str, snippet: str | None, width: int = PASSAGE) -> str | None:
    """The passage of `text` around `snippet` (a whitespace-normalised piece of it)."""
    if not snippet:
        return None
    at = text.find(snippet[:80])
    if at < 0:
        return snippet
    return text[max(0, at - width):at + len(snippet) + width]


def mentions(text: str, owner: str | None, ticker: str | None, limit: int = 3) -> list[str]:
    """Passages where the buyer's name or ticker code appears, beyond the head of the text."""
    patterns = []      # (pattern, flags): a name in any case, a ticker code in capitals only
    core = company_core(owner)
    if core:
        patterns.append((r"\b" + r"\W+".join(map(re.escape, core.split())) + r"\b", re.I))
    for _, code in listings(ticker) if ticker else []:
        if len(code) >= 3:
            patterns.append((r"\b" + re.escape(code) + r"\b", 0))
    found = []
    for pattern, flags in patterns:
        for m in re.finditer(pattern, text, flags):
            if m.start() > HEAD and all(abs(m.start() - f) > 2 * PASSAGE for f in found):
                found.append(m.start())
            if len(found) >= limit:
                break
    return [text[max(0, f - PASSAGE):f + PASSAGE] for f in sorted(found)]


def questions_for(case: dict) -> list[dict]:
    """Every question this case raises, each with its own state."""
    text, a, project = text_of(case), case["analyst"], case["project_name"]
    head = text[:HEAD]
    out = []
    owner = a.get("owner_name")
    if owner:
        out.append({"qid": f"{case['row_id']}:buyer_role", "row_id": case["row_id"], "case_id": case["id"],
                    "question": "buyer_role", "state": {
                        "project": project, "company": owner, "ticker": a.get("owner_ticker"),
                        "announcement_start": head, "other_passages_naming_the_company": mentions(text, owner, a.get("owner_ticker"))},
                    "ask": judge.choice(
                        "What role does the company named in `company` have in a deal over the project named in "
                        "`project`, according to this announcement?",
                        {"buyer": "It acquires, buys, options in, earns into or farms into the project, or receives an "
                                  "interest in it: it is the party gaining the project.",
                         "seller": "It sells, options out, farms out or transfers the project, or is paid for it: it is "
                                   "the party giving the project up.",
                         "other": "It is named, but the deal over this project is between other parties, or it is "
                                  "only an adviser, shareholder, neighbour or the operator.",
                         "no_deal": "The announcement does not describe a deal over this project, or does not name the "
                                    "company."})})
    area = builder.number(a.get("project_area"))
    rule = case["derived_in_document"].get("project_area")
    if area and (rule == "km2 to ha" or "project_area" in case["written_in_document"]):
        snippet = builder.km2_written(text, area / 100, PASSAGE) if rule else builder.evidence("project_area", area, text, PASSAGE)
        shown = f"{area / 100:g} km² ({area:g} ha)" if rule else f"{area:g} ha"
        out.append({"qid": f"{case['row_id']}:area_scope", "row_id": case["row_id"], "case_id": case["id"],
                    "question": "area_scope", "field": "project_area", "state": {
                        "project": project, "area": shown, "passage": snippet, "announcement_start": head},
                    "ask": judge.choice(
                        f"Is the area {shown} in `passage` the area of the project named in `project`?",
                        {"this_project": "Yes: it is the area of this project's tenements, licences or claims, on its own.",
                         "several_projects": "It is a total for this project together with other projects, or the area "
                                             "of a wider land package that includes other projects.",
                         "not_this_area": "It is not this project's area: another project's area, a distance, a strike "
                                          "length, a region, or another quantity."})})
    for name, how in case["derived_in_document"].items():
        if how != "sum":
            continue
        found = builder.derive(name, builder.number(a[name]), text)
        amounts = [float(x.replace(",", "")) for x in found[1].split(" + ")] if found else []
        parts = [{"amount": f"{x:,.0f}", "passage": around(text, find_number(text, x, kind=FIELD_KIND.get(name),
                                                                             exact_kind=True, width=250), 0)}
                 for x in amounts]
        what = FIELD_WORDS[name]
        # One question per amount as well: one judgment at a time works better than one about all amounts
        # at once (calibration against blind labels, 16 Nov 2026). The sum counts as right if every part is.
        for i, x in enumerate(amounts):
            snippet = find_number(text, x, kind=FIELD_KIND.get(name), exact_kind=True)
            out.append({"qid": f"{case['row_id']}:sum_part:{name}:{i}", "row_id": case["row_id"], "case_id": case["id"],
                        "question": "sum_part", "field": name, "parent": f"{case['row_id']}:sum_parts:{name}", "state": {
                            "project": project, "amount": f"{x:,.0f}", "passage": around(text, snippet, PASSAGE),
                            "announcement_start": head},
                        "ask": judge.noul(
                            f"Is the amount {x:,.0f} in `passage` part of the {what} for acquiring the project named in "
                            "`project` in this deal?",
                            f"Yes: it is an upfront, deferred, milestone or conditional part of the {what} for this "
                            "project in this deal.",
                            "No: it is something else, such as a fee, a capital raising or placement, a finder's or "
                            "adviser's payment, an exploration spend when the question is about cash, a price in "
                            "another deal or an earlier announcement, or an alternative to another amount.")})
        out.append({"qid": f"{case['row_id']}:sum_parts:{name}", "row_id": case["row_id"], "case_id": case["id"],
                    "question": "sum_parts", "field": name, "state": {
                        "project": project, "total": f"{builder.number(a[name]):,.0f}", "amounts": parts,
                        "announcement_start": head},
                    "ask": judge.noul(
                        f"Are all the listed amounts parts of the {what} for acquiring the project named in `project`, "
                        f"so that together they make the deal's total {what}?",
                        f"Each amount is part of the {what} for this project in this deal: an upfront, deferred, "
                        "milestone or conditional payment, or a tranche of the same commitment.",
                        "At least one amount is something else: a fee, a capital raising or placement, a finder's or "
                        "adviser's payment, a resource or other quantity, a price from another deal or an earlier "
                        "announcement, or an alternative to another listed amount.")})
    for name in ("interest_acquired_percent", "nsr_percent"):
        value = builder.number(a.get(name))
        if not value or name not in case["written_in_document"]:
            continue
        passage = around(text, builder.evidence(name, value, text), PASSAGE)
        if name == "interest_acquired_percent":
            ask = judge.noul(
                f"Does `passage` give {value:g}% as the interest in the project named in `project` that the buyer "
                "acquires, or can earn, in this deal?",
                f"Yes: {value:g}% is the interest, stake or ownership the buyer acquires, or the final interest it can "
                "earn under a farm-in, earn-in or option.",
                f"No: {value:g}% is something else, such as an interest held before the deal, the seller's remaining "
                "share, a grade, a share of samples or drill holes, a royalty, or a percentage of shares.")
        else:
            ask = judge.noul(
                f"Does `passage` give {value:g}% as a royalty that is part of the terms of this deal over the project "
                "named in `project`, acquired by the buyer or kept by the seller?",
                f"Yes: {value:g}% is a net smelter, gross revenue, net profit or similar royalty in this deal's terms.",
                f"No: {value:g}% is something else, such as a grade, a recovery, a royalty in another deal, or an "
                "interest.")
        out.append({"qid": f"{case['row_id']}:percent_role:{name}", "row_id": case["row_id"], "case_id": case["id"],
                    "question": "percent_role", "field": name,
                    "state": {"project": project, "value": f"{value:g}%", "passage": passage, "announcement_start": head},
                    "ask": ask})
    return out


def build_questions() -> list[dict]:
    cases = [json.loads(line) for line in builder.CASES.read_text().splitlines() if line.strip()]
    questions = [q for case in cases for q in questions_for(case)]
    QUESTIONS.parent.mkdir(parents=True, exist_ok=True)
    QUESTIONS.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions))
    return questions


def ask_all(questions: list[dict], workers: int = 8) -> list[dict]:
    def one(q: dict) -> dict:
        answers = judge.ask(q["state"], {"q": q["ask"]}, label=q["qid"])
        return {k: q[k] for k in ("qid", "row_id", "case_id", "question", "parent") if k in q} | \
               ({"field": q["field"]} if "field" in q else {}) | {"model": judge.MODEL, "answer": answers.get("q")}

    with telemetry.span("judge_cases", input_questions=len(questions), model=judge.MODEL) as s:
        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(telemetry.carry(one), questions))
        s.set(output_answers=len(results), spent_usd=judge.spent())
    RESULTS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    return results


def sum_each(results) -> dict[str, dict]:
    """A sum judged amount by amount: right only if every amount is, so P = the smallest P(yes) of its parts."""
    parts: dict[str, list[float]] = {}
    for r in results:
        if r["question"] == "sum_part" and r.get("answer"):
            parts.setdefault(r["parent"], []).append(r["answer"]["noul"])
    return {parent.replace(":sum_parts:", ":sum_each:"): {"qid": parent.replace(":sum_parts:", ":sum_each:"),
                                                          "question": "sum_each",
                                                          "answer": {"type": "noul", "noul": min(ps)}}
            for parent, ps in parts.items()}


def probability(answer: dict | None, question: str) -> float | None:
    """The probability that the evidence is right: P(buyer), P(this_project), or P(yes)."""
    if not answer:
        return None
    if answer.get("type") == "noul":
        return answer.get("noul")
    key = {"buyer_role": "buyer", "area_scope": "this_project"}[question]
    return (answer.get("probabilities") or {}).get(key)


def calibrate(source: str | None = None) -> None:
    """Agreement of the model with the hand labels, per question and threshold; `source` keeps one labeller."""
    results = {r["qid"]: r for r in map(json.loads, RESULTS.read_text().splitlines())}
    results.update(sum_each(results.values()))
    labels = [json.loads(line) for line in LABELS.read_text().splitlines() if line.strip()]
    labels = [label for label in labels if "right" in label and (source is None or label.get("source") == source)]
    # a sum's label also grades the per-amount way of asking it
    labels += [{**label, "qid": label["qid"].replace(":sum_parts:", ":sum_each:")} for label in labels
               if ":sum_parts:" in label["qid"]]
    print(f"labels used: {len(labels)}" + (f" from {source!r}" if source else " from every source"))
    by_question: dict[str, list[tuple[float, bool, str]]] = {}
    missing = []
    for label in labels:
        r = results.get(label["qid"])
        p = probability(r["answer"], r["question"]) if r else None
        if p is None:
            missing.append(label["qid"])
            continue
        by_question.setdefault(label["qid"].split(":")[1], []).append((p, label["right"], label["qid"]))
    for question, items in sorted(by_question.items()):
        right = sum(1 for _, ok, _ in items if ok)
        print(f"{question}: {len(items)} labelled ({right} right, {len(items) - right} wrong)")
        for threshold in (0.3, 0.5, 0.7, 0.9):
            agree = sum(1 for p, ok, _ in items if (p >= threshold) == ok)
            caught = sum(1 for p, ok, _ in items if not ok and p < threshold)
            lost = sum(1 for p, ok, _ in items if ok and p < threshold)
            print(f"  threshold {threshold}: agrees with the label {agree} of {len(items)}; "
                  f"wrong evidence caught {caught} of {len(items) - right}; right evidence rejected {lost} of {right}")
        for p, ok, qid in sorted(items):
            if (p >= 0.5) != ok:
                print(f"    disagrees: {qid} label {'right' if ok else 'wrong'}, model {p:.2f}")
    if missing:
        print(f"labels with no current question (the case changed since it was labelled): {len(missing)}: {missing}")


GOOD = {"buyer_role": "buyer", "area_scope": "this_project", "sum_parts": "yes", "percent_role": "yes",
        "sum_each": "yes"}


def import_labels(tsv: Path, source: str) -> int:
    """Add a blind labeller's labels (lines `qid<TAB>label<TAB>reason`) to LABELS, replacing earlier labels of
    the same qid from the same source. `unsure` labels are kept but not used for calibration."""
    old = [json.loads(line) for line in LABELS.read_text().splitlines() if line.strip()] if LABELS.exists() else []
    new = []
    for line in tsv.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or ":" not in parts[0]:
            continue
        qid, label = parts[0].strip(), parts[1].strip()
        question = qid.split(":")[1]
        entry = {"qid": qid, "label": label, "source": source, "reason": parts[2].strip() if len(parts) > 2 else ""}
        if label != "unsure":
            entry["right"] = label == GOOD[question]
        new.append(entry)
    keys = {(e["qid"], e["source"]) for e in new}
    kept = [e for e in old if (e["qid"], e.get("source")) not in keys]
    LABELS.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept + new))
    return len(new)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("questions", "ask", "calibrate", "import-labels"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--tsv", type=Path, help="import-labels: the labeller's file")
    parser.add_argument("--source", help="import-labels: who labelled, e.g. 'blind labeller A'")
    parser.add_argument("--source-filter", help="calibrate: only labels from this source")
    args = parser.parse_args()
    print("telemetry:", telemetry.setup("judge_cases"))
    if args.command == "questions":
        questions = build_questions()
        kinds: dict[str, int] = {}
        for q in questions:
            kinds[q["question"]] = kinds.get(q["question"], 0) + 1
        chars = sum(len(json.dumps(q["state"])) + len(json.dumps(q["ask"])) for q in questions)
        print(f"{len(questions)} questions: {kinds}; about {chars / 4:,.0f} input tokens, "
              f"about {chars / 4 * judge.USD_PER_INPUT_TOKEN:.3f} USD at {judge.MODEL}'s price")
    elif args.command == "ask":
        questions = [json.loads(line) for line in QUESTIONS.read_text().splitlines()]
        questions = questions[:args.limit] if args.limit else questions
        print(f"asking {len(questions)}; ledger before: {judge.spent():.4f} USD, cap {judge.cap():.2f} USD")
        results = ask_all(questions)
        print(f"answered {sum(1 for r in results if r['answer'])} of {len(results)}; ledger after: {judge.spent():.4f} USD")
    elif args.command == "import-labels":
        print(f"imported {import_labels(args.tsv, args.source)} labels from {args.tsv.name} as {args.source!r}")
    else:
        calibrate(args.source_filter)
    telemetry.shutdown()


if __name__ == "__main__":
    main()
