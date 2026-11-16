-- The review app: a schema for the evaluation results and the Streamlit app that shows them.
-- Run as ACCOUNTADMIN once; the pipeline role then loads the data (scripts/load_review_data.py) and deploys the
-- app (snow streamlit deploy, in streamlit_app/). No AI function is involved.

USE ROLE ACCOUNTADMIN;

CREATE SCHEMA IF NOT EXISTS MOTHERLODE.REVIEW
  COMMENT = 'Evaluation results of the three-tier cell check, and the Streamlit app that shows them.';
GRANT OWNERSHIP ON SCHEMA MOTHERLODE.REVIEW TO ROLE MOTHERLODE_PIPELINE COPY CURRENT GRANTS;
GRANT CREATE TABLE, CREATE VIEW, CREATE STAGE, CREATE STREAMLIT ON SCHEMA MOTHERLODE.REVIEW TO ROLE MOTHERLODE_PIPELINE;
