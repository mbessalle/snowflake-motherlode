"""Test the stated conversion rule: converted value = amount in the announcement's currency x the rate on the article date.

    python scripts/check_conversion.py EXPORT.csv [RATES.json]

The analyst's earlier sheet holds amounts converted to US dollars, while the deal table keeps the amounts as
the Australian announcements state them. This compares the two for the same project, using daily reference
rates from the Frankfurter service (European Central Bank data, no key needed). The rates are downloaded
to RATES.json when that file does not exist.

It answers one question with counts: can the converted figures be reproduced from the documents plus public
exchange rates, and how closely? No model is involved.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import statistics
import sys
from pathlib import Path

RATES_URL = "https://api.frankfurter.dev/v1/2021-06-01..{end}?base=AUD&symbols=USD"
PAIRS = (("cash_payments_value", "cash"), ("aggregate_deal_value", "agg"), ("share_price", "share_price"))


def normalise(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def rate_on(rates: dict, day: str) -> float:
    """The US dollar rate on a day, or on the last business day before it (weekends have no rate)."""
    current = dt.date.fromisoformat(day[:10])
    while current.isoformat() not in rates:
        current -= dt.timedelta(days=1)
    return rates[current.isoformat()]["USD"]


def classify(native: float, converted: float, rate: float) -> str:
    """same: not converted. Otherwise how close the converted value is to native x rate."""
    ratio = converted / native
    if abs(ratio - 1) <= 0.01:
        return "same"
    error = abs(converted / (native * rate) - 1)
    return "within 2%" if error <= 0.02 else "within 5%" if error <= 0.05 else "further off"


def main() -> None:
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    csv.field_size_limit(10**9)
    export = Path(sys.argv[1])
    rates_file = Path(sys.argv[2]) if len(sys.argv) == 3 else export.with_name("fx_aud_usd.json")
    if not rates_file.exists():
        import requests

        rates_file.write_text(requests.get(RATES_URL.format(end=dt.date.today().isoformat()), timeout=60).text)
    rates = json.loads(rates_file.read_text())["rates"]
    sources = {r["source"]: json.loads(r["rows"]) for r in csv.DictReader(export.open(newline=""))}
    table = {normalise(row["project_name"]): row for row in sources["oceania"]}
    for table_field, sheet_field in PAIRS:
        counts: dict[str, int] = {}
        errors = []
        for row in sources["oceania_sheet_20261113"]:
            native = (table.get(normalise(row["project_name"])) or {}).get(table_field)
            converted, day = row.get(sheet_field), row.get("article_date")
            if not native or not converted or not day:
                continue
            kind = classify(native, converted, rate_on(rates, str(day)))
            counts[kind] = counts.get(kind, 0) + 1
            if kind != "same":
                errors.append(converted / (native * rate_on(rates, str(day))))
        total = sum(counts.values())
        print(f"{sheet_field}: {total} pairs; " + ", ".join(f"{k} {counts.get(k, 0)}" for k in ("same", "within 2%", "within 5%", "further off"))
              + (f"; median converted / (native x rate) = {statistics.median(errors):.3f}" if errors else ""))


if __name__ == "__main__":
    main()
