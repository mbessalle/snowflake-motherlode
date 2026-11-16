-- Retrieval without embeddings: a hierarchical index the agent walks down with exact lookups.
--
--   level 0  FIND_DEALS(term)                  which announcement? (the deal table, also reachable through the semantic view)
--            FIND_PROJECTS(term)               which project? (the source PDFs are filed by project)
--            PROJECT_DOCUMENTS(project)        which of its documents? (pages, sections, how each starts)
--   level 1  DOCUMENT_OUTLINE(doc_id)          which section? (number, heading, size and first sentence of each)
--   level 2  READ_SECTION(doc_id, section_no)  the text of that section
--            FIND_IN_DOCUMENT(doc_id, term)    which sections of one document contain an exact word or number
--
-- Everything here is plain SQL and Snowpark Python. It needs no Cortex function, so it also works on a
-- trial account where the AI features are switched off. Run after sql/03 (it reuses the code zip).

USE ROLE MOTHERLODE_PIPELINE;
USE WAREHOUSE MOTHERLODE_XS;

CREATE TABLE IF NOT EXISTS MOTHERLODE.RAW.DOCUMENT_SECTIONS (
    DOC_ID          VARCHAR NOT NULL,
    SECTION_NO      NUMBER NOT NULL,
    HEADING         VARCHAR COMMENT 'The section heading, or its first words when the document gives none.',
    FIRST_SENTENCE  VARCHAR,
    TEXT            VARCHAR NOT NULL,
    CHARS           NUMBER,
    CONSTRAINT SECTIONS_PK PRIMARY KEY (DOC_ID, SECTION_NO)
)
COMMENT = 'Each announcement cut into sections by motherlode/outline.py. Level 1 and 2 of the index.';

-- Builds the sections inside Snowflake from RAW.DOCUMENT_TEXT. No model call.
CREATE OR REPLACE PROCEDURE MOTHERLODE.CORE.BUILD_OUTLINES()
  RETURNS VARCHAR
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic')
  IMPORTS = ('@MOTHERLODE.CORE.CODE/motherlode.zip')
  HANDLER = 'motherlode.pipeline.build_outlines'
  COMMENT = 'Cut every document into sections and rewrite RAW.DOCUMENT_SECTIONS.';

-- Level 0. One line per deal whose project, buyer or headline contains the term.
CREATE OR REPLACE FUNCTION MOTHERLODE.CORE.FIND_DEALS(TERM VARCHAR)
  RETURNS VARCHAR
  COMMENT = 'Deals whose project, buyer or headline contains the term. One line each: doc id | project | buyer | deal type | date.'
AS
$$
  SELECT COALESCE(LISTAGG(d.DOC_ID || ' | ' || COALESCE(d.PROJECT_NAME, '?') || ' | ' || COALESCE(d.OWNER_NAME, '?') || ' | '
                          || COALESCE(d.DEAL_TYPE, '?') || ' | ' || COALESCE(TO_VARCHAR(d.NEWS_DATE), '?'), '\n')
                  WITHIN GROUP (ORDER BY d.NEWS_DATE DESC), 'No deal matches.')
  FROM MOTHERLODE.CORE.DEALS d
  LEFT JOIN MOTHERLODE.RAW.DOCUMENTS doc ON doc.DOC_ID = d.DOC_ID
  WHERE CONTAINS(LOWER(COALESCE(d.PROJECT_NAME, '') || ' ' || COALESCE(d.OWNER_NAME, '') || ' ' || COALESCE(doc.TITLE, '')), LOWER(TERM))
$$;

-- Level 0 for the source PDFs, which are filed by project: which projects match a term?
CREATE OR REPLACE FUNCTION MOTHERLODE.CORE.FIND_PROJECTS(TERM VARCHAR)
  RETURNS VARCHAR
  COMMENT = 'Projects whose name contains the term, one per line: project | number of documents | total pages.'
AS
$$
  SELECT COALESCE(LISTAGG(PROJECT || ' | ' || DOCS || ' documents | ' || PAGES || ' pages', '\n') WITHIN GROUP (ORDER BY PROJECT),
                  'No project matches.')
  FROM (SELECT d.PROJECT, COUNT(*) AS DOCS, COALESCE(SUM(d.PAGES), 0) AS PAGES
        FROM MOTHERLODE.RAW.DOCUMENTS d
        WHERE d.PROJECT IS NOT NULL AND CONTAINS(LOWER(d.PROJECT), LOWER(REPLACE(TERM, ' ', '_')))
        GROUP BY d.PROJECT)
$$;

-- Between project and outline: the documents of one project, so the reader can pick the one worth opening.
CREATE OR REPLACE FUNCTION MOTHERLODE.CORE.PROJECT_DOCUMENTS(PROJECT_NAME VARCHAR)
  RETURNS VARCHAR
  COMMENT = 'The documents of one project, one per line: doc id | pages | sections | digital or mixed | how the document starts.'
AS
$$
  SELECT COALESCE(LISTAGG(d.DOC_ID || ' | ' || COALESCE(d.PAGES, 0) || ' pages | ' || COALESCE(s.SECTIONS, 0) || ' sections | '
                          || COALESCE(d.KIND, '?') || ' | ' || COALESCE(LEFT(s.OPENING, 160), '(no text layer)'), '\n')
                  WITHIN GROUP (ORDER BY d.DOC_ID), 'No such project.')
  FROM MOTHERLODE.RAW.DOCUMENTS d
  LEFT JOIN (SELECT DOC_ID, COUNT(*) AS SECTIONS, MAX(CASE WHEN SECTION_NO = 1 THEN FIRST_SENTENCE END) AS OPENING
             FROM MOTHERLODE.RAW.DOCUMENT_SECTIONS GROUP BY DOC_ID) s ON s.DOC_ID = d.DOC_ID
  WHERE LOWER(d.PROJECT) = LOWER(REPLACE(PROJECT_NAME, ' ', '_'))
$$;

-- Level 1. The table of contents of one announcement.
CREATE OR REPLACE FUNCTION MOTHERLODE.CORE.DOCUMENT_OUTLINE(ID VARCHAR)
  RETURNS VARCHAR
  COMMENT = 'The outline of one announcement: section number, heading, size and first sentence, one line per section.'
AS
$$
  SELECT COALESCE(LISTAGG(s.SECTION_NO || '. ' || s.HEADING || ' (' || s.CHARS || ' chars): ' || s.FIRST_SENTENCE, '\n')
                  WITHIN GROUP (ORDER BY s.SECTION_NO), 'No such document.')
  FROM MOTHERLODE.RAW.DOCUMENT_SECTIONS s
  WHERE s.DOC_ID = ID
$$;

-- Level 2. One section.
CREATE OR REPLACE FUNCTION MOTHERLODE.CORE.READ_SECTION(ID VARCHAR, SECTION NUMBER)
  RETURNS VARCHAR
  COMMENT = 'The full text of one section of one announcement.'
AS
$$
  SELECT COALESCE(MAX(s.TEXT), 'No such section.')
  FROM MOTHERLODE.RAW.DOCUMENT_SECTIONS s
  WHERE s.DOC_ID = ID AND s.SECTION_NO = SECTION
$$;

-- A shortcut inside one document: where does an exact word or number occur?
CREATE OR REPLACE FUNCTION MOTHERLODE.CORE.FIND_IN_DOCUMENT(ID VARCHAR, TERM VARCHAR)
  RETURNS VARCHAR
  COMMENT = 'The sections of one announcement that contain an exact word or number: section number and heading.'
AS
$$
  SELECT COALESCE(LISTAGG(s.SECTION_NO || '. ' || s.HEADING, '\n') WITHIN GROUP (ORDER BY s.SECTION_NO), 'Not found in this document.')
  FROM MOTHERLODE.RAW.DOCUMENT_SECTIONS s
  WHERE s.DOC_ID = ID AND CONTAINS(LOWER(s.TEXT), LOWER(TERM))
$$;
