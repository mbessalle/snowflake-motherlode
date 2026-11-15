"""Cortex AI function calls: the SQL to send and how to read what comes back.

Everything here is a pure function (text in, text or dict out) so it is tested without an account. The
statements follow the syntax in Snowflake's function reference as read on 16 Nov 2026; until they have run
against an account they are unverified, and the README says so.

Two ways to extract are built, to be measured against each other on the same eval cases:
  AI_COMPLETE  a general model with the original pipeline's extraction prompt, unchanged
  AI_EXTRACT   Snowflake's document extraction function (the successor of Document AI), one question per field
"""
from __future__ import annotations

import json
import re

from .fields import extracted_fields, numeric_names
from .prompts import EXTRACTOR_SYSTEM_PROMPT, TRIAGE_SYSTEM_PROMPT

ARTICLE_CHAR_LIMIT = 15000
TRIAGE_CHAR_LIMIT = 4000
CONTINENTS = ("Africa", "Asia", "Europe", "North America", "Oceania", "South America")
DEAL_TYPES = {
    "acquisition or sale": "One party buys or sells a mineral project or property outright.",
    "option or earn-in": "A party can earn or buy an interest over time by payments, shares or spending.",
    "joint venture": "Two or more parties agree to hold and fund a project together.",
    "merger or takeover": "Companies combine or one acquires another, transferring named projects.",
    "staking or claim purchase": "Claims or licences are staked or bought to form or enlarge a project.",
    "other": "A project transaction that fits none of the other categories.",
}

# --- statements -----------------------------------------------------------------------------------------
# Values are passed as bind parameters (the `?` marks); nothing from a document is pasted into SQL text.

PARSE_SQL = "SELECT AI_PARSE_DOCUMENT(TO_FILE(?, ?), {'mode': 'LAYOUT'})"
COMPLETE_SQL = "SELECT AI_COMPLETE(model => ?, prompt => ?, model_parameters => {'temperature': 0}, show_details => TRUE)"
EXTRACT_TEXT_SQL = "SELECT AI_EXTRACT(text => ?, responseFormat => PARSE_JSON(?))"
EXTRACT_FILE_SQL = "SELECT AI_EXTRACT(file => TO_FILE(?, ?), responseFormat => PARSE_JSON(?))"
FILTER_SQL = "SELECT AI_FILTER(PROMPT(?, ?))"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def classify_sql(categories: dict[str, str] | tuple[str, ...], task: str) -> str:
    """AI_CLASSIFY over one bound input. The categories are constants of this module, never document text."""
    if isinstance(categories, dict):
        items = ", ".join(f"{{'label': {_sql_string(k)}, 'description': {_sql_string(v)}}}" for k, v in categories.items())
    else:
        items = ", ".join(_sql_string(c) for c in categories)
    return f"SELECT AI_CLASSIFY(?, [{items}], {{'task_description': {_sql_string(task)}}})"


DEAL_TYPE_SQL = classify_sql(DEAL_TYPES, "Classify the kind of mineral project transaction this announcement reports.")
CONTINENT_SQL = classify_sql(CONTINENTS, "Classify the continent where the mineral project of this announcement is located.")
IS_DEAL_PREDICATE = (
    "This mining announcement reports a transaction over a specific mineral project or property (an acquisition, "
    "sale, option, earn-in, farm-in, joint venture, or a merger that transfers named projects), and not only "
    "drilling results, a financing, production, a shareholder vote or an operational update: {0}"
)

# --- prompts --------------------------------------------------------------------------------------------


def extraction_prompt(article_text: str, project_name: str | None, feedback: str | None = None) -> str:
    """The original pipeline's system prompt and user message, joined into the single string AI_COMPLETE takes."""
    user = f"""
    **Primary Project of Interest:** '{project_name}'
    **Existing Database Record (if any):**
    - project_name: {project_name}
    **News Article Content:**
    ---
    {article_text[:ARTICLE_CHAR_LIMIT]}
    ---
    **Task:**
    Execute your cognitive workflow. Produce the mandatory two-part JSON object containing your reasoning and the final database updates.
    """
    if feedback:
        user += f"""
    **Correction required:** an automatic check could not find these values in the article above:
    {feedback}
    Re-read the article. Keep a value only if the article states it or it follows from stated numbers by a rule above; otherwise set the field to null.
    """
    return EXTRACTOR_SYSTEM_PROMPT + "\n" + user


def triage_prompt(title: str, company: str, announcement_text: str) -> str:
    user = f"Headline: {title}\nCompany: {company}\nAnnouncement (start):\n---\n{announcement_text[:TRIAGE_CHAR_LIMIT]}\n---"
    return TRIAGE_SYSTEM_PROMPT + "\n" + user


def extract_questions(project_name: str | None) -> dict[str, str]:
    """One AI_EXTRACT question per field, built from the same descriptions the prompt gives the general model."""
    scope = f" Answer only for the project '{project_name}'." if project_name else ""
    unit = {"numeric": " Give one number without currency signs or units.", "date": " Use the format YYYY-MM-DD.", "text": ""}
    return {f.name: f"{f.description}{unit[f.kind]}{scope} If the document does not state it, answer None."
            for f in extracted_fields()}


# --- replies --------------------------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def _as_object(reply) -> dict:
    """A reply as a dict. Replies arrive as a JSON string, sometimes inside a Markdown code fence."""
    if isinstance(reply, dict):
        return reply
    if not isinstance(reply, str):
        return {}
    text = _FENCE.sub("", reply.strip())
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            parsed = json.loads(text[start:end + 1]) if 0 <= start < end else {}
        except ValueError:
            parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def completion_text(reply) -> tuple[str, dict]:
    """(model text, token usage) from AI_COMPLETE called with show_details => TRUE."""
    details = _as_object(reply)
    choices = details.get("choices") or [{}]
    return choices[0].get("messages") or choices[0].get("message") or "", details.get("usage") or {}


def parse_completion(reply) -> tuple[dict, dict]:
    """(the `database_updates` object, token usage). A reply that is not the agreed shape gives {}."""
    text, usage = completion_text(reply)
    updates = _as_object(text).get("database_updates")
    return (updates if isinstance(updates, dict) else {}), usage


def parse_triage(reply) -> tuple[dict, dict]:
    text, usage = completion_text(reply)
    return _as_object(text), usage


def parse_label(reply) -> str | None:
    labels = _as_object(reply).get("labels") or []
    return labels[0] if labels else None


def parse_document(reply) -> tuple[str, int]:
    """(text, page count) from AI_PARSE_DOCUMENT."""
    parsed = _as_object(reply)
    if "pages" in parsed:
        text = "\n\n".join(page.get("content") or "" for page in parsed["pages"])
    else:
        text = parsed.get("content") or ""
    return text, int((parsed.get("metadata") or {}).get("pageCount") or 0)


_NOTHING = {"", "none", "null", "n/a", "na", "unknown", "not stated", "not specified", "not mentioned", "not provided",
            "not disclosed", "not applicable"}
_NUMBER = re.compile(
    r"^(?P<currency>(?:[A-Z]{1,3})?\$|(?:USD|CAD|AUD|EUR|GBP|NZD)\b)?\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?\s*"
    r"(?P<scale>billion\b|million\b|thousand\b|bn\b|m\b|k\b)?\s*"
    r"(?:%|per\s?cent|percent|[A-Za-z²]+\d?(?:\s[A-Za-z²]+){0,3})?\.?$", re.I)
_SCALE = {"billion": 1e9, "bn": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def to_number(value: str):
    """'$250,000' -> 250000.0, '2%' -> 2.0, '1.5 million' -> 1500000.0, '4,857 hectares' -> 4857.0.

    A single letter scales only an amount of money: '$1.5M' is 1,500,000 but '4,000 m' is 4,000 (metres).
    Anything that is not one plain number ('250,000 and 500,000', 'two million') is returned unchanged,
    so the schema reports it instead of this function guessing."""
    match = _NUMBER.match(value.strip())
    if not match:
        return value
    number = float(match["num"].replace(",", "") + (match["frac"] or ""))
    scale = (match["scale"] or "").lower()
    if len(scale) == 1 and not match["currency"]:
        scale = ""          # a unit, not a multiplier
    return number * _SCALE.get(scale, 1)


def parse_extract(reply) -> dict:
    """AI_EXTRACT answers every question with a string. Turn 'None' into null and number strings into numbers."""
    answers = _as_object(reply).get("response")
    if not isinstance(answers, dict):
        return {}
    numeric, out = numeric_names(), {}
    for name, value in answers.items():
        if value is None or (isinstance(value, str) and value.strip().lower() in _NOTHING):
            out[name] = None
        elif name in numeric and isinstance(value, str):
            out[name] = to_number(value)
        else:
            out[name] = value
    return out
