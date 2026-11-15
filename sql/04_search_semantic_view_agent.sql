-- The question-answering layer: Cortex Search over the announcement texts, a semantic view over the deal
-- table, and one Cortex Agent that can use both. Run after the pipeline has filled CORE.DEALS.
-- NOT YET RUN against an account. Syntax follows CREATE CORTEX SEARCH SERVICE, CREATE SEMANTIC VIEW and
-- CREATE AGENT in the Snowflake reference as read on 16 Nov 2026.

USE ROLE MOTHERLODE_PIPELINE;
USE WAREHOUSE MOTHERLODE_XS;

-- 1. Search over the announcements. Serving is billed for as long as the service exists (6.3 AI credits per
--    GB-month of indexed data; this corpus is well under 1 MB), so 90_teardown.sql drops it.
CREATE OR REPLACE CORTEX SEARCH SERVICE MOTHERLODE.CORE.ANNOUNCEMENT_SEARCH
  ON TEXT
  PRIMARY KEY (DOC_ID)
  ATTRIBUTES DOC_ID, TITLE, COMPANY
  WAREHOUSE = MOTHERLODE_XS
  TARGET_LAG = '1 day'
  COMMENT = 'Full text of the deal announcements.'
AS (
  SELECT t.DOC_ID, t.TEXT, d.TITLE, d.COMPANY, d.SOURCE_URL
  FROM MOTHERLODE.RAW.DOCUMENT_TEXT t
  JOIN MOTHERLODE.RAW.DOCUMENTS d ON d.DOC_ID = t.DOC_ID
);

-- 2. What the columns of the deal table mean, so questions in plain language become correct SQL.
CREATE OR REPLACE SEMANTIC VIEW MOTHERLODE.CORE.DEALS_SEMANTIC
  TABLES (
    deals AS MOTHERLODE.CORE.DEALS
      PRIMARY KEY (DOC_ID)
      WITH SYNONYMS ('transactions', 'project deals', 'M&A deals')
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

-- 3. The agent. The budget stops a single run; motherlode/cost.py caps the total.
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
    orchestration: "Use deal_table for counts, sums, averages, rankings and filters over deals. Use announcements for wording, terms and context that are not columns of the deal table."
    sample_questions:
      - question: "Which deals involved an option or earn-in for a gold project?"
      - question: "What royalty did the vendor keep in the Big Onion deal?"

  tools:
    - tool_spec:
        type: "cortex_analyst_text_to_sql"
        name: "deal_table"
        description: "Structured deal terms: buyer, project, commodity, country, payments, shares, interest acquired, royalty."
    - tool_spec:
        type: "cortex_search"
        name: "announcements"
        description: "Full text of the deal announcements."

  tool_resources:
    deal_table:
      semantic_view: "MOTHERLODE.CORE.DEALS_SEMANTIC"
    announcements:
      search_service: "MOTHERLODE.CORE.ANNOUNCEMENT_SEARCH"
      max_results: "4"
      title_column: "TITLE"
      id_column: "DOC_ID"
  $$;
