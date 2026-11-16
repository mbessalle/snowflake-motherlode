-- Database, stage and tables. Run after 00_account_guardrails.sql, then run 02_deals_table.sql.
-- NOT YET RUN against an account.

USE ROLE SYSADMIN;

CREATE DATABASE IF NOT EXISTS MOTHERLODE COMMENT = 'Mining deal announcements to a structured deal table.';
CREATE SCHEMA IF NOT EXISTS MOTHERLODE.RAW  COMMENT = 'Announcement files and their text.';
CREATE SCHEMA IF NOT EXISTS MOTHERLODE.CORE COMMENT = 'Screening, extraction attempts, the deal table and the review queue.';
CREATE SCHEMA IF NOT EXISTS MOTHERLODE.EVAL COMMENT = 'Labelled cases and eval results.';

GRANT USAGE ON DATABASE MOTHERLODE TO ROLE MOTHERLODE_PIPELINE;
GRANT ALL ON SCHEMA MOTHERLODE.RAW TO ROLE MOTHERLODE_PIPELINE;
GRANT ALL ON SCHEMA MOTHERLODE.CORE TO ROLE MOTHERLODE_PIPELINE;
GRANT ALL ON SCHEMA MOTHERLODE.EVAL TO ROLE MOTHERLODE_PIPELINE;

USE ROLE MOTHERLODE_PIPELINE;
USE WAREHOUSE MOTHERLODE_XS;

-- Announcements land here. Cortex file functions read internal stages that use server-side encryption.
CREATE STAGE IF NOT EXISTS MOTHERLODE.RAW.ANNOUNCEMENTS
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  DIRECTORY = (ENABLE = TRUE)
  COMMENT = 'Deal announcements as files: HTML or PDF as published, or saved text.';

-- New files on the stage show up in this stream; the pipeline task reads it.
CREATE STREAM IF NOT EXISTS MOTHERLODE.RAW.NEW_ANNOUNCEMENTS ON STAGE MOTHERLODE.RAW.ANNOUNCEMENTS;

CREATE TABLE IF NOT EXISTS MOTHERLODE.RAW.DOCUMENTS (
    DOC_ID      VARCHAR NOT NULL PRIMARY KEY,
    FILE_NAME   VARCHAR NOT NULL COMMENT 'Relative path on the ANNOUNCEMENTS stage.',
    TITLE       VARCHAR,
    COMPANY     VARCHAR,
    SOURCE_URL  VARCHAR,
    OBTAINED    VARCHAR COMMENT 'downloaded (the page as published), saved_text (text kept by the original pipeline) or source_pdf.',
    LOADED_AT   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
-- Added for the source PDFs: the project a document belongs to, and what the text-layer check found.
ALTER TABLE MOTHERLODE.RAW.DOCUMENTS ADD COLUMN IF NOT EXISTS PROJECT VARCHAR COMMENT 'Project folder the document came from.';
ALTER TABLE MOTHERLODE.RAW.DOCUMENTS ADD COLUMN IF NOT EXISTS KIND VARCHAR COMMENT 'digital, mixed or scanned: whether the pages carry a text layer (scripts/inventory_pdfs.py).';
ALTER TABLE MOTHERLODE.RAW.DOCUMENTS ADD COLUMN IF NOT EXISTS PAGES NUMBER;
ALTER TABLE MOTHERLODE.RAW.DOCUMENTS ADD COLUMN IF NOT EXISTS PAGES_WITHOUT_TEXT NUMBER COMMENT 'Pages that are images only and need OCR.';

CREATE TABLE IF NOT EXISTS MOTHERLODE.RAW.DOCUMENT_TEXT (
    DOC_ID      VARCHAR NOT NULL PRIMARY KEY,
    TEXT        VARCHAR NOT NULL COMMENT 'What every number in the deal table is checked against.',
    PAGES       NUMBER,
    PARSER      VARCHAR COMMENT 'AI_PARSE_DOCUMENT LAYOUT, or saved_text when the file already is text.',
    PARSED_AT   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS MOTHERLODE.CORE.SCREENING (
    DOC_ID           VARCHAR NOT NULL,
    METHOD           VARCHAR NOT NULL COMMENT 'AI_COMPLETE with a model name, or AI_FILTER.',
    IS_PROJECT_DEAL  BOOLEAN,
    PROJECT_NAME     VARCHAR,
    PROJECT_NAMED_IN_TEXT BOOLEAN COMMENT 'Code check: the project name the model gave is written in the announcement.',
    REASON           VARCHAR,
    DEAL_CATEGORY    VARCHAR,
    CONTINENT        VARCHAR,
    SCREENED_AT      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT SCREENING_PK PRIMARY KEY (DOC_ID, METHOD)
);

-- Every model reply is kept with its validation report, so "why was this value not written?" is a query.
CREATE TABLE IF NOT EXISTS MOTHERLODE.CORE.EXTRACTION_ATTEMPTS (
    DOC_ID             VARCHAR NOT NULL,
    EXTRACTOR          VARCHAR NOT NULL,
    ATTEMPT            NUMBER(2) NOT NULL,
    EXTRACTION         VARIANT,
    REPORT             VARIANT COMMENT 'motherlode.validate report: status and evidence per checked field.',
    PROMPT_TOKENS      NUMBER,
    COMPLETION_TOKENS  NUMBER,
    CREATED_AT         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT ATTEMPTS_PK PRIMARY KEY (DOC_ID, EXTRACTOR, ATTEMPT)
);

CREATE TABLE IF NOT EXISTS MOTHERLODE.CORE.REVIEW_QUEUE (
    DOC_ID      VARCHAR NOT NULL,
    EXTRACTOR   VARCHAR NOT NULL,
    WITHHELD    VARIANT COMMENT 'Values the announcement does not support, removed before writing.',
    REPORT      VARIANT,
    QUEUED_AT   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS MOTHERLODE.CORE.RUN_LOG (
    RUN_ID      VARCHAR NOT NULL,
    DOC_ID      VARCHAR,
    STEP        VARCHAR NOT NULL,
    DETAIL      VARCHAR,
    LOGGED_AT   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
