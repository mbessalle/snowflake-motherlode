-- Stop everything that can keep consuming credits. Safe to run more than once.
-- NOT YET RUN against an account.

USE ROLE MOTHERLODE_PIPELINE;

-- Nothing in this project is billed while idle: there is no search service, and the task and the
-- warehouse are suspended here.
ALTER TASK IF EXISTS MOTHERLODE.CORE.PROCESS_NEW_ANNOUNCEMENTS SUSPEND;
ALTER WAREHOUSE MOTHERLODE_XS SUSPEND;

-- To remove Cortex access as well (the kill switch), as ACCOUNTADMIN:
-- REVOKE USE AI FUNCTIONS ON ACCOUNT FROM ROLE MOTHERLODE_PIPELINE;
-- REVOKE DATABASE ROLE SNOWFLAKE.CORTEX_USER FROM ROLE MOTHERLODE_PIPELINE;
