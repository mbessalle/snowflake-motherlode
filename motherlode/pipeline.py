"""The pipeline steps as Snowpark Python: each takes a Snowpark session and works on the MOTHERLODE tables.

    register ─► parse ─► screen ─► extract + validate (+ retry) ─► DEALS / REVIEW_QUEUE

The same functions run from a laptop (scripts/run_pipeline.py, where the spending guard sits in front of them)
and inside Snowflake as the stored procedure PROCESS_NEW_ANNOUNCEMENTS (sql/03_pipeline_procedure.sql).
The decisions are not made here: routing is motherlode.process, checking is motherlode.validate, and the
statements and reply parsing are motherlode.cortex, all of which are tested offline.

NOT YET RUN against an account. `session` is only used through session.sql(text, params=[...]).collect().
"""
from __future__ import annotations

import json
import re
import uuid

from . import cortex, ddl
from .process import NO_DATA, Result, process

STAGE = "@MOTHERLODE.RAW.ANNOUNCEMENTS"
AI_EXTRACT = "AI_EXTRACT"


def _value(session, statement: str, params: list):
    rows = session.sql(statement, params=params).collect()
    return rows[0][0] if rows else None


def _run(session, statement: str, params: list | None = None) -> None:
    session.sql(statement, params=params or []).collect()


def log(session, run_id: str, doc_id: str | None, step: str, detail: str) -> None:
    _run(session, "INSERT INTO MOTHERLODE.CORE.RUN_LOG (RUN_ID, DOC_ID, STEP, DETAIL) VALUES (?, ?, ?, ?)",
         [run_id, doc_id, step, detail[:2000]])


def named_in(text: str, name: str | None) -> bool:
    """The project name the model gave must be written in the announcement (ignoring case and punctuation)."""
    def squash(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    return bool(name) and squash(name) in squash(text)


# --- steps ----------------------------------------------------------------------------------------------


def register(session, entries: list[dict]) -> int:
    """Record the documents of the manifest that are on the stage. Returns how many are new."""
    known = {row[0] for row in session.sql("SELECT DOC_ID FROM MOTHERLODE.RAW.DOCUMENTS").collect()}
    new = [e for e in entries if e.get("file") and e["id"] not in known]
    for e in new:
        _run(session, "INSERT INTO MOTHERLODE.RAW.DOCUMENTS (DOC_ID, FILE_NAME, TITLE, COMPANY, SOURCE_URL, OBTAINED) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
             [e["id"], e["file"], e.get("title"), e.get("company"), e.get("source_url"), e.get("obtained")])
    return len(new)


def parse(session, doc_id: str, file_name: str, saved_text: str | None = None) -> tuple[str, int]:
    """Text of one document. A file that already is text is stored as it is; HTML and PDF go through
    AI_PARSE_DOCUMENT in LAYOUT mode. Returns (text, pages)."""
    if saved_text is not None:
        text, pages, parser = saved_text, 0, "saved_text"
    else:
        text, pages = cortex.parse_document(_value(session, cortex.PARSE_SQL, [STAGE, file_name]))
        parser = "AI_PARSE_DOCUMENT LAYOUT"
    if text:
        _run(session, "DELETE FROM MOTHERLODE.RAW.DOCUMENT_TEXT WHERE DOC_ID = ?", [doc_id])
        _run(session, "INSERT INTO MOTHERLODE.RAW.DOCUMENT_TEXT (DOC_ID, TEXT, PAGES, PARSER) VALUES (?, ?, ?, ?)",
             [doc_id, text, pages, parser])
    return text, pages


def screen(session, doc_id: str, title: str, company: str, text: str, model: str) -> dict:
    """Is it a project deal and which project (the original triage prompt), then deal category and continent."""
    reply = _value(session, cortex.COMPLETE_SQL, [model, cortex.triage_prompt(title, company, text)])
    verdict, _usage = cortex.parse_triage(reply)
    is_deal = verdict.get("is_project_deal") is True          # only a real boolean counts
    start = text[:cortex.TRIAGE_CHAR_LIMIT]
    result = {
        "is_project_deal": is_deal, "project_name": verdict.get("project_name"), "reason": verdict.get("reason"),
        "project_named_in_text": named_in(text, verdict.get("project_name")),
        "deal_category": cortex.parse_label(_value(session, cortex.DEAL_TYPE_SQL, [start])) if is_deal else None,
        "continent": cortex.parse_label(_value(session, cortex.CONTINENT_SQL, [start])) if is_deal else None,
    }
    method = f"AI_COMPLETE {model}"
    _run(session, "DELETE FROM MOTHERLODE.CORE.SCREENING WHERE DOC_ID = ? AND METHOD = ?", [doc_id, method])
    _run(session, "INSERT INTO MOTHERLODE.CORE.SCREENING (DOC_ID, METHOD, IS_PROJECT_DEAL, PROJECT_NAME, "
                  "PROJECT_NAMED_IN_TEXT, REASON, DEAL_CATEGORY, CONTINENT) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
         [doc_id, method, result["is_project_deal"], result["project_name"], result["project_named_in_text"],
          result["reason"], result["deal_category"], result["continent"]])
    return result


def screen_with_filter(session, text: str) -> bool:
    """The same yes/no question through AI_FILTER, for the comparison in the eval."""
    return bool(_value(session, cortex.FILTER_SQL, [cortex.IS_DEAL_PREDICATE, text[:cortex.TRIAGE_CHAR_LIMIT]]))


def extract_and_store(session, doc_id: str, text: str, screening: dict, extractor: str, max_attempts: int = 2) -> Result:
    """Run the extract, validate, retry, quarantine loop for one document and write what it decides.

    `extractor` is "AI_EXTRACT" or the name of a model for AI_COMPLETE. AI_EXTRACT takes questions, not a
    prompt, so it has no way to receive the correction and gets a single attempt."""
    name = AI_EXTRACT if extractor == AI_EXTRACT else f"AI_COMPLETE {extractor}"
    attempts = {"n": 0}

    def extract(article: str, project_name: str | None, feedback: str | None) -> dict:
        attempts["n"] += 1
        if extractor == AI_EXTRACT:
            questions = json.dumps(cortex.extract_questions(project_name))
            extraction, usage = cortex.parse_extract(_value(session, cortex.EXTRACT_TEXT_SQL, [article, questions])), {}
        else:
            reply = _value(session, cortex.COMPLETE_SQL, [extractor, cortex.extraction_prompt(article, project_name, feedback)])
            extraction, usage = cortex.parse_completion(reply)
        _run(session, "INSERT INTO MOTHERLODE.CORE.EXTRACTION_ATTEMPTS (DOC_ID, EXTRACTOR, ATTEMPT, EXTRACTION, "
                      "PROMPT_TOKENS, COMPLETION_TOKENS) SELECT ?, ?, ?, PARSE_JSON(?), ?, ?",
             [doc_id, name, attempts["n"], json.dumps(extraction, default=str), usage.get("prompt_tokens"),
              usage.get("completion_tokens")])
        return extraction

    _run(session, "DELETE FROM MOTHERLODE.CORE.EXTRACTION_ATTEMPTS WHERE DOC_ID = ? AND EXTRACTOR = ?", [doc_id, name])
    result = process(text, screening.get("project_name"), extract, 1 if extractor == AI_EXTRACT else max_attempts)
    if result.report:
        _run(session, "UPDATE MOTHERLODE.CORE.EXTRACTION_ATTEMPTS SET REPORT = PARSE_JSON(?) "
                      "WHERE DOC_ID = ? AND EXTRACTOR = ? AND ATTEMPT = ?",
             [json.dumps(result.report), doc_id, name, result.attempts])
    if result.needs_review:
        _run(session, "INSERT INTO MOTHERLODE.CORE.REVIEW_QUEUE (DOC_ID, EXTRACTOR, WITHHELD, REPORT) "
                      "SELECT ?, ?, PARSE_JSON(?), PARSE_JSON(?)",
             [doc_id, name, json.dumps(result.withheld, default=str), json.dumps(result.report)])
    if result.row is not None:
        row = {**result.row, "project_name": screening.get("project_name") or result.row.get("project_name")}
        _run(session, "DELETE FROM MOTHERLODE.CORE.DEALS WHERE DOC_ID = ?", [doc_id])
        _run(session, ddl.insert_deal_sql(), [doc_id, screening.get("continent"), screening.get("deal_category"), name,
                                               result.attempts, result.outcome, json.dumps(row)])
    return result


# --- the whole run, as the stored procedure calls it -------------------------------------------------------


def process_new_announcements(session, extractor: str, screen_model: str, max_documents: int) -> str:
    """Parse, screen and extract documents that are registered but not yet in DOCUMENT_TEXT.

    `max_documents` is a hard limit per call: inside Snowflake there is no local ledger, so the number of
    documents is what bounds the spend of one run."""
    run_id = uuid.uuid4().hex[:12]
    pending = session.sql(
        "SELECT d.DOC_ID, d.FILE_NAME, d.TITLE, d.COMPANY FROM MOTHERLODE.RAW.DOCUMENTS d "
        "LEFT JOIN MOTHERLODE.RAW.DOCUMENT_TEXT t ON t.DOC_ID = d.DOC_ID "
        "WHERE t.DOC_ID IS NULL AND NOT ENDSWITH(d.FILE_NAME, '.txt') ORDER BY d.DOC_ID LIMIT ?",
        params=[int(max_documents)]).collect()
    outcomes: dict[str, int] = {}
    for doc_id, file_name, title, company in pending:
        text, pages = parse(session, doc_id, file_name)
        if not text:
            outcome = "unreadable"
        else:
            screening = screen(session, doc_id, title or "", company or "", text, screen_model)
            if not screening["is_project_deal"]:
                outcome = "not_a_project_deal"
            elif not screening["project_named_in_text"]:
                outcome = "project_name_not_in_source"
            else:
                outcome = extract_and_store(session, doc_id, text, screening, extractor).outcome or NO_DATA
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        log(session, run_id, doc_id, "RESULT", f"{outcome}; {pages} pages")
    summary = f"run {run_id}: {len(pending)} documents; " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
    log(session, run_id, None, "SUMMARY", summary)
    return summary
