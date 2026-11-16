"""One announcement: extract, validate, retry once with feedback, withhold what stays unsupported.

    extract ──► validate ──┬─ every value supported ──────────────► stored
       ▲                   ├─ unsupported, attempts left ──► extract again, naming the unsupported values
       └───────────────────┘
                           └─ unsupported, no attempts left ─► withhold those values, queue them for review,
                                                               store the rest (or nothing if the record is malformed)

This is the same loop the original pipeline ran as a LangGraph state machine. Here it is a plain function
with the model call passed in, so the routing is tested offline and the Snowpark procedure only supplies
`extract` (a Cortex call) and writes the result to tables.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import schema
from .validate import resolve_splits, validate

STORED, STORED_PARTIAL, REVIEW_ONLY, NO_DATA = "stored", "stored_partial", "review_only", "no_data"

# extract(article_text, project_name, feedback) -> the raw extraction as a dict ({} when the call failed)
Extract = Callable[[str, str | None, str | None], dict]


@dataclass
class Result:
    outcome: str
    attempts: int
    row: dict | None = None                         # typed values to store; None when nothing is written
    withheld: dict = field(default_factory=dict)    # values removed before writing, for the review queue
    report: dict | None = None                      # the last validation report
    first_report: dict | None = None                # the report of the first attempt, for the eval

    @property
    def needs_review(self) -> bool:
        return bool(self.withheld) or self.outcome == REVIEW_ONLY


def process(text: str, project_name: str | None, extract: Extract, max_attempts: int = 2,
            judge_split: Callable | None = None) -> Result:
    """`judge_split(project, field, value, candidate) -> probability` reads the totals that may be shared
    between projects (motherlode.judge.judge_split, or a Cortex AI_FILTER call inside Snowflake). Without it a
    split with no project word next to its total stays unsupported, as before."""
    attempts, feedback, first_report = 0, None, None
    while True:
        attempts += 1
        extraction = extract(text, project_name, feedback)
        if not extraction:
            return Result(NO_DATA, attempts, first_report=first_report)
        report = resolve_splits(extraction, text, project_name, judge_split) if judge_split else validate(extraction, text)
        first_report = first_report or report.as_dict()
        if report.passed:
            return Result(STORED, attempts, schema.normalise(extraction), report=report.as_dict(), first_report=first_report)
        if attempts < max_attempts:
            feedback = report.feedback()
            continue
        withheld = {name: extraction.get(name) for name in report.not_found}
        if report.schema_issues:   # a malformed record is not written at all
            return Result(REVIEW_ONLY, attempts, None, withheld, report.as_dict(), first_report)
        kept = {name: (None if name in withheld else value) for name, value in extraction.items()}
        return Result(STORED_PARTIAL, attempts, schema.normalise(kept), withheld, report.as_dict(), first_report)


def merge_without_nulls(existing: dict, update: dict) -> dict:
    """An update can add or correct a value but never erase one: nulls and blanks in `update` are ignored."""
    return {**existing, **{key: value for key, value in update.items() if value not in (None, "")}}
