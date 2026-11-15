"""Run everything after the model call on Snowflake, using extractions saved by the original pipeline.

    python scripts/replay_baseline.py [--cache DIR]

Why this exists: on a trial account the Cortex AI functions stay switched off until a payment card is
accepted. The rest of the pipeline does not need them. This script takes the 33 eval cases with the text and
the `gpt-6.1-terra` replies the original pipeline saved, and for each one runs, in Snowflake, the same code a
live run uses after the model answers: validate every number against the text, use the saved retry when
validation fails, withhold what stays unsupported, and write DOCUMENT_TEXT, EXTRACTION_ATTEMPTS, DEALS and
REVIEW_QUEUE. Rows are marked "REPLAY gpt-6.1-terra" so they are never mistaken for Cortex output.

It costs warehouse time only (no AI credits). At the end it checks the tables against the eval scorer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals import score  # noqa: E402
from motherlode import connection, cost, pipeline  # noqa: E402

DEFAULT_CACHE = Path.home() / "github-mbessalle" / "motherlode-data-pipeline" / "evals" / ".cache"
MODEL = "gpt-6.1-terra"
NAME = f"REPLAY {MODEL}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="the original pipeline's evals/.cache")
    args = parser.parse_args()
    texts, replies = args.cache / "texts", args.cache / "extractions" / MODEL
    connection.guard().authorise("replay baseline (warehouse only)", cost.warehouse_usd(0.25))
    session = connection.session()
    session.sql("DELETE FROM MOTHERLODE.CORE.REVIEW_QUEUE WHERE EXTRACTOR = ?", params=[NAME]).collect()
    outcomes: dict[str, int] = {}
    for case in score.load_cases():
        doc_id = case["id"]
        text = " ".join((texts / f"{doc_id}.txt").read_text().split())
        saved = [json.loads(p.read_text()) for p in (replies / "attempt1" / f"{doc_id}.json", replies / "attempt2" / f"{doc_id}.json")
                 if p.exists()]
        pipeline.parse(session, doc_id, f"{doc_id}.txt", saved_text=text)
        result = pipeline.extract_and_store(session, doc_id, text, {"project_name": case["project"]}, MODEL, replay=saved)
        outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
        print(f"{doc_id:>7}  {result.outcome:<15} attempts {result.attempts}  withheld {sorted(result.withheld)}")

    # The tables must agree with the scorer, which reads the same saved replies without Snowflake.
    expected = score.score(score.load_cases(), texts, replies, retry=True)
    counts = {name: session.sql(sql, params=[NAME]).collect()[0][0] for name, sql in {
        "deals": "SELECT COUNT(*) FROM MOTHERLODE.CORE.DEALS WHERE EXTRACTOR = ?",
        "deals_complete": "SELECT COUNT(*) FROM MOTHERLODE.CORE.DEALS WHERE EXTRACTOR = ? AND OUTCOME = 'stored'",
        "attempts": "SELECT COUNT(*) FROM MOTHERLODE.CORE.EXTRACTION_ATTEMPTS WHERE EXTRACTOR = ?",
        "retries": "SELECT COUNT(*) FROM MOTHERLODE.CORE.EXTRACTION_ATTEMPTS WHERE EXTRACTOR = ? AND ATTEMPT = 2",
        "review_queue": "SELECT COUNT(*) FROM MOTHERLODE.CORE.REVIEW_QUEUE WHERE EXTRACTOR = ?",
    }.items()}
    print("\noutcomes:", outcomes, "\ntables:  ", counts)
    checks = {
        "every record with all numbers supported is a complete deal": counts["deals_complete"] == expected["final"]["records_passed"],
        "one retry row per record that failed its first attempt": counts["retries"] == expected["retried"],
        "every other record is in the review queue": counts["review_queue"] == expected["records"] - expected["final"]["records_passed"],
    }
    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
