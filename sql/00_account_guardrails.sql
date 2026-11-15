-- Run once, first, as ACCOUNTADMIN, before anything that can consume credits.
-- NOT YET RUN against an account. Written from the Snowflake documentation as read on 16 Nov 2026.
--
-- What each control does and does not do:
--   resource monitor   suspends the warehouse at a credit quota. Covers warehouses only, not AI features.
--   account budget     notifies when spending is on track to pass the limit. Does not stop anything.
--   one AI role        the only role allowed to call Cortex. Revoking its two grants stops AI spend at once.
--   motherlode/cost.py refuses a paid step before it runs. This is the cap that actually holds for AI spend.

USE ROLE ACCOUNTADMIN;

-- 1. One extra-small warehouse that suspends after 60 seconds of idleness.
CREATE WAREHOUSE IF NOT EXISTS MOTHERLODE_XS
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  COMMENT = 'The only warehouse this project uses.';

-- 2. A hard stop for that warehouse: 20 credits for the whole trial, never reset.
CREATE OR REPLACE RESOURCE MONITOR MOTHERLODE_WAREHOUSE_CAP
  WITH CREDIT_QUOTA = 20
       FREQUENCY = NEVER
       START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 50 PERCENT DO NOTIFY
           ON 100 PERCENT DO SUSPEND
           ON 110 PERCENT DO SUSPEND_IMMEDIATE;
ALTER WAREHOUSE MOTHERLODE_XS SET RESOURCE_MONITOR = MOTHERLODE_WAREHOUSE_CAP;

-- The trial account came with two default warehouses (seen with SHOW WAREHOUSES on 16 Nov 2026), one of
-- them running with a 5 minute auto-suspend. Put both under the same cap and a 60 second auto-suspend.
ALTER WAREHOUSE COMPUTE_WH SET AUTO_SUSPEND = 60 RESOURCE_MONITOR = MOTHERLODE_WAREHOUSE_CAP;
ALTER WAREHOUSE SNOWFLAKE_LEARNING_WH SET AUTO_SUSPEND = 60 RESOURCE_MONITOR = MOTHERLODE_WAREHOUSE_CAP;

-- 3. The account budget, in credits per calendar month. It sends a notification; it does not block.
CALL SNOWFLAKE.LOCAL.ACCOUNT_ROOT_BUDGET!ACTIVATE();
CALL SNOWFLAKE.LOCAL.ACCOUNT_ROOT_BUDGET!SET_SPENDING_LIMIT(75);

-- 4. Only one role may use Cortex. PUBLIC has this by default, so take it away first.
--    ACCOUNTADMIN keeps access regardless, so the pipeline never runs as ACCOUNTADMIN.
REVOKE USE AI FUNCTIONS ON ACCOUNT FROM ROLE PUBLIC;
REVOKE DATABASE ROLE SNOWFLAKE.CORTEX_USER FROM ROLE PUBLIC;

CREATE ROLE IF NOT EXISTS MOTHERLODE_PIPELINE COMMENT = 'Runs the pipeline. The only role with Cortex access.';
GRANT USE AI FUNCTIONS ON ACCOUNT TO ROLE MOTHERLODE_PIPELINE;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE MOTHERLODE_PIPELINE;
GRANT USAGE, OPERATE ON WAREHOUSE MOTHERLODE_XS TO ROLE MOTHERLODE_PIPELINE;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE MOTHERLODE_PIPELINE;   -- to read the usage views
GRANT ROLE MOTHERLODE_PIPELINE TO ROLE SYSADMIN;
GRANT ROLE MOTHERLODE_PIPELINE TO USER MBESSALLE;

-- THE KILL SWITCH. Run these two statements to stop all Cortex spending by this project immediately:
-- REVOKE USE AI FUNCTIONS ON ACCOUNT FROM ROLE MOTHERLODE_PIPELINE;
-- REVOKE DATABASE ROLE SNOWFLAKE.CORTEX_USER FROM ROLE MOTHERLODE_PIPELINE;
