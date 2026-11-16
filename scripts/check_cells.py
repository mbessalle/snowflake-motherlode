"""Check every value of the analyst's table against the source documents, cell by cell, in three tiers.

    python scripts/check_cells.py [--limit N] [--out evals/cell_checks.jsonl]

Tier 1, code (free, exact): every place the cell's value is written, in every distinct PDF of the project, with
the validator's number matching (units, trailing zeros, money or share kind). Calculated cells are checked by
their formula: aggregate = cash + exploration commitment + shares value; shares value = shares x price or a
value stated in the news.

Tier 2, the decision model (motherlode.judge, about 0.00006 USD a question):
  confirm   for each place found: does this passage give the value as this field of this deal?
  propose   when no place is confirmed: code lists the amounts of the field's kind in the case PDF, and the
            model picks which one is the field, or none. A pick that differs from the analyst's value is a
            proposed correction.

Verdicts per cell: confirmed, calculated (formula holds on confirmed parts), contradicted (the model picks
another value from the document), not_in_document, unsure (a probability between 0.3 and 0.7, or a choice won
by a small margin). Contradicted and unsure cells are escalated: scripts/escalate_cells.py hands them to a
`claude -p` session in its own worktree that reads the documents in full (tier 3).

Output: one line per cell, no document text (ids, values, verdicts, probabilities, proposals).
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

from motherlode import judge, telemetry  # noqa: E402
from motherlode.fields import all_fields  # noqa: E402
from motherlode.validate import MONEY, SHARES, find_number, written_amounts  # noqa: E402

OUT = ROOT / "evals" / "cell_checks.jsonl"
HEAD, PASSAGE, PROPOSE_MAX = 1500, 400, 12
# Accepted at 0.5, the cut-off that agreed best with 147 blind hand labels; unsure only close to it.
UNSURE = (0.4, 0.6)
DESCRIPTION = {f.name: f.description for f in all_fields()}
# The cells checked against the text, and the kind of amount each one is.
KINDS = {"cash_payments_value": MONEY, "exp_comit_value": MONEY, "shares_value": MONEY, "shares_value_scraped": MONEY,
         "shares_amount": SHARES, "nsr_percent": "percent", "interest_acquired_percent": "percent",
         "project_area": "area", "share_price": "price", "drilling_amount": "metres"}
PRICE_NEAR = re.compile(r"per share|issue price|deemed|price of|at \$|at a price|VWAP|issued at", re.I)


def documents() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for entry in map(json.loads, builder.INVENTORY.read_text().splitlines()):
        if "duplicate_of" not in entry and entry["kind"] != "unreadable":
            out.setdefault(entry["project"], []).append(entry)
    return out


def text_of(file: str) -> str:
    path = builder.TEXT_CACHE / f"{Path(file).stem}.txt"
    return " ".join(path.read_text().split()) if path.exists() else ""


def occurrences(field: str, value: float, text: str) -> list[str]:
    """Passages (about 800 characters) where the value is written as this field's kind of number."""
    if field == "share_price":
        snippet = find_number(text, value, near=PRICE_NEAR, width=PASSAGE)
    elif field == "project_area":
        snippet = builder.evidence("project_area", value, text, PASSAGE) or builder.km2_written(text, value / 100, PASSAGE)
    elif field == "drilling_amount":
        snippet = find_number(text, value, width=PASSAGE)
    else:
        snippet = builder.evidence(field, value, text, PASSAGE)
    return [snippet] if snippet else []


def shown(field: str, value: float) -> str:
    """The value as the question shows it: a percentage with %, an area in both units (the PDF often has km2)."""
    if KINDS[field] == "percent":
        return f"{value:g}%"
    if KINDS[field] == "area":
        return f"{value:,.10g} ha ({value / 100:,.10g} km²)"
    return f"{value:,.10g}"


def confirm_question(project: str, field: str, value: float, passage: str, head: str, part_of: float | None = None):
    """Does the passage give the value as this field? With `part_of`, the value is one part (a tranche) of a
    total of part_of: is it part of this field?"""
    shown_value = shown(field, value)
    if part_of:
        state = {"project": project, "field": field, "field_means": DESCRIPTION.get(field, field),
                 "amount": shown_value, "deal_total": f"{part_of:,.10g}", "passage": passage, "announcement_start": head}
        return state, {"q": judge.noul(
            f"Is the amount {shown_value} in `passage` part of this deal's {field} (see `field_means`) for the project "
            "named in `project`: an upfront, deferred, milestone or conditional part of it?",
            f"Yes: {shown_value} is one part of this deal's {field} for this project.",
            "No: it is something else, such as a fee, a capital raising or placement, a finder's or adviser's payment, "
            "an exploration spend when the field is cash, another deal's price, or an alternative to another amount.")}
    return _confirm(project, field, shown_value, passage, head)


def _confirm(project: str, field: str, shown: str, passage: str, head: str) -> tuple[dict, dict]:
    state = {"project": project, "field": field, "field_means": DESCRIPTION.get(field, field), "value": shown,
             "passage": passage, "announcement_start": head}
    return state, {"q": judge.noul(
        f"Does `passage` give {shown} as the {field} (see `field_means`) of this deal over the project named in "
        "`project`?",
        f"Yes: {shown} is this deal's {field} for this project, written in the passage.",
        f"No: {shown} is written but means something else, such as another payment, another deal, another project, "
        "a total of several projects, or a quantity of another kind.")}


def candidates_of_kind(field: str, text: str) -> list[float]:
    """The distinct amounts of the field's kind written in the text, largest first, for the model to pick from."""
    kind = KINDS[field]
    if kind in (MONEY, SHARES):
        values = written_amounts(text, only=kind)
    elif kind == "percent":
        values = [float(m) for m in re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%", text)]
    elif kind == "price":
        values = [float(m) for m in re.findall(r"\$\s?(\d*\.\d+)\s*(?:per share|each)", text, re.I)]
    else:
        return []
    return sorted(set(values), reverse=True)[:PROPOSE_MAX]


def propose_question(project: str, field: str, values: list[float], text: str, head: str) -> tuple[dict, dict] | None:
    if not values:
        return None
    options, places = {"none": "None of these amounts is this deal's value for the field."}, {}
    for i, v in enumerate(values):
        options[f"v{i}"] = shown(field, v)
        place = find_number(text, v, percent=KINDS[field] == "percent", width=150)
        if place:
            places[f"v{i}"] = place
    state = {"project": project, "field": field, "field_means": DESCRIPTION.get(field, field),
             "amounts_in_context": places, "announcement_start": head}
    return state, {"q": judge.choice(
        f"Which amount is this deal's {field} (see `field_means`) for the project named in `project`? "
        "`amounts_in_context` shows where each one is written.", options)}


def sum_parts(case: dict, field: str, value: float, text: str, head: str) -> float | None:
    """When the value is a sum of two or three amounts of its kind written in the case PDF (the builder's sum
    rule), the smallest probability that each amount is part of this field; None when it is no such sum."""
    if KINDS[field] not in (MONEY, SHARES):
        return None
    found = builder.derive(field, value, text)
    if not found or found[0] != "sum":
        return None
    ps = []
    for amount in (float(x.replace(",", "")) for x in found[1].split(" + ")):
        passage = find_number(text, amount, kind=KINDS[field], exact_kind=True, width=PASSAGE)
        state, q = confirm_question(case["project_name"], field, amount, passage or "", head, part_of=value)
        ps.append((judge.ask(state, q, label=f"cell:{case['row_id']}:{field}:part").get("q") or {}).get("noul") or 0.0)
    return min(ps) if ps else None


def margin(answer: dict) -> float:
    ps = sorted((answer.get("probabilities") or {}).values(), reverse=True) + [0, 0]
    return ps[0] - ps[1]


def check_cell(case: dict, field: str, texts: dict[str, str]) -> dict:
    a, project = case["analyst"], case["project_name"]
    value = builder.number(a.get(field))
    cell = {"row_id": case["row_id"], "case_id": case["id"], "field": field, "value": value}
    chosen = texts.get(case["file"], "")
    head = chosen[:HEAD]
    found = [(file, p) for file, text in texts.items() for p in occurrences(field, value, text)]
    cell["found_in"] = sorted({f for f, _ in found})
    best = None
    for file, passage in found[:3]:
        state, q = confirm_question(project, field, value, passage, texts[file][:HEAD])
        p = (judge.ask(state, q, label=f"cell:{case['row_id']}:{field}:confirm").get("q") or {}).get("noul")
        best = p if best is None or (p is not None and p > best) else best
    if best is None or best < UNSURE[0]:
        sum_p = sum_parts(case, field, value, chosen, head)        # a sum of tranches, judged amount by amount
        if sum_p is not None:
            cell["sum_parts_min_p"] = sum_p
            best = sum_p if best is None else max(best, sum_p)
            cell["derived"] = "sum"
    cell["p_confirmed"] = best
    if best is not None and best >= UNSURE[1]:
        cell["verdict"] = "confirmed"
    elif best is not None and best >= UNSURE[0]:
        cell["verdict"] = "unsure"
    else:
        pq = propose_question(project, field, candidates_of_kind(field, chosen), chosen, head)
        if pq is None:
            cell["verdict"] = "not_in_document"
        else:
            answer = judge.ask(*pq, label=f"cell:{case['row_id']}:{field}:propose").get("q") or {}
            pick = answer.get("choice")
            options = pq[1]["q"]["criteria"]
            cell["proposal"] = None if pick in (None, "none") else options[pick]
            cell["p_proposal"] = (answer.get("probabilities") or {}).get(pick)
            close = margin(answer) < 0.2
            if pick in (None, "none"):
                cell["verdict"] = "unsure" if close else "not_in_document"
            else:
                cell["verdict"] = "unsure" if close else "contradicted"
    cell["escalate"] = cell["verdict"] in ("contradicted", "unsure")
    return cell


def formula_cells(case: dict, checked: dict[str, dict]) -> list[dict]:
    """The calculated cells, checked by code from the checked parts."""
    a, out = case["analyst"], []
    n = builder.number
    ok = {f for f, c in checked.items() if c["verdict"] == "confirmed"}
    agg = n(a.get("aggregate_deal_value"))
    if agg:
        parts = {f: n(a.get(f)) for f in ("cash_payments_value", "exp_comit_value", "shares_value") if n(a.get(f))}
        holds = bool(parts) and abs(sum(parts.values()) - agg) <= max(1.0, 0.01 * agg)
        verdict = "calculated" if holds and set(parts) <= ok else "formula_holds_parts_unconfirmed" if holds else "formula_fails"
        out.append({"row_id": case["row_id"], "case_id": case["id"], "field": "aggregate_deal_value", "value": agg,
                    "verdict": verdict, "parts": parts, "escalate": verdict == "formula_fails"})
    sv, amount, price = n(a.get("shares_value")), n(a.get("shares_amount")), n(a.get("share_price"))
    if sv and amount and price and "shares_value" not in ok:
        holds = abs(amount * price - sv) <= max(1.0, 0.02 * sv) or builder.rounds_to(sv / amount, price)
        verdict = "calculated" if holds and {"shares_amount", "share_price"} <= ok else \
                  "formula_holds_parts_unconfirmed" if holds else "formula_fails"
        out.append({"row_id": case["row_id"], "case_id": case["id"], "field": "shares_value (shares x price)",
                    "value": sv, "verdict": verdict, "escalate": False})
    return out


def check_case(case: dict, docs: dict[str, list[dict]]) -> list[dict]:
    with telemetry.span("check_case", case_id=case["id"]) as s:
        texts = {e["file"]: text_of(e["file"]) for e in docs.get(case["project"], [])}
        cells = {}
        for field in KINDS:
            if builder.number(case["analyst"].get(field)) is not None:
                cells[field] = check_cell(case, field, texts)
        out = list(cells.values()) + formula_cells(case, cells)
        s.set(output_cells=len(out), output_escalate=sum(c["escalate"] for c in out))
        return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    telemetry.setup("check_cells")
    cases = [json.loads(line) for line in builder.CASES.read_text().splitlines()]
    cases = cases[:args.limit] if args.limit else cases
    docs = documents()
    before = judge.spent()
    with telemetry.span("check_cells", input_cases=len(cases)):
        with ThreadPoolExecutor(8) as pool:
            rows = [c for cells in pool.map(telemetry.carry(lambda case: check_case(case, docs)), cases) for c in cells]
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"{len(rows)} cells in {len(cases)} cases: {counts}; escalated {sum(r['escalate'] for r in rows)}; "
          f"spent {judge.spent() - before:.4f} USD")
    telemetry.shutdown()


if __name__ == "__main__":
    main()
