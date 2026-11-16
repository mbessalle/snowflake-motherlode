"""Score extractions of the 236 source-PDF cases, on every extracted field. No model is called here.

    python -m evals.score_pdf --replies DIR --name LABEL [--texts DIR] [--out FILE]

  --replies  DIR/<case id>.json   one extraction per case: the field -> value object the extractor returns
                                  (a {"database_updates": {...}} wrapper is accepted)
  --texts    DIR/<case id>.txt    the PDF text layers (default data/source_text, not committed)

Every field the extractor fills has exactly one rule in RULES below, and a test fails if one is missing, so
no field drops out of the score unnoticed. A rule says how the model's value is compared with the analyst's
and where the analyst's value comes from:

  scraped     written in the announcement. Compared with the analyst, and also checked against the PDF text
              by motherlode.validate (written as such, or derived by a rule: a sum, a split, a unit).
  calculated  a formula over scraped values (aggregate, shares value, drilling value).
  market      share price: market data on the announcement date, usually not in the document.
  location    coordinates: usually from the listing site, not the document. Within 0.05 degrees.
  text        a label from the document, compared after normalising (see the compare functions).
  input       given to the model in the prompt, so agreement says nothing about extraction.
  no_truth    the analyst's table has no comparable value; only how often the model fills it is counted.

For each field the report counts: both filled, agree, model only, analyst only. For scraped fields it also
gives agreement on the subset where the case file says the analyst's value is in the PDF (written or
derived) and the PDF names the analyst's buyer, which is the fair measure of extraction, and the share of
the model's values found in the text.
"""
from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path

from motherlode.fields import extracted_fields
from motherlode.validate import CHECKED_FIELDS, DERIVED, FOUND, NOT_FOUND, to_number, validate

HERE = Path(__file__).parent
CASES = HERE / "pdf_cases.jsonl"
TEXTS = HERE.parent / "data" / "source_text"

# --- comparisons ------------------------------------------------------------------------------------------

HECTARES_PER = {"ha": 1.0, "hectares": 1.0, "hectare": 1.0, "km2": 100.0, "sqkm": 100.0, "squarekm": 100.0,
                "squarekilometres": 100.0, "squarekilometers": 100.0, "acres": 0.40468564224, "acre": 0.40468564224,
                "sqmi": 258.9988110336, "squaremiles": 258.9988110336, "m2": 0.0001}
# The eight Australian states and territories (every case is in Australia): abbreviation -> name.
AUSTRALIAN_STATES = {"nsw": "new south wales", "vic": "victoria", "qld": "queensland", "wa": "western australia",
                     "sa": "south australia", "tas": "tasmania", "nt": "northern territory",
                     "act": "australian capital territory"}
COMPANY_SUFFIXES = re.compile(r"\b(limited|ltd|nl|no liability|corp|corporation|inc|incorporated|plc|pty|co|company)\b")


def plain(text) -> str:
    """Lower case, words separated by single spaces, no punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def same_number(a, b, tolerance: float = 0.01) -> bool:
    a, b = to_number(a), to_number(b)
    return a is not None and b is not None and abs(a - b) <= max(0.011, tolerance * abs(b))


def hectares(value, unit) -> float | None:
    """An area in hectares, or None when the unit is unknown. No unit is read as hectares, the analyst's unit."""
    value = to_number(value)
    factor = HECTARES_PER.get(re.sub(r"[^a-z0-9]", "", str(unit or "ha").lower().replace("²", "2")))
    return None if value is None or factor is None else value * factor


def same_label(a, b) -> bool:
    return plain(a) == plain(b)


def same_set(a, b) -> bool:
    """Comma-separated lists, compared as sets: 'Ag, Au' equals 'Au,Ag'."""
    def items(s):
        return {plain(x) for x in str(s).split(",") if plain(x)}
    return items(a) == items(b)


def company_core(name) -> str:
    """'Errawarra Resources Ltd' -> 'errawarra resources': lower case, no punctuation, no legal suffix."""
    return re.sub(r"\s+", " ", COMPANY_SUFFIXES.sub(" ", plain(name or ""))).strip()


def same_company(a, b) -> bool:
    return company_core(a) == company_core(b)


def same_ticker(a, b) -> bool:
    """'ASX:MM8', 'ASX.MM8' and 'MM8' name the same listing; two different exchanges do not."""
    def parts(s):
        bits = re.split(r"[:.\s]+", str(s).strip().upper())
        return (bits[0], bits[-1]) if len(bits) > 1 else (None, bits[0])
    (ea, ta), (eb, tb) = parts(a), parts(b)
    return ta == tb and (ea is None or eb is None or ea == eb)


def same_state(a, b) -> bool:
    def full(s):
        k = plain(s)
        return AUSTRALIAN_STATES.get(k, k)
    return full(a) == full(b)


def same_date(a, b) -> bool:
    return str(a)[:10] == str(b)[:10]


def same_place(a, b) -> bool:
    return same_number(a, b, tolerance=0) or (to_number(a) is not None and to_number(b) is not None
                                              and abs(to_number(a) - to_number(b)) <= 0.05)


# --- one rule per extracted field ---------------------------------------------------------------------------

SCRAPED, CALCULATED, MARKET, LOCATION, TEXT, INPUT, NO_TRUTH = (
    "scraped", "calculated", "market", "location", "text", "input", "no_truth")

RULES: dict[str, tuple[str, Callable | None, str]] = {
    "project_name": (INPUT, same_label, "given to the model as the project of interest"),
    "primary_commodity": (TEXT, same_label, "chemical symbol; the analyst also uses 'Other'"),
    "commodities": (TEXT, same_set, "compared as a set"),
    "resource_value_scraped": (NO_TRUTH, None, "the analyst's table has no values"),
    "stage_mining_hub": (NO_TRUTH, None, "the analyst's table has no values"),
    "stage_scrapped": (TEXT, same_label, "analyst uses Early Stage / Advanced Stage"),
    "project_state_or_province": (TEXT, same_state, "abbreviations count, e.g. WA"),
    "project_country": (TEXT, same_label, ""),
    "longitude": (LOCATION, same_place, "within 0.05 degrees; usually not in the document"),
    "latitude": (LOCATION, same_place, "within 0.05 degrees; usually not in the document"),
    "project_area": (SCRAPED, None, "converted to hectares with the unit, then within 1%"),
    "project_area_unit": (TEXT, None, "checked through the area: the unit is used for the conversion"),
    "owner_name": (TEXT, same_company, "Ltd, Limited, NL and similar ignored"),
    "owner_ticker": (TEXT, same_ticker, "ASX:X, ASX.X and X match"),
    "owner_marketcap": (NO_TRUTH, None, "the analyst's table has no values"),
    "deal_type": (NO_TRUTH, None, "not comparable: the analyst uses listing-site categories such as "
                                  "'Project Acquisition', the prompt asks for option agreement, JV, sale"),
    "deal_period": (NO_TRUTH, None, "the analyst's table has no values"),
    "interest_acquired_percent": (SCRAPED, same_number, "within 1%"),
    "aggregate_deal_currency": (TEXT, same_label, "ISO code"),
    "aggregate_deal_value": (CALCULATED, same_number, "cash + exploration commitment + shares value; within 1%"),
    "cash_payments_currency": (TEXT, same_label, "ISO code"),
    "cash_payments_value": (SCRAPED, same_number, "within 1%"),
    "exp_comit_currency": (NO_TRUTH, None, "the analyst's table has no values"),
    "exp_comit_value": (SCRAPED, same_number, "within 1%"),
    "drilling_amount": (SCRAPED, same_number, "within 1%"),
    "drilling_value": (CALCULATED, same_number, "metres x 250 USD; within 1%"),
    "shares_currency": (TEXT, same_label, "ISO code"),
    "shares_value": (CALCULATED, same_number, "stated, or shares x price; within 1%"),
    "shares_value_scraped": (SCRAPED, same_number, "within 1%"),
    "shares_amount": (SCRAPED, same_number, "within 1%"),
    "share_price": (MARKET, same_number, "market price on the news date; within 1%"),
    "nsr_percent": (SCRAPED, same_number, "within 1%"),
    "news_date": (TEXT, same_date, "same day"),
}


def agrees(name: str, model: dict, analyst: dict) -> bool:
    if name == "project_area":
        a = hectares(model.get("project_area"), model.get("project_area_unit"))
        b = hectares(analyst.get("project_area"), analyst.get("project_area_unit"))
        return a is not None and b is not None and same_number(a, b)
    if name == "project_area_unit":
        return agrees("project_area", model, analyst)
    return RULES[name][1](model.get(name), analyst.get(name))


def filled(value) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    number = to_number(value) if not isinstance(value, str) else None
    return number != 0


# --- scoring -----------------------------------------------------------------------------------------------

def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_reply(path: Path) -> dict | None:
    if not path.exists():
        return None
    reply = json.loads(path.read_text())
    return reply.get("database_updates", reply) if isinstance(reply, dict) else None


def score(cases: list[dict], replies: Path, texts: Path = TEXTS) -> dict:
    scored, skipped = [], []
    for case in cases:
        model = read_reply(replies / f"{case['id']}.json")
        if model is None:
            skipped.append(case["id"])
            continue
        text_file = texts / f"{Path(case['file']).stem}.txt"
        report = validate(model, " ".join(text_file.read_text().split())) if text_file.exists() else None
        scored.append((case, model, report))

    fields = {}
    for f in extracted_fields():
        kind, _, note = RULES[f.name]
        c = {"kind": kind, "note": note, "both": 0, "agree": 0, "model_only": 0, "analyst_only": 0,
             "in_document": 0, "in_document_agree": 0, "grounded": 0, "grounding_checked": 0}
        for case, model, report in scored:
            m, a = filled(model.get(f.name)), filled(case["analyst"].get(f.name))
            if kind == NO_TRUTH or kind == INPUT:
                c["model_only"] += m
                continue
            if m and a:
                c["both"] += 1
                c["agree"] += agrees(f.name, model, case["analyst"])
            elif m:
                c["model_only"] += 1
            elif a:
                c["analyst_only"] += 1
            supported = f.name in case["written_in_document"] or f.name in case.get("derived_in_document", {})
            if kind == SCRAPED and a and supported and case.get("buyer_in_document", True):
                c["in_document"] += 1
                c["in_document_agree"] += m and agrees(f.name, model, case["analyst"])
            if report is not None and f.name in CHECKED_FIELDS and f.name in report.fields:
                status = report.fields[f.name].status
                c["grounding_checked"] += status in (FOUND, DERIVED, NOT_FOUND)
                c["grounded"] += status in (FOUND, DERIVED)
        fields[f.name] = c
    return {"records": len(scored), "skipped": skipped, "fields": fields}


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def markdown(summary: dict, name: str) -> str:
    out = [f"# Eval: `{name}` on {summary['records']} source-PDF cases\n"]
    if summary["skipped"]:
        out.append(f"No extraction for {len(summary['skipped'])} cases: {', '.join(summary['skipped'][:20])}"
                   + (" ..." if len(summary["skipped"]) > 20 else "") + "\n")
    out += ["| Field | Kind | Both filled | Agree | Model only | Analyst only | Analyst value in PDF: model agrees "
            "| Model values found in PDF | How compared |", "|---|---|---|---|---|---|---|---|---|"]
    for field, c in summary["fields"].items():
        if c["kind"] in (NO_TRUTH, INPUT):
            out.append(f"| `{field}` | {c['kind']} | | | {c['model_only']} | | | | {c['note']} |")
            continue
        in_doc = f"{c['in_document_agree']} of {c['in_document']}" if c["kind"] == SCRAPED else ""
        grounded = f"{c['grounded']} of {c['grounding_checked']}" if c["grounding_checked"] else ""
        out.append(f"| `{field}` | {c['kind']} | {c['both']} | {c['agree']} ({_percent(c['agree'], c['both'])}) | "
                   f"{c['model_only']} | {c['analyst_only']} | {in_doc} | {grounded} | {c['note']} |")
    scraped = [c for c in summary["fields"].values() if c["kind"] == SCRAPED]
    found = sum(c["in_document_agree"] for c in scraped)
    total = sum(c["in_document"] for c in scraped)
    out.append(f"\n- Scraped values the analyst has and the PDF supports, extracted with the same value: "
               f"**{found} of {total} ({_percent(found, total)})**")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replies", type=Path, required=True)
    parser.add_argument("--texts", type=Path, default=TEXTS)
    parser.add_argument("--name", required=True, help="label for the report, for example the extractor and model")
    parser.add_argument("--out", type=Path, help="write the Markdown report here as well as printing it")
    args = parser.parse_args()
    report = markdown(score(load_cases(), args.replies, args.texts), args.name)
    print(report)
    if args.out:
        args.out.write_text(report)


if __name__ == "__main__":
    main()
