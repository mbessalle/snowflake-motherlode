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
import logging
import math
import re
from collections.abc import Callable
from pathlib import Path

from motherlode import telemetry
from motherlode.fields import extracted_fields, numeric_names
from motherlode.validate import CHECKED_FIELDS, DERIVED, FOUND, NOT_FOUND, to_number, validate

log = logging.getLogger("motherlode.score_pdf")
HERE = Path(__file__).parent
CASES = HERE / "pdf_cases.jsonl"
TEXTS = HERE.parent / "data" / "source_text"

# --- comparisons ------------------------------------------------------------------------------------------

_KM2, _MI2, _ACRE = 100.0, 258.9988110336, 0.40468564224
HECTARES_PER = {"ha": 1.0, "hectares": 1.0, "hectare": 1.0, "m2": 0.0001,
                **{k: _KM2 for k in ("km2", "kms2", "sqkm", "sqkms", "squarekm", "squarekms", "squarekilometre",
                                     "squarekilometres", "squarekilometer", "squarekilometers", "sqkilometre",
                                     "sqkilometres", "sqkilometer", "sqkilometers")},
                **{k: _MI2 for k in ("sqmi", "mi2", "sqmile", "sqmiles", "squaremile", "squaremiles")},
                "acres": _ACRE, "acre": _ACRE}
# The eight Australian states and territories (every case is in Australia): abbreviation -> name.
AUSTRALIAN_STATES = {"nsw": "new south wales", "vic": "victoria", "qld": "queensland", "wa": "western australia",
                     "sa": "south australia", "tas": "tasmania", "nt": "northern territory",
                     "act": "australian capital territory"}
# Legal-form words, removed only at the end of a name ("Orion Co Resources" keeps its "co").
COMPANY_SUFFIX = re.compile(r"(?:\s+(?:limited|ltd|nl|no liability|corp|corporation|inc|incorporated|plc|pty|co|company))+$")
EMPTY_WORDS = {"", "0", "n a", "na", "none", "null", "nil", "unknown", "not stated", "not available", "not applicable",
               "false", "nan"}
CURRENCY_SIGNS = {"a$": "AUD", "au$": "AUD", "c$": "CAD", "ca$": "CAD", "us$": "USD", "nz$": "NZD"}
_CURRENCY = r"[A-Z]{0,3}\$|AUD|CAD|USD|NZD|EUR|GBP"
_NUMBER = re.compile(rf"^\s*(?P<before>{_CURRENCY})?\s*(?P<num>-?\d[\d,]*(?:\.\d+)?)\s*"
                     r"(?P<unit>%|billion|bn|million|mm|m|thousand|k)?\s*"
                     rf"(?P<after>{_CURRENCY}|dollars)?\s*$", re.I)
_SCALE = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
_ABBREVIATED = {"bn", "mm", "m", "k"}    # scale only with a currency: "5,000 m" is 5,000 metres (critic, round 2)


def plain(text) -> str:
    """Lower case, words separated by single spaces, no punctuation; '&' is 'and', 'N.L.' is 'nl'."""
    text = re.sub(r"(?<=\b\w)\.(?=\w\b)|(?<=\b\w\.\w)\.", "", str(text).lower()).replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def parse_number(value) -> float | None:
    """A number from a reply: a number, or a string such as '70%', 'A$600,000', '600,000 AUD', '$1.5m' or
    '44.5 million'. The abbreviations m, mm, k and bn scale only next to a currency, so '5,000 m' is 5,000
    (metres). None for anything else, and for NaN or infinity."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        m = _NUMBER.match(value)
        if not m:
            return None
        unit = (m["unit"] or "").lower()
        if unit in _ABBREVIATED and not (m["before"] or m["after"]):
            unit = ""
        number = float(m["num"].replace(",", "")) * _SCALE.get(unit, 1)
    else:
        number = to_number(value)
    return number if number is not None and math.isfinite(number) else None


def same_number(a, b, tolerance: float = 0.01) -> bool:
    """Within `tolerance` of b, relative: 0.02 and 0.03 differ (a share price), 2.0 and 2.01 agree."""
    a, b = parse_number(a), parse_number(b)
    return a is not None and b is not None and abs(a - b) <= tolerance * abs(b) + 1e-9


def hectares(value, unit) -> float | None:
    """An area in hectares, or None when the unit is unknown. No unit is read as hectares, the analyst's unit."""
    value = parse_number(value)
    unit = re.sub(r"\(.*?\)", "", str(unit or "ha").lower().replace("²", "2"))      # "hectares (ha)"
    factor = HECTARES_PER.get(re.sub(r"[^a-z0-9]", "", unit))
    return None if value is None or factor is None else value * factor


def same_label(a, b) -> bool:
    return plain(a) == plain(b)


def same_set(a, b) -> bool:
    """Comma-separated lists, compared as sets: 'Ag, Au' equals 'Au,Ag'."""
    def items(s):
        return {plain(x) for x in str(s).split(",") if plain(x)}
    return items(a) == items(b)


def company_core(name) -> str:
    """'Errawarra Resources Ltd' -> 'errawarra resources': lower case, no punctuation, no legal suffix at the end."""
    return COMPANY_SUFFIX.sub("", plain(name or "")).strip()


def same_company(a, b) -> bool:
    core = company_core(a)
    return bool(core) and core == company_core(b)


EXCHANGE_SUFFIX = {"AX": "ASX", "V": "TSXV", "TO": "TSX", "CN": "CSE", "L": "LSE"}


def listings(s) -> set[tuple[str | None, str]]:
    """Every (exchange, code) in a ticker field: 'ASX:MM8', 'ASX.MM8', '(ASX:MM8)', 'MM8.AX', 'MM8',
    'ASX:BNZ, TSXV:BZ', 'ASX:ERW (Errawarra Resources)' (a name in brackets is dropped)."""
    out = set()
    text = re.sub(r"\(([^()]*)\)", lambda m: m[0] if re.fullmatch(r"\s*[A-Z]+\s*[:.]\s*[A-Z0-9]+\s*", m[1].upper())
                  else " ", str(s))
    for part in re.split(r"[,;/]|\band\b", text.upper()):
        bits = [b for b in re.split(r"[^A-Z0-9]+", part) if b]
        if len(bits) == 1:
            out.add((None, bits[0]))
        elif len(bits) >= 2:
            if bits[-1] in EXCHANGE_SUFFIX and len(bits[0]) <= 5 and bits[0] not in EXCHANGE_SUFFIX.values():
                out.add((EXCHANGE_SUFFIX[bits[-1]], bits[0]))       # MM8.AX
            else:
                out.add((bits[0], bits[-1]))
    return out


def same_ticker(a, b) -> bool:
    """Some listing in a equals some listing in b; an exchange missing on one side is not a conflict."""
    return any(ta == tb and (ea is None or eb is None or ea == eb) for ea, ta in listings(a) for eb, tb in listings(b))


def same_currency(a, b) -> bool:
    def code(s):
        k = str(s).strip().lower()
        return CURRENCY_SIGNS.get(k, k.upper())
    return code(a) == code(b)


def same_state(a, b) -> bool:
    def full(s):
        k = plain(s)
        return AUSTRALIAN_STATES.get(k, k)
    return full(a) == full(b)


def same_date(a, b) -> bool:
    return str(a)[:10] == str(b)[:10]


def same_place(a, b) -> bool:
    a, b = parse_number(a), parse_number(b)
    return a is not None and b is not None and abs(a - b) <= 0.05


# --- one rule per extracted field ---------------------------------------------------------------------------

# Derivation rules of the case builder whose results count as confirmed: a hand check found the km2 rule right
# once it requires a km2 unit, while sums matched by chance often enough to be reported apart.
CONFIRMED_RULES = {"km2 to ha"}

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
    "aggregate_deal_currency": (TEXT, same_currency, "ISO code; A$, C$, US$ accepted"),
    "aggregate_deal_value": (CALCULATED, same_number, "cash + exploration commitment + shares value; within 1%"),
    "cash_payments_currency": (TEXT, same_currency, "ISO code; A$, C$, US$ accepted"),
    "cash_payments_value": (SCRAPED, same_number, "within 1%"),
    "exp_comit_currency": (NO_TRUTH, None, "the analyst's table has no values"),
    "exp_comit_value": (SCRAPED, same_number, "within 1%"),
    "drilling_amount": (SCRAPED, same_number, "within 1%"),
    "drilling_value": (CALCULATED, same_number, "metres x 250 USD; within 1%"),
    "shares_currency": (TEXT, same_currency, "ISO code; A$, C$, US$ accepted"),
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
    """Does the reply hold a value? None, False, 0, empty lists and objects, and strings such as '', '0',
    'N/A', 'null' or 'none' do not count: a model writing 'N/A' left the field empty."""
    if value is None or isinstance(value, bool) or value == [] or value == {}:
        return False
    if isinstance(value, str):
        # "0.0", "0.00" and "A$0" are a zero written as text, as empty as the number 0 (critic, round 2)
        return plain(value) not in EMPTY_WORDS and parse_number(value) != 0
    number = parse_number(value)
    return number is not None and number != 0


# --- scoring -----------------------------------------------------------------------------------------------

def case_text(case: dict, texts: Path = TEXTS) -> str:
    """The text of a case's documents: every PDF of its set (`files`, the announcing PDF first; a deal's terms can
    be split between the agreement and a completion notice), or its one `file` for cases built before sets.
    Whitespace is normalised; documents are separated by a marker naming the file. Empty without a text layer."""
    files = case.get("files") or [case["file"]]
    parts = []
    for file in files:
        path = texts / f"{Path(file).stem}.txt"
        if path.exists():
            text = " ".join(path.read_text().split())
            parts.append(f"=== {Path(file).name} === {text}" if len(files) > 1 else text)
    return " ".join(parts)


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class BadReply(ValueError):
    pass


def read_reply(path: Path) -> dict | None:
    """The field -> value object of a reply file, None when there is no file. Raises BadReply when the file is
    not a JSON object (or a {"database_updates": {...}} wrapper around one)."""
    if not path.exists():
        return None
    try:
        reply = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BadReply(f"not JSON: {error}") from error
    except OSError as error:            # a folder named <id>.json, no permission (critic, round 2)
        raise BadReply(f"cannot read: {error}") from error
    if isinstance(reply, dict) and "database_updates" in reply:
        reply = reply["database_updates"]
    if not isinstance(reply, dict):
        raise BadReply(f"not a JSON object but {type(reply).__name__}")
    return reply


def for_validator(model: dict) -> dict:
    """Numbers the model wrote as text ('70%', 'A$600,000', '44.5 million') as numbers, so the validator
    checks them instead of treating them as empty."""
    numeric = numeric_names()
    return {k: (parse_number(v) if k in numeric and isinstance(v, str) else v) for k, v in model.items()}


# Below this probability the decision model's reading of the evidence overrules the code's match. One value for
# all four questions: it agrees best with 147 blind hand labels (16 Nov 2026; scripts/judge_cases.py calibrate).
JUDGE_THRESHOLD = 0.5
JUDGMENTS = HERE / "judge_results.jsonl"


def load_judgments(path: Path = JUDGMENTS) -> dict:
    """The decision model's answers as probabilities that the evidence is right: per row, P(the analyst's
    buyer is the buyer); per (row, field), P(the evidence means this field). A sum is judged amount by amount
    and holds only if every amount does (the smallest P)."""
    buyer, evidence, parts = {}, {}, {}
    for r in map(json.loads, path.read_text().splitlines()):
        answer = r.get("answer") or {}
        if r["question"] == "buyer_role":
            buyer[r["row_id"]] = (answer.get("probabilities") or {}).get("buyer", 0.0)
        elif r["question"] == "area_scope":
            evidence[(r["row_id"], "project_area")] = (answer.get("probabilities") or {}).get("this_project", 0.0)
        elif r["question"] == "percent_role":
            evidence[(r["row_id"], r["field"])] = answer.get("noul", 0.0)
        elif r["question"] == "sum_part":
            parts.setdefault((r["row_id"], r["field"]), []).append(answer.get("noul", 0.0))
    evidence.update({key: min(ps) for key, ps in parts.items()})
    return {"buyer": buyer, "evidence": evidence}


def score(cases: list[dict], replies: Path, texts: Path = TEXTS, judgments: dict | None = None) -> dict:
    """With `judgments` (load_judgments()), evidence the decision model rejects leaves the headline measure,
    and sums it accepts join it, reported in their own columns."""
    with telemetry.span("score_pdf", input_cases=len(cases), input_replies=str(replies), input_texts=str(texts),
                        judged=judgments is not None) as root:
        summary = _score(cases, replies, texts, judgments)
        root.set(output_records=summary["records"], output_skipped=len(summary["skipped"]))
        return summary


def _score_case(case: dict, model: dict, texts: Path):
    """One span per case, with the validator's span inside it: the reply as input, and for every compared
    field both values and the verdict. Returns the validator's report, or None without a text layer."""
    with telemetry.span("score_case", case_id=case["id"], file=case["file"], input_reply=model,
                        buyer_in_document=case.get("buyer_in_document", True)) as s:
        text = case_text(case, texts)
        report = None
        if text:
            report = validate(for_validator(model), text)
        else:
            log.warning("case %s: no text layer in %s, so its values are not checked against the PDF", case["id"], texts)
        for name, (kind, _, _) in RULES.items():
            m, a = model.get(name), case["analyst"].get(name)
            if not (filled(m) or filled(a)):
                continue
            verdict = ("not compared" if kind in (NO_TRUTH, INPUT) else "agree" if filled(m) and filled(a) and agrees(name, model, case["analyst"])
                       else "disagree" if filled(m) and filled(a) else "model only" if filled(m) else "analyst only")
            s.set(**{f"field.{name}.model": m, f"field.{name}.analyst": a, f"field.{name}.verdict": verdict,
                     f"field.{name}.validator": report.fields[name].status if report and name in report.fields else None})
            if verdict == "disagree":
                log.info("case %s: %s model %r, analyst %r", case["id"], name, m, a)
        return report


def _not_scored(case: dict, reason: str, detail: str) -> None:
    """A case that is not scored still gets its span, so the trace shows every case (critic, round 2)."""
    with telemetry.span("score_case", case_id=case["id"], file=case.get("file"), output_not_scored=reason,
                        detail=detail):
        log.warning("case %s: %s, not scored: %s", case["id"], reason, detail)


def _score(cases: list[dict], replies: Path, texts: Path, judgments: dict | None = None) -> dict:
    scored, skipped, bad = [], [], []
    for case in cases:
        try:
            model = read_reply(replies / f"{case['id']}.json")
        except BadReply as error:
            bad.append(case["id"])
            _not_scored(case, "unreadable reply", str(error))
            continue
        if model is None:
            skipped.append(case["id"])
            _not_scored(case, "no extraction", str(replies))
            continue
        try:
            scored.append((case, model, _score_case(case, model, texts)))
        except Exception as error:      # one reply never stops the run (critic, round 2: 1e308 overflowed)
            bad.append(case["id"])
            log.error("case %s: scoring failed, not scored: %r", case["id"], error)

    fields = {}
    for f in extracted_fields():
        kind, _, note = RULES[f.name]
        c = {"kind": kind, "note": note, "both": 0, "agree": 0, "model_only": 0, "analyst_only": 0,
             "in_document": 0, "in_document_agree": 0, "probable": 0, "probable_agree": 0,
             "judged_in": 0, "judged_in_agree": 0, "judged_out": 0, "grounded": 0, "grounding_checked": 0}
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
            # a case whose PDF does not name the buyer, or where no PDF of the project announces the deal, has no
            # document to score against
            buyer_ok = case.get("buyer_in_document", True) and case.get("announcement_found", True)
            if judgments is not None and case["row_id"] in judgments["buyer"]:
                buyer_ok = buyer_ok and judgments["buyer"][case["row_id"]] >= JUDGE_THRESHOLD
            if kind == SCRAPED and a and not buyer_ok:
                c["judged_out"] += 1 if judgments is not None and case.get("buyer_in_document", True) else 0
            if kind == SCRAPED and a and buyer_ok:
                rule = case.get("derived_in_document", {}).get(f.name)
                right = bool(m and agrees(f.name, model, case["analyst"]))
                p = None if judgments is None else judgments["evidence"].get((case["row_id"], f.name))
                if f.name in case["written_in_document"] or rule in CONFIRMED_RULES:
                    if p is not None and p < JUDGE_THRESHOLD:       # the model reads the evidence otherwise
                        c["judged_out"] += 1
                    else:
                        c["in_document"] += 1
                        c["in_document_agree"] += right
                elif rule and p is not None and p >= JUDGE_THRESHOLD:  # a sum the model accepts, amount by amount
                    c["judged_in"] += 1
                    c["judged_in_agree"] += right
                elif rule and p is not None:
                    c["judged_out"] += 1
                elif rule:          # a sum of written amounts: probably from the document, not confirmed
                    c["probable"] += 1
                    c["probable_agree"] += right
            if report is not None and f.name in CHECKED_FIELDS and f.name in report.fields:
                status = report.fields[f.name].status
                c["grounding_checked"] += status in (FOUND, DERIVED, NOT_FOUND)
                c["grounded"] += status in (FOUND, DERIVED)
        fields[f.name] = c
    return {"records": len(scored), "skipped": skipped, "unreadable": bad, "fields": fields, "judged": judgments is not None}


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def markdown(summary: dict, name: str) -> str:
    out = [f"# Eval: `{name}` on {summary['records']} source-PDF cases\n"]
    if summary["skipped"]:
        out.append(f"No extraction for {len(summary['skipped'])} cases: {', '.join(summary['skipped'][:20])}"
                   + (" ..." if len(summary["skipped"]) > 20 else "") + "\n")
    if summary.get("unreadable"):
        out.append(f"Unreadable reply files, not scored: {', '.join(summary['unreadable'])}\n")
    out += ["| Field | Kind | Both filled | Agree | Model only | Analyst only | Analyst value in PDF: model agrees "
            "| Probably in PDF (a sum): model agrees | Model values found in PDF | How compared |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for field, c in summary["fields"].items():
        if c["kind"] in (NO_TRUTH, INPUT):
            out.append(f"| `{field}` | {c['kind']} | | | {c['model_only']} | | | | | {c['note']} |")
            continue
        in_doc = f"{c['in_document_agree']} of {c['in_document']}" if c["kind"] == SCRAPED else ""
        probable = f"{c['probable_agree']} of {c['probable']}" if c["kind"] == SCRAPED and c["probable"] else ""
        grounded = f"{c['grounded']} of {c['grounding_checked']}" if c["grounding_checked"] else ""
        out.append(f"| `{field}` | {c['kind']} | {c['both']} | {c['agree']} ({_percent(c['agree'], c['both'])}) | "
                   f"{c['model_only']} | {c['analyst_only']} | {in_doc} | {probable} | {grounded} | {c['note']} |")
    scraped = [c for c in summary["fields"].values() if c["kind"] == SCRAPED]
    found, total = sum(c["in_document_agree"] for c in scraped), sum(c["in_document"] for c in scraped)
    maybe, maybe_total = sum(c["probable_agree"] for c in scraped), sum(c["probable"] for c in scraped)
    if summary.get("judged"):
        ok, ok_total = sum(c["judged_in_agree"] for c in scraped), sum(c["judged_in"] for c in scraped)
        out_total = sum(c["judged_out"] for c in scraped)
        out.append(f"\n- Scraped values the analyst has and the PDF confirms (written, or an area in km², and not "
                   f"rejected by the decision model), plus sums the model accepts amount by amount, extracted with "
                   f"the same value: **{found + ok} of {total + ok_total} ({_percent(found + ok, total + ok_total)})**"
                   f" ({found} of {total} confirmed by code, {ok} of {ok_total} sums confirmed by the model)")
        out.append(f"- Left out because the decision model reads the evidence otherwise, or the PDF names the "
                   f"analyst's buyer in another role: {out_total} values")
    else:
        out.append(f"\n- Scraped values the analyst has and the PDF confirms (written, or an area in km²), extracted "
                   f"with the same value: **{found} of {total} ({_percent(found, total)})**")
    if maybe_total:
        out.append(f"- Values that are probably a sum of amounts in the PDF, reported apart: {maybe} of {maybe_total}")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replies", type=Path, required=True)
    parser.add_argument("--texts", type=Path, default=TEXTS)
    parser.add_argument("--name", required=True, help="label for the report, for example the extractor and model")
    parser.add_argument("--out", type=Path, help="write the Markdown report here as well as printing it")
    parser.add_argument("--no-judgments", action="store_true",
                        help="ignore the decision model's answers (evals/judge_results.jsonl) and score by code alone")
    args = parser.parse_args()
    print("telemetry:", telemetry.setup("score_pdf"))
    judgments = None if args.no_judgments or not JUDGMENTS.exists() else load_judgments()
    report = markdown(score(load_cases(), args.replies, args.texts, judgments), args.name)
    telemetry.shutdown()
    print(report)
    if args.out:
        args.out.write_text(report)


if __name__ == "__main__":
    main()
