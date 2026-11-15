-- The pipeline as a Snowpark Python stored procedure, and a task that runs it when new files arrive.
-- NOT YET RUN against an account.
--
-- Before creating the procedure, create the CODE stage below and upload the package as a zip so the
-- procedure can import it:
--     zip -r motherlode.zip motherlode -x '*__pycache__*'
--     snow stage copy motherlode.zip @MOTHERLODE.CORE.CODE --overwrite

USE ROLE MOTHERLODE_PIPELINE;
USE WAREHOUSE MOTHERLODE_XS;

CREATE STAGE IF NOT EXISTS MOTHERLODE.CORE.CODE COMMENT = 'The motherlode Python package, as a zip.';

CREATE OR REPLACE PROCEDURE MOTHERLODE.CORE.PROCESS_NEW_ANNOUNCEMENTS(
    EXTRACTOR VARCHAR, SCREEN_MODEL VARCHAR, MAX_DOCUMENTS NUMBER)
  RETURNS VARCHAR
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic')
  IMPORTS = ('@MOTHERLODE.CORE.CODE/motherlode.zip')
  HANDLER = 'motherlode.pipeline.process_new_announcements'
  COMMENT = 'Parse, screen, extract and validate announcements that are registered but not yet processed.';

-- The task only runs when the stage stream has new files, and at most 5 documents per run.
-- It is created suspended and stays suspended on the trial: run it by hand with EXECUTE TASK.
CREATE OR REPLACE TASK MOTHERLODE.CORE.PROCESS_NEW_ANNOUNCEMENTS
  WAREHOUSE = MOTHERLODE_XS
  SCHEDULE = '60 minutes'
  COMMENT = 'Processes new files on the ANNOUNCEMENTS stage.'
  WHEN SYSTEM$STREAM_HAS_DATA('MOTHERLODE.RAW.NEW_ANNOUNCEMENTS')
AS
BEGIN
  -- Reading the stream in a DML statement advances it, so each new file is registered once.
  INSERT INTO MOTHERLODE.RAW.DOCUMENTS (DOC_ID, FILE_NAME, OBTAINED)
    SELECT SPLIT_PART(RELATIVE_PATH, '.', 1), RELATIVE_PATH, 'stage'
    FROM MOTHERLODE.RAW.NEW_ANNOUNCEMENTS
    WHERE METADATA$ACTION = 'INSERT'
      AND SPLIT_PART(RELATIVE_PATH, '.', 1) NOT IN (SELECT DOC_ID FROM MOTHERLODE.RAW.DOCUMENTS);
  CALL MOTHERLODE.CORE.PROCESS_NEW_ANNOUNCEMENTS('claude-sonnet-5', 'claude-haiku-4-5', 5);
END;
