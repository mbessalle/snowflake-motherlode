"""What has actually been spent, read from Snowflake's usage views, next to what the local ledger planned.

    python scripts/spend_report.py

The AI function view (columns as in Snowflake's cost-management guide) is at most a few minutes behind.
METERING_HISTORY covers warehouses and other services and can be hours behind; its column names here
(SERVICE_TYPE, CREDITS_USED, START_TIME) are from memory and unverified until this has run.

NOT YET RUN against an account.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motherlode import connection, cost  # noqa: E402

AI_FUNCTIONS = """
SELECT FUNCTION_NAME, MODEL_NAME, SUM(CREDITS) AS CREDITS, COUNT(DISTINCT QUERY_ID) AS QUERIES
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
GROUP BY 1, 2 ORDER BY CREDITS DESC"""
METERING = """
SELECT SERVICE_TYPE, SUM(CREDITS_USED) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY
GROUP BY 1 ORDER BY CREDITS DESC"""


def main() -> None:
    session = connection.session()
    print("AI functions (AI credits):")
    total_ai = 0.0
    for function, model, credits, queries in session.sql(AI_FUNCTIONS).collect():
        total_ai += float(credits or 0)
        print(f"  {function:<20} {model or '-':<24} {float(credits or 0):>9.4f} credits  {queries} queries")
    print(f"  total {total_ai:.4f} AI credits = about ${total_ai * cost.USD_PER_AI_CREDIT:.2f}\n")
    print("All metered services (credits):")
    for service, credits in session.sql(METERING).collect():
        print(f"  {service:<28} {float(credits or 0):>9.4f}")
    guard = connection.guard()
    print(f"\nLocal ledger: ${guard.spent_usd():.2f} planned or recorded, cap ${guard.cap_usd:.2f}")


if __name__ == "__main__":
    main()
