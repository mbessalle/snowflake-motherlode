"""First thing to run on a new account: one small call per service, to learn what the account really allows.

    python scripts/smoke_test.py [--file 38826.html] [--pdf 74543.pdf]

It answers the questions the documentation left open: are AI features on (a trial needs a card on file),
does Cortex Agents work on a trial, do the statements in motherlode/cortex.py bind parameters the way they
are written, which models can be called. Each check prints PASS or FAIL with Snowflake's own message.
Expected cost of the whole script: under 0.05 USD of trial balance (a few hundred tokens per call).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motherlode import connection, cortex  # noqa: E402
from motherlode.pipeline import STAGE  # noqa: E402

TEXT = ("Acme Gold Corp. has entered into an option agreement to acquire a 100% interest in the Big Onion property "
        "in British Columbia by paying $250,000 in cash. The vendor retains a 2% NSR royalty.")
MODEL = "claude-haiku-4-5"


def checks(file_name: str | None, pdf_name: str | None) -> list[tuple[str, str, list]]:
    questions = json.dumps({"cash": "What is the total cash payment?", "royalty": "What is the NSR royalty in percent?"})
    items = [
        ("session, role and region", "SELECT CURRENT_ROLE() || ' in ' || CURRENT_REGION()", []),
        ("models this role can call", "SHOW CORTEX BASE MODELS IN SCHEMA SNOWFLAKE.MODELS", []),
        ("AI_COMPLETE with bound parameters", cortex.COMPLETE_SQL, [MODEL, "Reply with the single word: ready"]),
        ("AI_CLASSIFY (continent)", cortex.CONTINENT_SQL, [TEXT]),
        ("AI_FILTER (is it a project deal)", cortex.FILTER_SQL, [cortex.IS_DEAL_PREDICATE, TEXT]),
        ("AI_EXTRACT on text", cortex.EXTRACT_TEXT_SQL, [TEXT, questions]),
        ("AI_COUNT_TOKENS", "SELECT AI_COUNT_TOKENS('ai_complete', ?, ?)", [MODEL, TEXT]),
        ("usage view is readable", "SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY", []),
        ("Cortex Agents: create an agent", "CREATE OR REPLACE AGENT MOTHERLODE.CORE.SMOKE_TEST_AGENT FROM SPECIFICATION "
         "$$\nmodels:\n  orchestration: auto\ninstructions:\n  response: \"Answer in one word.\"\n$$", []),
        ("Cortex Agents: drop it again", "DROP AGENT IF EXISTS MOTHERLODE.CORE.SMOKE_TEST_AGENT", []),
    ]
    for label, name in (("HTML", file_name), ("PDF", pdf_name)):
        if name:
            items.append((f"AI_PARSE_DOCUMENT on a staged {label} file", cortex.PARSE_SQL, [STAGE, name]))
            items.append((f"AI_EXTRACT on a staged {label} file", cortex.EXTRACT_FILE_SQL, [STAGE, name, questions]))
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", help="name of an HTML file already on the ANNOUNCEMENTS stage")
    parser.add_argument("--pdf", help="name of a PDF file already on the ANNOUNCEMENTS stage")
    args = parser.parse_args()
    connection.guard().authorise("smoke_test", 0.05)
    session, failed = connection.session(), 0
    for name, statement, params in checks(args.file, args.pdf):
        try:
            rows = session.sql(statement, params=params).collect()
            shown = f"{len(rows)} rows" if len(rows) != 1 else str(rows[0][0])[:160].replace("\n", " ")
            print(f"PASS  {name}: {shown}")
        except Exception as error:   # the message is the finding, so every check runs
            failed += 1
            print(f"FAIL  {name}: {str(error)[:300]}")
    print(f"\n{failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
