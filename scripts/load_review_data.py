"""Load the evaluation results into Snowflake, for the review app (streamlit_app/).

    python scripts/load_review_data.py

In the schema MOTHERLODE.REVIEW (sql/06_review_app.sql creates it), replaces one table per results file, each row one JSON object in a
VARIANT column, plus typed views the app reads:

  CASES          evals/pdf_cases.jsonl      the 236 cases: the analyst's row, the deal's PDFs, the checks
  CELL_CHECKS    evals/cell_checks.jsonl    one verdict per cell (tiers 1 and 2)
  JUDGE          evals/judge_results.jsonl  the decision model's answers (buyer, area, sums, percentages)
  PDF_CHOICE     evals/pdf_choice.jsonl     which PDF announces each deal, by the count rule, the model, the label
  PDF_SET        evals/pdf_set.jsonl        which PDFs give terms of each deal
  CELL_REVIEW    evals/cell_review_pilot.jsonl   the tier 3 verdicts of the first review run

The document texts the app quotes are already in MOTHERLODE.RAW.DOCUMENT_TEXT (DOC_ID = the PDF's file name
without .pdf). Warehouse time only; no AI function is called.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motherlode.connection import session  # noqa: E402

FILES = {"CASES": "pdf_cases.jsonl", "CELL_CHECKS": "cell_checks.jsonl", "JUDGE": "judge_results.jsonl",
         "PDF_CHOICE": "pdf_choice.jsonl", "PDF_SET": "pdf_set.jsonl", "CELL_REVIEW": "cell_review_pilot.jsonl"}

VIEWS = """
CREATE OR REPLACE VIEW MOTHERLODE.REVIEW.V_CASES AS
SELECT DATA:row_id::NUMBER AS ROW_ID, DATA:id::VARCHAR AS CASE_ID, DATA:project_name::VARCHAR AS PROJECT,
       DATA:analyst:owner_name::VARCHAR AS BUYER, DATA:analyst:owner_ticker::VARCHAR AS TICKER,
       DATA:analyst:news_date::VARCHAR AS NEWS_DATE, DATA:file::VARCHAR AS FILE,
       COALESCE(DATA:files, ARRAY_CONSTRUCT(DATA:file)) AS FILES, DATA:chosen_by::VARCHAR AS CHOSEN_BY,
       DATA:announcement_found::BOOLEAN AS ANNOUNCEMENT_FOUND, DATA:analyst AS ANALYST
FROM MOTHERLODE.REVIEW.CASES;

CREATE OR REPLACE VIEW MOTHERLODE.REVIEW.V_CELLS AS
SELECT c.DATA:row_id::NUMBER AS ROW_ID, c.DATA:case_id::VARCHAR AS CASE_ID, c.DATA:field::VARCHAR AS FIELD,
       c.DATA:value::FLOAT AS VALUE, c.DATA:verdict::VARCHAR AS VERDICT, c.DATA:p_confirmed::FLOAT AS P_CONFIRMED,
       c.DATA:proposal::VARCHAR AS PROPOSAL, c.DATA:p_proposal::FLOAT AS P_PROPOSAL, c.DATA:derived::VARCHAR AS DERIVED,
       c.DATA:escalate::BOOLEAN AS ESCALATE, c.DATA:found_in AS FOUND_IN,
       r.DATA:true_value::VARCHAR AS TIER3_VALUE, r.DATA:analyst_right::BOOLEAN AS TIER3_ANALYST_RIGHT,
       r.DATA:proposal_right::BOOLEAN AS TIER3_PROPOSAL_RIGHT
FROM MOTHERLODE.REVIEW.CELL_CHECKS c
LEFT JOIN MOTHERLODE.REVIEW.CELL_REVIEW r
  ON r.DATA:case_id::VARCHAR = c.DATA:case_id::VARCHAR AND r.DATA:field::VARCHAR = c.DATA:field::VARCHAR;

CREATE OR REPLACE VIEW MOTHERLODE.REVIEW.V_BUYER AS
SELECT DATA:row_id::NUMBER AS ROW_ID, DATA:answer:choice::VARCHAR AS ROLE,
       DATA:answer:probabilities:buyer::FLOAT AS P_BUYER
FROM MOTHERLODE.REVIEW.JUDGE WHERE DATA:question::VARCHAR = 'buyer_role';
"""


def main() -> None:
    s = session()          # the schema itself comes from sql/06_review_app.sql (run as ACCOUNTADMIN)
    s.use_schema("MOTHERLODE.REVIEW")       # save_as_table's overwrite needs a current schema
    for table, name in FILES.items():
        rows = [[line] for line in (ROOT / "evals" / name).read_text().splitlines() if line.strip()]
        df = s.create_dataframe(rows, schema=["RAW"]).select_expr("PARSE_JSON(RAW) AS DATA")
        df.write.mode("overwrite").save_as_table(f"MOTHERLODE.REVIEW.{table}")
        count = s.sql(f"SELECT COUNT(*) FROM MOTHERLODE.REVIEW.{table}").collect()[0][0]
        print(f"MOTHERLODE.REVIEW.{table}: {count} rows (file has {len(rows)})")
    for statement in filter(str.strip, VIEWS.split(";")):
        s.sql(statement).collect()
    print(s.sql("SELECT COUNT(*), COUNT_IF(ESCALATE) FROM MOTHERLODE.REVIEW.V_CELLS").collect())
    # sanity: every cell's case is a case, and the views read typed values
    json.dumps([r.as_dict() for r in s.sql("SELECT * FROM MOTHERLODE.REVIEW.V_CELLS LIMIT 1").collect()], default=str)


if __name__ == "__main__":
    main()
