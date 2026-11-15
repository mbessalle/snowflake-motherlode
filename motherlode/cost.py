"""What a step is expected to cost, and a guard that refuses to start a step that would pass the cap.

Why this is code and not a Snowflake setting: a resource monitor covers warehouses only, and a budget on AI
services notifies but does not stop anything and can lag by hours (Snowflake docs, read 16 Nov 2026). So the
limit that actually holds is enforced here, before a call is made, from a local ledger. Planned amounts count
as spent until the measured amount replaces them, which errs on the side of stopping early.

Rates are copied from Snowflake's Service Consumption Table effective 12 Nov 2026. They are estimates of what
the usage views will later report, not a bill; `scripts/spend_report.py` reads the measured numbers.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RATES_AS_OF = "2026-11-12"
USD_PER_AI_CREDIT = 2.00            # Table 2(b), on demand, global routing (2.20 for regional routing)
USD_PER_PLATFORM_CREDIT = 2.60      # Table 2(a), Standard edition, AWS EU (Frankfurt): the trial account's
WAREHOUSE_XS_CREDITS_PER_HOUR = 1   # Table 1(a)

# AI credits per one million tokens: (input, output). Table 6(a).
COMPLETE_RATES = {
    "claude-sonnet-5": (1.20, 6.00),
    "claude-haiku-4-5": (0.60, 3.00),
    "openai-gpt-6.1-terra": (0.60, 3.00),
    "openai-gpt-6-luna": (0.06, 0.30),
    "llama3.3-70b": (0.432, 0.432),
}
# AI credits per one million tokens, input and output alike. Table 6(a).
FUNCTION_RATES = {"AI_EXTRACT": 5.55, "AI_CLASSIFY": 1.62, "AI_FILTER": 1.62}
# AI credits per 1,000 pages. Table 6(g).
PARSE_RATES = {"LAYOUT": 3.66, "OCR": 0.68}
# AI credits per one million tokens: (input, output) when the model runs inside a Cortex Agent. Table 6(d).
AGENT_RATES = {"claude-sonnet-5": (1.30, 6.50), "claude-haiku-4-5": (0.65, 3.25), "openai-gpt-6-luna": (0.065, 0.325)}
SEARCH_CREDITS_PER_GB_MONTH = 6.3   # Table 6(g)
EXTRACT_TOKENS_PER_PAGE = 970       # AI_EXTRACT reference: each page of a paged file counts as 970 tokens
CHARS_PER_TOKEN = 4                 # rule of thumb for English; AI_COUNT_TOKENS gives the real count on an account


def tokens(text_or_chars: str | int) -> int:
    chars = text_or_chars if isinstance(text_or_chars, int) else len(text_or_chars)
    return math.ceil(chars / CHARS_PER_TOKEN)


def complete_usd(model: str, input_tokens: int, output_tokens: int, rates: dict = COMPLETE_RATES) -> float:
    rate_in, rate_out = rates[model]
    return (input_tokens * rate_in + output_tokens * rate_out) / 1e6 * USD_PER_AI_CREDIT


def function_usd(function: str, total_tokens: int) -> float:
    return total_tokens * FUNCTION_RATES[function] / 1e6 * USD_PER_AI_CREDIT


def parse_usd(pages: int, mode: str = "LAYOUT") -> float:
    return pages * PARSE_RATES[mode] / 1000 * USD_PER_AI_CREDIT


def warehouse_usd(hours: float) -> float:
    return hours * WAREHOUSE_XS_CREDITS_PER_HOUR * USD_PER_PLATFORM_CREDIT


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class SpendGuard:
    """A cap in dollars and an append-only ledger file. `authorise` must be called before a paid step."""

    cap_usd: float
    ledger_path: Path

    def entries(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text().splitlines() if line.strip()]

    def spent_usd(self) -> float:
        """Measured amounts where a step has one, otherwise its planned amount."""
        latest: dict[str, dict] = {}
        for entry in self.entries():
            latest[entry["step"]] = {**latest.get(entry["step"], {}), **entry}
        return sum(e.get("actual_usd", e.get("planned_usd", 0.0)) for e in latest.values())

    def _append(self, entry: dict) -> None:
        entry["at"] = datetime.now(UTC).isoformat(timespec="seconds")
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def authorise(self, step: str, planned_usd: float) -> float:
        """Record the plan and return what is left, or raise without recording if the cap would be passed."""
        spent = self.spent_usd()
        if spent + planned_usd > self.cap_usd:
            raise BudgetExceeded(f"{step}: planned ${planned_usd:.2f} on top of ${spent:.2f} spent passes the "
                                 f"${self.cap_usd:.2f} cap. Nothing was run.")
        self._append({"step": step, "planned_usd": round(planned_usd, 4)})
        return self.cap_usd - spent - planned_usd

    def record_actual(self, step: str, actual_usd: float) -> None:
        self._append({"step": step, "actual_usd": round(actual_usd, 4)})
