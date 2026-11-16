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
rules: tranches summed into one payment, a total split between projects, an area given in km2 and stored in
hectares. Those are recorded separately as derived, with the rule that explains them.

Writes evals/pdf_cases.jsonl (case id, project, chosen file, the analyst's values, which scraped values are
written in the document and which are derived from it) and prints the counts. No document text is stored.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.score_pdf import company_core, plain  # noqa: E402
from motherlode.fields import extracted_fields  # noqa: E402
from motherlode.validate import FIELD_KIND, MONEY, SHARES, find_number, sum_of_amounts, written_amounts  # noqa: E402

CASES = ROOT / "evals" / "pdf_cases.jsonl"
INVENTORY = ROOT / "data" / "pdf_inventory.jsonl"
TEXT_CACHE = ROOT / "data" / "source_text"
SCRAPED = ("nsr_percent", "interest_acquired_percent", "cash_payments_value", "exp_comit_value", "drilling_amount",
           "shares_amount", "project_area")
PERCENT = ("nsr_percent", "interest_acquired_percent")
# every extracted field the analyst's table holds, so the scorer can compare all of them
KEPT = tuple(f.name for f in extracted_fields())


def normalise(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0 else None


def written_fields(row: dict, text: str) -> list[str]:
    """The scraped fields of the row whose value is written in the text."""
    found = []
    for name in SCRAPED:
        value = number(row.get(name))
        if value is not None and find_number(text, value, percent=name in PERCENT, kind=FIELD_KIND.get(name)):
            found.append(name)
    return found


def as_sum(target: float, amounts: list[float], max_terms: int = 3) -> bool:
    """Is the target a sum of two or three of the amounts? The validator's own sum check."""
    return sum_of_amounts(target, amounts, max_terms) is not None


def derived_by_rule(name: str, value: float, text: str) -> str | None:
    """How a scraped value not written as such follows from the document by the original extraction rules
    (docs/extraction-rules in the old pipeline):
      km2 to ha  the area: the document gives km2 and the row holds hectares (value / 100 is written)
      sum        "sum all forms of a payment type": a sum of two or three amounts written in the document
      split      "shared commitments": a written total, or a sum of two, divided between 2-6 projects
    A plausibility test, not a proof: a sum can match by chance, so the README reports the chance rate."""
    if name == "project_area":
        return "km2 to ha" if find_number(text, value / 100) else None
    kind = FIELD_KIND.get(name)
    if kind is None:
        return None
    amounts = written_amounts(text, exclude={MONEY: SHARES, SHARES: MONEY}[kind])
    if as_sum(value, amounts):
        return "sum"
    for n in range(2, 7):
        total = value * n
        if total < 10000 or abs(total - round(total)) > 0.01 * n:
            continue
        if find_number(text, round(total), kind=kind) or as_sum(round(total), amounts, 2):
            return "split"
    return None


def buyer_named(owner: str | None, text: str) -> bool:
    """Rule 1 of the extraction rules is data relevance: is the document about this deal? A cheap test: the
    first word of the buyer's name ('Errawarra' of 'Errawarra Resources Ltd') appears in the text."""
    core = company_core(owner)
    return bool(core) and re.search(r"\b" + re.escape(core.split()[0]) + r"\b", plain(text)) is not None


def choose(candidates: list[tuple[str, list[str], int]]) -> tuple[str, list[str]] | None:
    """The document with the most scraped values written in it; on a tie, the longer text. None if there are none.

    Each candidate is (file name, fields written in it, characters of text)."""
    if not candidates:
        return None
    name, fields, _ = max(candidates, key=lambda c: (len(c[1]), c[2], c[0]))
    return name, fields


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
    computed = shares is not None and price is not None and close(shares * price, value)
    stated = bool(find_number(text, value, kind="money"))
    return "both" if computed and stated else "computed" if computed else "stated" if stated else "unexplained"


def document_text(project: str, file: str, directory: Path) -> str:
    cache = TEXT_CACHE / f"{Path(file).stem}.txt"
    if cache.exists():
        return cache.read_text()
    from pypdf import PdfReader

    try:
        text = "\n\n".join((page.extract_text() or "").strip() for page in PdfReader(directory / project / file).pages)
    except Exception:   # an unreadable file simply has no text
        text = ""
    TEXT_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return text


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    logging.disable(logging.CRITICAL)      # pypdf warns about fonts on many files
    csv.field_size_limit(10**9)
    export, directory = Path(sys.argv[1]), Path(sys.argv[2])
    rows = next(json.loads(r["rows"]) for r in csv.DictReader(export.open(newline="")) if r["source"] == "oceania")
    documents: dict[str, list[dict]] = {}
    for line in INVENTORY.read_text().splitlines():
        entry = json.loads(line)
        if "duplicate_of" not in entry and entry["kind"] != "unreadable":
            documents.setdefault(entry["project"], []).append(entry)

    cases, no_folder = [], 0
    for row in rows:
        project = normalise(row.get("project_name"))
        if project not in documents:
            no_folder += 1
            continue
        candidates, kinds, texts = [], {}, {}
        for entry in documents[project]:
            text = " ".join(document_text(project, entry["file"], directory).split())
            candidates.append((entry["file"], written_fields(row, text), len(text)))
            kinds[entry["file"]], texts[entry["file"]] = entry["kind"], text
        file, written = choose(candidates)
        anywhere = sorted({f for _, fields, _ in candidates for f in fields})
        derived = {}
        for name in SCRAPED:
            value = number(row.get(name))
            if value is not None and name not in written:
                how = derived_by_rule(name, value, texts[file])
                if how:
                    derived[name] = how
        cases.append({
            # two deals on one project can choose the same PDF, so the row number keeps the id unique
            "id": f"{Path(file).stem}-{row['id']}", "row_id": row["id"],
            "buyer_in_document": buyer_named(row.get("owner_name"), texts[file]), "project": project, "project_name": row["project_name"], "file": file,
            "kind": kinds[file], "documents_in_project": len(candidates),
            "analyst": {k: row[k] for k in KEPT if row.get(k) not in (None, "")},
            "scraped_values": sum(1 for k in SCRAPED if number(row.get(k)) is not None),
            "written_in_document": written, "derived_in_document": derived, "written_in_any_document": anywhere,
            "formulas": formula_checks(row),
            "shares_value_source": shares_value_source(row, texts[file]),
        })
    CASES.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases))

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
