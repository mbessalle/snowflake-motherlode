"""Build the larger eval set: one case per project, pairing the analyst's database row with its source PDF.

    python scripts/build_pdf_cases.py EXPORT.csv PDF_DIR

EXPORT.csv is a read-only export of the deal table (one JSON array per source); PDF_DIR holds one folder per
project. Neither is in the repository.

A project folder often holds several PDFs about the same sale, and by the analyst's own account only one of
them carries the deal terms. So for each project every distinct PDF is scored by how many of the row's
scraped values are written in its text, and the best one becomes the case document.

Which fields can be checked against a document follows the analyst's field definitions:
  scraped     written in the announcement: royalty, interest acquired, cash payments, exploration
              commitment, metres of drilling, number of shares, project area
  calculated  aggregate = cash + exploration commitment + shares value; shares value = shares x price;
              drilling value = metres x 250 USD. Checked by their formula, not against the text.
  market      share price: the market price on the announcement date. Not in the document; not scored.

A scraped value that is not written as such may still follow from the document by the original extraction
rules: tranches summed into one payment, or an area given in km2 and stored in hectares. Those are recorded
separately as derived, with the rule that explains them (see derive).

Writes evals/pdf_cases.jsonl (case id, project, chosen file, the analyst's values, which scraped values are
written in the document and which are derived from it) and prints the counts. No document text is stored.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.score_pdf import company_core, listings, plain  # noqa: E402
from motherlode import telemetry  # noqa: E402
from motherlode.fields import extracted_fields  # noqa: E402
from motherlode.validate import (  # noqa: E402
    FIELD_KIND,
    FIELD_NEAR,
    FIELD_SKIP,
    _forms,
    _written,
    find_number,
    sum_of_amounts,
    written_amounts,
)

CASES = ROOT / "evals" / "pdf_cases.jsonl"
INVENTORY = ROOT / "data" / "pdf_inventory.jsonl"
TEXT_CACHE = ROOT / "data" / "source_text"
SCRAPED = ("nsr_percent", "interest_acquired_percent", "cash_payments_value", "exp_comit_value", "drilling_amount",
           "shares_amount", "project_area")
PERCENT = ("nsr_percent", "interest_acquired_percent")
log = logging.getLogger("motherlode.build_pdf_cases")
# every extracted field the analyst's table holds, so the scorer can compare all of them
KEPT = tuple(f.name for f in extracted_fields())


def normalise(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0 else None


HA_AFTER = r"\s*(?:ha\b|hectares?\b)"


def evidence(name: str, value: float, text: str, width: int = 90) -> str | None:
    """The snippet where a scraped value is written in the text, with the same checks the validator makes:
    a percentage near a word of its kind, a cash payment that is not an exploration amount or a placement,
    and (here only) an area followed by a hectare unit, since a bare number is no area (critic, round 2)."""
    if name == "project_area":
        for form in _forms(value):
            m = re.search(_written(form) + HA_AFTER, text, re.I)
            if m:
                return re.sub(r"\s+", " ", text[max(0, m.start() - width):m.end() + width]).strip()
        return None
    return find_number(text, value, percent=name in PERCENT, kind=FIELD_KIND.get(name), near=FIELD_NEAR.get(name),
                       skip=FIELD_SKIP.get(name), width=width)


def written_fields(row: dict, text: str) -> list[str]:
    """The scraped fields of the row whose value is written in the text."""
    return [name for name in SCRAPED if number(row.get(name)) is not None and evidence(name, number(row.get(name)), text)]


def as_sum(target: float, amounts: list[float], max_terms: int = 3) -> bool:
    """Is the target a sum of two or three of the amounts? The validator's own sum check."""
    return sum_of_amounts(target, amounts, max_terms) is not None


KM2_AFTER = r"\s*(?:km2|km²|km 2|sq\.?\s?kms?\b|square\s+kilomet(?:re|er)s?)"


def km2_written(text: str, km2: float, width: int = 90) -> str | None:
    """The snippet where `km2` is written followed by a km2 unit, else None. A bare number does not count:
    "12 months" or "16 km of strike length" is not a 1,200 ha area (critic finding 4)."""
    for form in _forms(km2):
        m = re.search(_written(form) + KM2_AFTER, text, re.I)
        if m:
            return re.sub(r"\s+", " ", text[max(0, m.start() - width):m.end() + width]).strip()
    return None


def derive(name: str, value: float, text: str) -> tuple[str, str] | None:
    """(rule, evidence) when a scraped value not written as such follows from the document by the original
    extraction rules (docs/extraction-rules in the old pipeline), else None:
      km2 to ha  the area: the document gives the area in km2 (value / 100 followed by a km2 unit) and the
                 row holds hectares
      sum        "sum all forms of a payment type": a sum of two or three amounts of the same kind written
                 in the document (money for a payment, share counts for shares)
    The evidence is the text snippet or the amounts used, for the trace. A sum is a plausibility test, not a
    proof: a hand check found many chance matches, so the scorer reports sums apart from confirmed values.

    The rule for a total shared between projects was removed: none of the 13 splits it found survived a
    hand check, because nothing tied the total to the number of projects."""
    if name == "project_area":
        snippet = km2_written(text, value / 100)
        return ("km2 to ha", snippet) if snippet else None
    kind = FIELD_KIND.get(name)
    if kind is None:
        return None
    combo = sum_of_amounts(value, written_amounts(text, only=kind))
    return ("sum", " + ".join(f"{c:,.0f}" for c in combo)) if combo else None


def derived_by_rule(name: str, value: float, text: str) -> str | None:
    found = derive(name, value, text)
    return found[0] if found else None


def buyer_named(owner: str | None, text: str, ticker: str | None = None) -> bool:
    """Rule 1 of the extraction rules is data relevance: is the document about this deal? A cheap test: the
    buyer's whole name without its legal form ('errawarra resources' of 'Errawarra Resources Ltd'), or its
    ticker code (MM8 of ASX:MM8, in capitals), appears in the text. The first word alone was too weak:
    'West Coast Silver' matched 'Challenger West' (critic finding 9)."""
    core = company_core(owner)
    if core and re.search(r"\b" + re.escape(core) + r"\b", plain(text)):
        return True
    codes = {code for _, code in listings(ticker)} if ticker else set()
    return any(len(code) >= 3 and re.search(r"\b" + re.escape(code) + r"\b", text) for code in codes)


def choose(candidates: list[tuple]) -> tuple[str, list[str]] | None:
    """The document with the most scraped values written in it; on a tie, the one that gives the row's news
    date most often, then the longer text. None if there are none.

    Each candidate is (file name, fields written in it, characters of text[, times it gives the news date]).
    The date comes second because a project folder can hold the agreement and, weeks later, the completion
    notice the row was taken from (critic, round 2: kat_gap, newman). It is a count, not yes or no, because a
    later announcement can cite the earlier one's date once ("released yesterday, 25 March 2025")."""
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (len(c[1]), c[3] if len(c) > 3 else 0, c[2], c[0]))
    return best[0], best[1]


def mentions_date(text: str, day: str | None) -> int:
    """How often the text gives this date (YYYY-MM-DD) as '24 March 2025', 'March 24, 2025', '24 Mar 2025',
    '2025-03-24' or '24/03/2025'."""
    try:
        d = dt.date.fromisoformat(str(day)[:10])
    except ValueError:
        return 0
    month, short = d.strftime("%B"), d.strftime("%b")
    forms = [rf"\b0?{d.day}(?:st|nd|rd|th)?\s+(?:{month}|{short})\.?,?\s+{d.year}\b",
             rf"\b(?:{month}|{short})\.?\s+0?{d.day}(?:st|nd|rd|th)?,?\s+{d.year}\b",
             rf"\b{d.isoformat()}\b", rf"\b0?{d.day}[/.]0?{d.month}[/.]{d.year}\b"]
    return sum(len(re.findall(f, text, re.I)) for f in forms)


def close(a: float, b: float, tolerance: float = 0.02) -> bool:
    return abs(a - b) <= max(1.0, tolerance * abs(b))


def formula_checks(row: dict) -> dict[str, bool]:
    """Do the calculated fields follow the analyst's formulas? Only the checks whose inputs are present."""
    out = {}
    aggregate, shares_value = number(row.get("aggregate_deal_value")), number(row.get("shares_value"))
    parts = [number(row.get(k)) or 0.0 for k in ("cash_payments_value", "exp_comit_value", "shares_value")]
    if aggregate is not None and any(parts):
        out["aggregate"] = close(sum(parts), aggregate)
    shares, price = number(row.get("shares_amount")), number(row.get("share_price"))
    if shares_value is not None and shares is not None and price is not None:
        out["shares_value"] = close(shares * price, shares_value)
    return out


def shares_value_source(row: dict, text: str) -> str | None:
    """Where a row's shares value comes from. The analyst's sheet allows two origins: the announcement states
    the value of the share payment directly, or it is computed as number of shares x a share price (taken
    from the announcement or from the listing site).

    stated: the value is written in the document. computed: it equals shares x the stored share price.
    both, or unexplained when neither holds (for example a price that was used but not stored)."""
    value = number(row.get("shares_value"))
    if value is None:
        return None
    shares, price = number(row.get("shares_amount")), number(row.get("share_price"))
    computed = shares is not None and price is not None and (close(shares * price, value) or rounds_to(value / shares, price))
    # stated: written as money near the word share, and not simply the cash payment's number (critic, round 2:
    # a waived option fee and an exploration spend had counted as a stated shares value)
    stated = value != number(row.get("cash_payments_value")) and bool(find_number(text, value, kind="money", near=SHARE_WORDS))
    return "both" if computed and stated else "computed" if computed else "stated" if stated else "unexplained"


SHARE_WORDS = re.compile(r"\bshares?\b|\bscrip\b", re.I)


def rounds_to(implied: float, stored: float) -> bool:
    """Is the stored share price the implied price rounded to the stored price's decimals? 262,300 / 6,100,000
    = 0.043, stored as 0.04 (critic, round 2: bynoe)."""
    decimals = len(f"{stored:.6f}".rstrip("0").split(".")[1]) if "." in f"{stored:.6f}".rstrip("0") else 0
    return round(implied, decimals) == round(stored, decimals)


def document_text(project: str, file: str, directory: Path) -> str:
    cache = TEXT_CACHE / f"{Path(file).stem}.txt"
    if cache.exists():
        return cache.read_text()
    from pypdf import PdfReader

    path = directory / project / file
    if not path.exists():
        # a wrong PDF folder used to give empty texts, an empty cache and exit code 0 (critics, round 2)
        raise SystemExit(f"missing PDF {path}: is {directory} the folder that holds one folder per project?")
    try:
        text = "\n\n".join((page.extract_text() or "").strip() for page in PdfReader(path).pages)
    except Exception as error:      # an unreadable file has no text, and the empty text is not cached
        log.warning("cannot read %s: %s", path, error)
        return ""
    TEXT_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return text


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    logging.getLogger("pypdf").setLevel(logging.ERROR)      # pypdf warns about fonts on many files
    print("telemetry:", telemetry.setup("build_pdf_cases"))
    with telemetry.span("build_pdf_cases", input_export=sys.argv[1], input_pdf_dir=sys.argv[2]) as root:
        cases = build(Path(sys.argv[1]), Path(sys.argv[2]))
        root.set(output_cases=len(cases), output_file=str(CASES))
    telemetry.shutdown()


def build(export: Path, directory: Path) -> list[dict]:
    csv.field_size_limit(10**9)
    rows = next(json.loads(r["rows"]) for r in csv.DictReader(export.open(newline="")) if r["source"] == "oceania")
    documents: dict[str, list[dict]] = {}
    for line in INVENTORY.read_text().splitlines():
        entry = json.loads(line)
        if "duplicate_of" not in entry and entry["kind"] != "unreadable":
            documents.setdefault(entry["project"], []).append(entry)

    cases, no_folder = [], 0
    for row in rows:
        project = normalise(row.get("project_name"))
        with telemetry.span("case", row_id=row["id"], project=project, project_name=row.get("project_name"),
                            input_analyst_scraped={k: row.get(k) for k in SCRAPED if number(row.get(k)) is not None}) as s:
            if project not in documents:
                no_folder += 1
                log.warning("row %s: no PDF folder for project %r", row["id"], project)
                s.set(output_skipped="no PDF folder")
                continue
            case = build_case(row, project, documents[project], directory, s)
            cases.append(case)
    CASES.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases))
    report(rows, cases, no_folder)
    return cases


def build_case(row: dict, project: str, entries: list[dict], directory: Path, s) -> dict:
    """One case: score every PDF of the project, keep the best, and record how each value is supported."""
    candidates, kinds, texts = [], {}, {}
    for entry in entries:
        text = " ".join(document_text(project, entry["file"], directory).split())
        candidates.append((entry["file"], written_fields(row, text), len(text), mentions_date(text, row.get("news_date"))))
        kinds[entry["file"]], texts[entry["file"]] = entry["kind"], text
    file, written = choose(candidates)
    text = texts[file]
    s.set(step_candidates=[{"file": f, "written": w, "chars": n, "mentions_news_date": d} for f, w, n, d in candidates],
          output_chosen=file)
    for name in written:          # the snippet that proves each written value, for checking by eye or by code
        value = number(row.get(name))
        s.set(**{f"written.{name}.value": value,
                 f"written.{name}.evidence": evidence(name, value, text)})
    derived = {}
    for name in SCRAPED:
        value = number(row.get(name))
        if value is not None and name not in written:
            found = derive(name, value, text)
            s.set(**{f"derived.{name}.value": value, f"derived.{name}.rule": found[0] if found else "none",
                     f"derived.{name}.evidence": found[1] if found else None})
            if found:
                derived[name] = found[0]
    case = {
        # two deals on one project can choose the same PDF, so the row number keeps the id unique
        "id": f"{Path(file).stem}-{row['id']}", "row_id": row["id"],
        "buyer_in_document": buyer_named(row.get("owner_name"), text, row.get("owner_ticker")), "project": project,
        "project_name": row["project_name"], "file": file, "kind": kinds[file], "documents_in_project": len(candidates),
        "analyst": {k: row[k] for k in KEPT if row.get(k) not in (None, "")},
        "scraped_values": sum(1 for k in SCRAPED if number(row.get(k)) is not None),
        "written_in_document": written, "derived_in_document": derived,
        "written_in_any_document": sorted({f for c in candidates for f in c[1]}),
        "formulas": formula_checks(row), "shares_value_source": shares_value_source(row, text),
    }
    if not case["buyer_in_document"]:
        log.warning("case %s: the chosen PDF %s does not name the buyer %r", case["id"], file, row.get("owner_name"))
    s.set(case_id=case["id"], output_buyer_in_document=case["buyer_in_document"], output_formulas=case["formulas"],
          output_shares_value_source=case["shares_value_source"], output_case=case)
    return case


def report(rows: list[dict], cases: list[dict], no_folder: int) -> None:

    total = sum(c["scraped_values"] for c in cases)
    best = sum(len(c["written_in_document"]) for c in cases)
    anywhere = sum(len(c["written_in_any_document"]) for c in cases)
    several = [c for c in cases if c["documents_in_project"] > 1]
    print(f"rows: {len(rows)}; without a PDF folder: {no_folder}; cases: {len(cases)}")
    print(f"scraped values in the rows: {total}; written in the chosen PDF: {best} ({100 * best / total:.0f}%); "
          f"written in any PDF of the project: {anywhere} ({100 * anywhere / total:.0f}%)")
    for name in SCRAPED:
        have = [c for c in cases if number(c["analyst"].get(name)) is not None]
        hit = sum(1 for c in have if name in c["written_in_document"])
        how = [c["derived_in_document"][name] for c in have if name in c["derived_in_document"]]
        print(f"  {name:<28} {hit:>3} of {len(have):>3}  ({100 * hit / max(1, len(have)):.0f}%); by a rule {len(how):>3}"
              + "".join(f", {k} {how.count(k)}" for k in sorted(set(how))))
    one_holds_all = sum(1 for c in several if len(c["written_in_document"]) == len(c["written_in_any_document"]))
    print(f"projects with several PDFs: {len(several)}; the chosen PDF holds every value any of them holds: {one_holds_all}")
    checked = [c["formulas"]["aggregate"] for c in cases if "aggregate" in c["formulas"]]
    print(f"formula aggregate: {sum(checked)} of {len(checked)} rows consistent")
    sources = [c["shares_value_source"] for c in cases if c["shares_value_source"]]
    print(f"shares value, {len(sources)} rows: " + ", ".join(f"{k} {sources.count(k)}" for k in ("stated", "computed", "both", "unexplained")))
    print(f"chosen PDF names the analyst's buyer: {sum(c['buyer_in_document'] for c in cases)} of {len(cases)}")
    print(f"chosen PDFs: digital {sum(c['kind'] == 'digital' for c in cases)}, mixed {sum(c['kind'] == 'mixed' for c in cases)}")


if __name__ == "__main__":
    main()
