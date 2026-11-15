"""A Snowpark session from the settings in .env. Values are read, never printed."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from .cost import SpendGuard

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY_FILE")


def settings() -> dict:
    load_dotenv(ROOT / ".env")
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)} (see .env.example)")
    return {
        "account": os.environ["SNOWFLAKE_ACCOUNT"], "user": os.environ["SNOWFLAKE_USER"],
        "private_key_file": os.environ["SNOWFLAKE_PRIVATE_KEY_FILE"],
        "role": os.getenv("SNOWFLAKE_ROLE", "MOTHERLODE_PIPELINE"),
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE", "MOTHERLODE_XS"),
        "database": os.getenv("SNOWFLAKE_DATABASE", "MOTHERLODE"),
    }


def session():
    from snowflake.snowpark import Session  # imported here so the offline tests do not need the package

    return Session.builder.configs(settings()).create()


def guard() -> SpendGuard:
    load_dotenv(ROOT / ".env")
    return SpendGuard(float(os.getenv("SPEND_CAP_USD", "150")), ROOT / "spend_ledger.jsonl")
