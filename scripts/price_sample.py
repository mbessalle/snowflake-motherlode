"""Choose a sample of deals whose stored share price can be checked against public daily closes.

    python scripts/price_sample.py OUT.json [N]

A deal qualifies when the analyst stored a share price, the buyer has an ASX ticker, the row has a news date,
and the decision model reads the analyst's company as the buyer in the case PDF (P >= 0.5), so the ticker is
the one whose shares were issued. The sample is drawn with a fixed seed, so it can be redrawn.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    out, n = Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 20
    cases = [json.loads(line) for line in (ROOT / "evals" / "pdf_cases.jsonl").read_text().splitlines()]
    buyer = {r["row_id"]: (r["answer"].get("probabilities") or {}).get("buyer", 0)
             for r in map(json.loads, (ROOT / "evals" / "judge_results.jsonl").read_text().splitlines())
             if r["question"] == "buyer_role"}
    pool = [c for c in cases if c["analyst"].get("share_price") and c["analyst"].get("news_date")
            and str(c["analyst"].get("owner_ticker", "")).upper().startswith("ASX") and buyer.get(c["row_id"], 0) >= 0.5]
    random.Random(20).shuffle(pool)
    sample = [{"row_id": c["row_id"], "case_id": c["id"], "ticker": str(c["analyst"]["owner_ticker"]).split(":")[-1].strip().upper(),
               "news_date": str(c["analyst"]["news_date"])[:10], "share_price": c["analyst"]["share_price"],
               "shares_amount": c["analyst"].get("shares_amount"), "shares_value": c["analyst"].get("shares_value")}
              for c in pool[:n]]
    out.write_text(json.dumps(sample, indent=1))
    print(f"{len(pool)} eligible; {len(sample)} drawn")
    for s in sample:
        print(s)


if __name__ == "__main__":
    main()
