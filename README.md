# Mining M&A announcements → a checked deal table, inside Snowflake

[![CI](https://github.com/mbessalle/snowflake-motherlode/actions/workflows/ci.yml/badge.svg)](https://github.com/mbessalle/snowflake-motherlode/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A rebuild of [motherlode-data-pipeline](https://github.com/mbessalle/motherlode-data-pipeline) on Snowflake.
Deal announcements land on a stage, Cortex AI functions read them, Snowpark Python holds the pipeline, code
checks every extracted number against the announcement before it is written, and a Cortex Agent answers
questions over the result.

> **Status: everything except the model calls runs on Snowflake; no Cortex AI function has run yet.**
> On a trial account, the pipeline's Snowpark code has written 33 deals to the deal table from extractions
> saved by the original pipeline, with validation, retry and the review queue all running in Snowflake.
> The stored procedure, the task, the semantic view and the agent object exist. The Cortex AI functions are
> still refused with "not available for trial accounts" (a trial needs an accepted payment card), so there
> are no Cortex extraction, screening or agent results yet. [What was and was not tested](#status) is listed
> below and is updated as each part runs.

## Architecture

```mermaid
flowchart TD
    D(["Announcements<br/>HTML, PDF or saved text"]) -->|"upload from outside:<br/>a trial has no external network access"| S[("Stage with a directory table<br/>RAW.ANNOUNCEMENTS")]
    S --> P["AI_PARSE_DOCUMENT<br/>file to text"]
    P --> T{{"Screening<br/>AI_COMPLETE: project deal? which project?<br/>AI_CLASSIFY: deal type, continent"}}
    T -->|"not a project deal,<br/>or project name not in the text"| X(["stop"])
    T --> E{{"Extraction<br/>AI_COMPLETE with the original prompt<br/>or AI_EXTRACT, one question per field"}}
    E --> V{"Validation<br/>Python, no model"}
    V -->|"unsupported, first attempt"| E
    V -->|"every number supported"| DEALS[("CORE.DEALS")]
    V -->|"still unsupported"| Q[("CORE.REVIEW_QUEUE<br/>values withheld")]
    Q -->|"the supported values"| DEALS
    DEALS --> SV["Semantic view"]
    P --> CS["Cortex Search service"]
    SV --> A{{"Cortex Agent<br/>DEAL_ANALYST"}}
    CS --> A
    A --> R(["REST API client"])

    classDef llm fill:#5b4bdb,stroke:#2f2496,color:#ffffff
    classDef stop fill:#f3f3f3,stroke:#999999,color:#333333
    class T,E,A llm
    class X stop
```

## What each Snowflake service does here

| Service | Its job in this project | Where |
|---|---|---|
| Stage, directory table, stream | Announcements land as files; the stream tells the pipeline which are new | [`sql/01_objects.sql`](sql/01_objects.sql) |
| `AI_PARSE_DOCUMENT` | Turns an HTML or PDF announcement into the text everything else reads | [`motherlode/cortex.py`](motherlode/cortex.py) |
| `AI_COMPLETE` | Screening (project deal or not, which project) and extraction with the original pipeline's prompt | [`motherlode/cortex.py`](motherlode/cortex.py) |
| `AI_CLASSIFY`, `AI_FILTER` | Deal type and continent from fixed lists; a second way to screen, for comparison | [`motherlode/cortex.py`](motherlode/cortex.py) |
| `AI_EXTRACT` | Document extraction with one question per field, measured against `AI_COMPLETE` on the same cases | [`motherlode/cortex.py`](motherlode/cortex.py) |
| Snowpark Python, stored procedure, task | The pipeline code; the procedure runs it inside Snowflake when new files arrive | [`motherlode/pipeline.py`](motherlode/pipeline.py), [`sql/03_pipeline_procedure.sql`](sql/03_pipeline_procedure.sql) |
| Cortex Search | Retrieval over the announcement texts | [`sql/04_search_semantic_view_agent.sql`](sql/04_search_semantic_view_agent.sql) |
| Semantic view | Tells the agent what the deal table's columns mean | same file |
| Cortex Agent | Answers questions using the semantic view and the search service; called over REST | same file, [`scripts/agent.py`](scripts/agent.py) |
| Resource monitor, budget, one AI role | Spending controls | [`sql/00_account_guardrails.sql`](sql/00_account_guardrails.sql) |

A note on names: Snowflake's Document AI product (its UI and the `!PREDICT` method) was
[decommissioned on 16 March 2026](https://docs.snowflake.com/en/release-notes/bcr-bundles/un-bundled/bcr-2156).
Document extraction is now `AI_EXTRACT`, with `AI_PARSE_DOCUMENT` for text and layout, so those are what
this project uses.

## The model extracts, code decides

This is carried over unchanged from the original pipeline, and it is the part that does not depend on which
model or platform runs the extraction.

- [`motherlode/validate.py`](motherlode/validate.py) gives every extracted number a status: written in the
  announcement, derived from written amounts by a rule (a sum of tranches, a total split across projects,
  shares × price), or unsupported. Only unsupported values block a write.
- [`motherlode/process.py`](motherlode/process.py) gives an unsupported value one retry that names it. If it
  is still unsupported it is withheld and queued for a person, and the rest of the record is stored. A
  record that breaks the [Pydantic schema](motherlode/schema.py) is not written at all.
- The field list lives in one place, the prompt. The table DDL, the insert statement and the `AI_EXTRACT`
  questions are generated from it ([`motherlode/fields.py`](motherlode/fields.py),
  [`motherlode/ddl.py`](motherlode/ddl.py)), and tests fail if any of them drift.
- Every model reply is kept in `CORE.EXTRACTION_ATTEMPTS` with its validation report.

## Evals

**Extraction.** [`evals/score.py`](evals/score.py) scores saved extractions on the original pipeline's 33
real press releases ([`evals/cases.jsonl`](evals/cases.jsonl)): how many extracted numbers the announcement
supports, and agreement with an analyst's hand-entered values.

The only numbers measured so far are the baseline. Pointed at the original pipeline's saved `gpt-6.1-terra`
replies, the scorer reproduces what that repository published, which shows the scorer and the ported
validator behave as the originals do:

| | First attempt | After the corrected retry |
|---|---|---|
| Extracted numbers supported by the article | 122 of 126 (97%) | 123 of 126 (98%) |
| Records with every number supported | 30 of 33 | 31 of 33 |
| Agreement with the analyst, where both have a value | | 83 of 108 (77%) |

These are OpenAI results from the original pipeline, not Snowflake results. The Snowflake rows
(`AI_COMPLETE` and `AI_EXTRACT` on the same cases) will be added when they have been run.

**Screening.** 43 labelled announcements ([`evals/screening_cases.jsonl`](evals/screening_cases.jsonl)).
Only 3 of them are not project deals, so this set says little about false positives until more negatives
are labelled.

**The same cases, replayed through Snowflake.** [`scripts/replay_baseline.py`](scripts/replay_baseline.py)
feeds those saved replies into the pipeline code running against the Snowflake account, in place of the
model call. Measured on 16 November 2026 and read back from the tables:

| | Count |
|---|---|
| Deals written to `CORE.DEALS` | 33 |
| Complete (every number supported) | 31 |
| Partial (unsupported values withheld) | 2 |
| Second attempts recorded in `CORE.EXTRACTION_ATTEMPTS` | 3 |
| Entries in `CORE.REVIEW_QUEUE` | 2 |

These match the scorer's numbers above (31 of 33 records fully supported, 3 retried), which is the point:
the validation, retry, quarantine and storage path behaves on Snowflake as it does offline. The rows are
marked `REPLAY gpt-6.1-terra`. They are not Cortex output.

**Agent.** [`evals/agent_questions.jsonl`](evals/agent_questions.jsonl) holds 10 questions, each with a SQL
query that computes the true answer from the deal table. An answer passes only if it contains that number.

## Documents

[`scripts/fetch_documents.py`](scripts/fetch_documents.py) made one plain request per case to the public
press-release URL. Of 43 cases, 14 were downloaded as published (13 HTML, 1 PDF). The other 29 sites
refused the request, timed out or served a page without the headline; for those, the text saved by the
original pipeline is used as a `.txt` file. Nothing was done to get around a refusal.
[`data/manifest.jsonl`](data/manifest.jsonl) records which is which. The documents themselves are other
people's press releases and are not committed.

## Cost

A trial account's resource monitor covers warehouses only, and a budget on AI services notifies without
stopping anything. So the cap is enforced in code: [`motherlode/cost.py`](motherlode/cost.py) estimates a
step from Snowflake's published rates (Service Consumption Table effective 12 November 2026) and
`SpendGuard.authorise` refuses to start a step that would pass the cap, from a local ledger.

Estimate from the documents on disk (`python scripts/estimate_cost.py`), not a measurement:

| Step | Estimated USD of trial balance |
|---|---|
| `AI_PARSE_DOCUMENT`, layout mode, 14 files, about 106 pages | 0.78 |
| Screening, `AI_COMPLETE` claude-haiku-4-5, 43 documents | 0.08 |
| Deal type and continent, `AI_CLASSIFY`, 43 documents | 0.30 |
| Screening comparison, `AI_FILTER`, 43 documents | 0.15 |
| Extraction, `AI_COMPLETE` claude-sonnet-5, 33 cases, one pass | 0.83 |
| Extraction, `AI_EXTRACT`, 33 cases, one pass | 1.73 |
| **One full pass** | **3.86** |
| Warehouse, extra small, per hour running | 2.60 |

Agent runs, the search service and repeated passes come on top. Measured spend will replace this table.

## Status

Tested, offline, in CI (116 tests, no account or network needed):

- The validator and schema, with the original pipeline's tests.
- The extract, validate, retry, quarantine loop, at every exit.
- Building each Cortex statement and reading each documented reply shape.
- The Snowpark steps end to end against a stand-in session that records every statement and its parameters.
- The spending guard, the generated DDL, the agent answer check and the eval scorer.
- Locally only (it needs the original pipeline's cache): the scorer reproduces the published baseline.

Run on a Snowflake trial account (Standard edition, AWS EU Frankfurt) on 16 November 2026:

- [`sql/00_account_guardrails.sql`](sql/00_account_guardrails.sql): every statement succeeded. Read back:
  all three warehouses are under the 20-credit resource monitor with a 60 second auto-suspend.
- [`sql/01_objects.sql`](sql/01_objects.sql) and [`sql/02_deals_table.sql`](sql/02_deals_table.sql): the
  stage, the stream and the tables exist and are owned by the pipeline role. Two mistakes in the first
  version were found by running it and fixed (a `GRANT` on three schemas in one statement; an `&` in a
  comment that the Snowflake CLI read as a variable).
- Key-pair login through Snowpark, and the upload of the 43 documents to the stage.
- **The pipeline after the model call**, through [`scripts/replay_baseline.py`](scripts/replay_baseline.py):
  `DOCUMENT_TEXT`, `EXTRACTION_ATTEMPTS`, `DEALS` and `REVIEW_QUEUE` are written by the Snowpark code with
  bind parameters, `PARSE_JSON` and one typed cast per column. Counts are in [Evals](#evals).
- **The stored procedure**: the package is uploaded as a zip, `PROCESS_NEW_ANNOUNCEMENTS` is created with
  Python 3.12 and `pydantic` from Snowflake's package channel, and a call with a limit of 0 documents
  imports the package, runs and writes its summary to `RUN_LOG`. The **task** is created and suspended,
  and the stage stream reports the uploaded files.
- **The semantic view** is created and answers a `SEMANTIC_VIEW(...)` query with metrics by dimension.
  **The agent object** `DEAL_ANALYST` is created from its specification.
- The 10 agent eval queries all run and return values from the deal table.
- [`scripts/smoke_test.py`](scripts/smoke_test.py): `AI_COMPLETE`, `AI_CLASSIFY`, `AI_FILTER`, `AI_EXTRACT`,
  `AI_PARSE_DOCUMENT` and `AI_COUNT_TOKENS` were each refused with error 399258, "is not available for
  trial accounts". Creating the **Cortex Search service** is refused the same way, because it embeds text
  (`EMBED_TEXT_768`). This matches the documentation: AI features on a trial need a payment card on file.

Not tested yet, all waiting on the AI features being enabled:

- Every statement in [`motherlode/cortex.py`](motherlode/cortex.py): whether named arguments accept bind
  parameters as written, and whether `AI_PARSE_DOCUMENT` accepts HTML files. The refusal above came before
  Snowflake looked at the arguments.
- Extraction and screening by Cortex: no `AI_COMPLETE` or `AI_EXTRACT` result has been measured. The
  `CONTINENT` and `DEAL_CATEGORY` columns are empty because they come from `AI_CLASSIFY`.
- The search service, and running the agent (only creating the object was checked).
- The REST call in [`scripts/agent.py`](scripts/agent.py): the authentication header and the response shape.
- `METERING_HISTORY` column names in [`scripts/spend_report.py`](scripts/spend_report.py).

[`scripts/smoke_test.py`](scripts/smoke_test.py) is the first thing to run on an account: one small call
per service, printing Snowflake's own message for each.

Not built: the optional ML model, Snowflake's native agent evaluations, and automatic discovery of new
announcements (the original pipeline does that outside Snowflake).

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install ".[dev]"
pytest                               # 116 tests, offline
ruff check .
python scripts/estimate_cost.py      # needs data/documents; see scripts/fetch_documents.py
```

With an account (untested, see Status): run `sql/00` to `sql/02`, copy `.env.example` to `.env`, then

```bash
pip install ".[snowflake]"
python scripts/smoke_test.py
python scripts/run_pipeline.py upload
python scripts/run_pipeline.py run --extractor claude-sonnet-5 --limit 3
python scripts/run_pipeline.py export --extractor claude-sonnet-5
```

## Layout

| Path | What it holds |
|---|---|
| [`motherlode/validate.py`](motherlode/validate.py), [`schema.py`](motherlode/schema.py), [`prompts.py`](motherlode/prompts.py) | Byte-identical copies from the original pipeline |
| [`motherlode/process.py`](motherlode/process.py) | The extract, validate, retry, quarantine loop |
| [`motherlode/cortex.py`](motherlode/cortex.py) | Cortex statements and reply parsing |
| [`motherlode/pipeline.py`](motherlode/pipeline.py) | The Snowpark steps and the stored procedure's handler |
| [`motherlode/cost.py`](motherlode/cost.py) | Published rates, estimates and the spending guard |
| [`motherlode/fields.py`](motherlode/fields.py), [`ddl.py`](motherlode/ddl.py) | The field list and what is generated from it |
| [`sql/`](sql/) | Guardrails, objects, the deal table, the procedure and task, search, semantic view and agent, teardown |
| [`scripts/`](scripts/) | Fetch documents, estimate cost, smoke test, run the pipeline, spend report, agent client |
| [`evals/`](evals/) | Cases, the scorer and the agent question set |
| [`tests/`](tests/) | Offline tests |
