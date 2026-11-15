"""The deal fields, read from the one place they are defined: the schema list inside the extraction prompt.

The prompt, the Pydantic model, the Snowflake table and the AI_EXTRACT questions all have to list the same
fields. Instead of four hand-kept lists, the table and the questions are generated from here, and tests check
the prompt against the Pydantic model and the committed DDL against this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .prompts import EXTRACTOR_SYSTEM_PROMPT

_LINE = re.compile(r"^- (?P<name>[a-z_]+): \((?P<kind>text|numeric|date)\) (?P<description>.+)$")

# Not asked of the model: an internal label, and two values that come from the document's metadata.
NOT_EXTRACTED = ("stage", "news_link", "news_title")
CURRENCY_FIELDS = ("aggregate_deal_currency", "cash_payments_currency", "exp_comit_currency", "shares_currency")
SQL_TYPE = {"text": "VARCHAR", "numeric": "FLOAT", "date": "DATE"}


@dataclass(frozen=True)
class Field:
    name: str
    kind: str          # text, numeric or date, as the prompt declares it
    description: str

    @property
    def sql_type(self) -> str:
        return "VARCHAR(3)" if self.name in CURRENCY_FIELDS else SQL_TYPE[self.kind]


def all_fields() -> list[Field]:
    """Every field of the target schema, in the prompt's order."""
    found = [_LINE.match(line) for line in EXTRACTOR_SYSTEM_PROMPT.splitlines()]
    return [Field(m["name"], m["kind"], m["description"].strip()) for m in found if m]


def extracted_fields() -> list[Field]:
    return [f for f in all_fields() if f.name not in NOT_EXTRACTED]


def numeric_names() -> set[str]:
    return {f.name for f in all_fields() if f.kind == "numeric"}
