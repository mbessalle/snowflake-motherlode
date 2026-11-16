-- The question-answering layer: a semantic view over the deal table, and one Cortex Agent that uses it
-- together with the hierarchical index of sql/05_hierarchical_index.sql. Run after sql/05.
-- There is no embedding search here on purpose: the agent finds text by walking the index
-- (deal -> outline -> section), every step an exact lookup. See motherlode/outline.py.
--
-- Run on the trial account on 16 Nov 2026: both statements succeed. The agent has not answered a question
-- yet, because running it needs the AI features that a trial keeps off until a payment card is accepted.

USE ROLE MOTHERLODE_PIPELINE;
USE WAREHOUSE MOTHERLODE_XS;

-- 1. What the columns of the deal table mean, so questions in plain language become correct SQL.
CREATE OR REPLACE SEMANTIC VIEW MOTHERLODE.CORE.DEALS_SEMANTIC
  TABLES (
    deals AS MOTHERLODE.CORE.DEALS
      PRIMARY KEY (DOC_ID)
      WITH SYNONYMS ('transactions', 'project deals', 'mergers and acquisitions')
      COMMENT = 'One row per mineral project transaction. Every number was checked against its announcement.'
  )
  FACTS (
    deals.cash_payments AS CASH_PAYMENTS_VALUE COMMENT = 'Sum of all cash payments, in CASH_PAYMENTS_CURRENCY.',
    deals.exploration_commitment AS EXP_COMIT_VALUE COMMENT = 'Committed exploration spending, in EXP_COMIT_CURRENCY.',
    deals.shares_issued AS SHARES_AMOUNT COMMENT = 'Number of shares issued as payment.',
    deals.aggregate_value AS AGGREGATE_DEAL_VALUE COMMENT = 'Total of all payments, in AGGREGATE_DEAL_CURRENCY.',
    deals.interest_acquired AS INTEREST_ACQUIRED_PERCENT COMMENT = 'Interest acquired, in percent (0 to 100).',
    deals.nsr AS NSR_PERCENT COMMENT = 'Net smelter royalty in the deal, in percent.',
    deals.area AS PROJECT_AREA COMMENT = 'Project surface area, in PROJECT_AREA_UNIT.'
  )
  DIMENSIONS (
    deals.project AS PROJECT_NAME WITH SYNONYMS = ('property', 'asset') COMMENT = 'Name of the project.',
    deals.buyer AS OWNER_NAME WITH SYNONYMS = ('acquirer', 'owner', 'company') COMMENT = 'The company buying or optioning the project.',
    deals.commodity AS PRIMARY_COMMODITY COMMENT = 'Chemical symbol of the main commodity, for example Au or Cu.',
    deals.country AS PROJECT_COUNTRY COMMENT = 'Country of the project.',
    deals.state_or_province AS PROJECT_STATE_OR_PROVINCE COMMENT = 'State or province of the project.',
    deals.continent AS CONTINENT COMMENT = 'Continent of the project.',
    deals.deal_category AS DEAL_CATEGORY COMMENT = 'Deal type from a fixed list: acquisition or sale, option or earn-in, joint venture, merger or takeover, staking or claim purchase, other.',
    deals.cash_currency AS CASH_PAYMENTS_CURRENCY COMMENT = 'ISO code of the cash payments. Never add amounts in different currencies.',
    deals.announced_on AS NEWS_DATE COMMENT = 'Date of the announcement.',
    deals.announcement_title AS NEWS_TITLE COMMENT = 'Headline of the announcement.',
    deals.outcome AS OUTCOME COMMENT = 'stored, or stored_partial when unsupported values were withheld.'
  )
  METRICS (
    deals.deal_count AS COUNT(DOC_ID) COMMENT = 'Number of deals.',
    deals.total_cash AS SUM(CASH_PAYMENTS_VALUE) COMMENT = 'Total cash payments. Only meaningful within one currency.',
    deals.average_interest AS AVG(INTEREST_ACQUIRED_PERCENT) COMMENT = 'Average interest acquired, in percent.',
    deals.average_nsr AS AVG(NSR_PERCENT) COMMENT = 'Average net smelter royalty, in percent.'
  )
  COMMENT = 'Mining project transactions extracted from announcements.';

-- 2. The agent. The budget stops a single run; motherlode/cost.py caps the total.
CREATE OR REPLACE AGENT MOTHERLODE.CORE.DEAL_ANALYST
  COMMENT = 'Answers questions about mining project transactions from the deal table and the announcements.'
  FROM SPECIFICATION
  $$
  models:
    orchestration: auto

  orchestration:
    budget:
      seconds: 60
      tokens: 16000

  instructions:
    response: "Answer briefly. State the currency with every amount and never add amounts in different currencies. If the data does not contain the answer, say so."
    orchestration: "Use deal_table for counts, sums, averages, rankings and filters over deals. For wording, terms and context that are not columns, read the announcement the way a person uses a table of contents: find_deals to get the doc id (or find_projects, then project_documents to choose one document of a project), document_outline to see its sections, then read_section for only the section you need. Use find_in_document when you look for an exact word or number. Do not read every section."
    sample_questions:
      - question: "Which deals involved an option or earn-in for a gold project?"
      - question: "What are the payment terms of the South Falcon East option?"

  tools:
    - tool_spec:
        type: "cortex_analyst_text_to_sql"
        name: "deal_table"
        description: "Structured deal terms: buyer, project, commodity, country, payments, shares, interest acquired, royalty."
    - tool_spec:
        type: "generic"
        name: "find_deals"
        description: "Level 0 of the index. Lists deals whose project, buyer or headline contains a term, one per line: doc id | project | buyer | deal type | date."
        input_schema:
          type: "object"
          properties:
            term:
              type: "string"
              description: "A word or name to look for, for example a project or company name."
          required: ["term"]
    - tool_spec:
        type: "generic"
        name: "find_projects"
        description: "Level 0 for the source PDFs. Projects whose name contains a term, with their number of documents and pages."
        input_schema:
          type: "object"
          properties:
            term:
              type: "string"
              description: "A project name or part of one."
          required: ["term"]
    - tool_spec:
        type: "generic"
        name: "project_documents"
        description: "The documents of one project: doc id, pages, sections, whether it has a text layer, and how it starts. Use it to pick the one document worth opening."
        input_schema:
          type: "object"
          properties:
            project_name:
              type: "string"
              description: "The project name from find_projects."
          required: ["project_name"]
    - tool_spec:
        type: "generic"
        name: "document_outline"
        description: "Level 1 of the index. The outline of one announcement: section number, heading, size and first sentence of every section."
        input_schema:
          type: "object"
          properties:
            id:
              type: "string"
              description: "The doc id from find_deals or from the deal table."
          required: ["id"]
    - tool_spec:
        type: "generic"
        name: "read_section"
        description: "Level 2 of the index. The full text of one section of one announcement."
        input_schema:
          type: "object"
          properties:
            id:
              type: "string"
              description: "The doc id."
            section:
              type: "number"
              description: "The section number from document_outline."
          required: ["id", "section"]
    - tool_spec:
        type: "generic"
        name: "find_in_document"
        description: "The sections of one announcement that contain an exact word or number."
        input_schema:
          type: "object"
          properties:
            id:
              type: "string"
              description: "The doc id."
            term:
              type: "string"
              description: "The exact word or number to look for."
          required: ["id", "term"]

  tool_resources:
    deal_table:
      semantic_view: "MOTHERLODE.CORE.DEALS_SEMANTIC"
    find_deals:
      type: "function"
      identifier: "MOTHERLODE.CORE.FIND_DEALS"
      execution_environment:
        type: "warehouse"
        warehouse: "MOTHERLODE_XS"
    find_projects:
      type: "function"
      identifier: "MOTHERLODE.CORE.FIND_PROJECTS"
      execution_environment:
        type: "warehouse"
        warehouse: "MOTHERLODE_XS"
    project_documents:
      type: "function"
      identifier: "MOTHERLODE.CORE.PROJECT_DOCUMENTS"
      execution_environment:
        type: "warehouse"
        warehouse: "MOTHERLODE_XS"
    document_outline:
      type: "function"
      identifier: "MOTHERLODE.CORE.DOCUMENT_OUTLINE"
      execution_environment:
        type: "warehouse"
        warehouse: "MOTHERLODE_XS"
    read_section:
      type: "function"
      identifier: "MOTHERLODE.CORE.READ_SECTION"
      execution_environment:
        type: "warehouse"
        warehouse: "MOTHERLODE_XS"
    find_in_document:
      type: "function"
      identifier: "MOTHERLODE.CORE.FIND_IN_DOCUMENT"
      execution_environment:
        type: "warehouse"
        warehouse: "MOTHERLODE_XS"
  $$;
