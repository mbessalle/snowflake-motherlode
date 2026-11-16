"""Ask a decision model closed questions about a text, with probabilities. For judgments that are a matter of
meaning, not of exact matching: is this company the buyer or the seller, is this area one project's or a total
for several, are these amounts tranches of the same payment.

    from motherlode import judge
    answers = judge.ask(state={"project": "Yalgoo", "text": "..."},
                        questions={"role": judge.choice("What role ...?", {"buyer": "...", "seller": "..."})})
    answers["role"]["probabilities"]["buyer"]

Code stays the first filter (cheap, exact: is the number written at all?). The model only judges what code
proposes, and its answers are calibrated against hand labels before a threshold is used (scripts/judge_cases.py).

Backend: OpenRouter's Decisions API (POST /api/alpha/decisions) with TypeSafe's Jev, a decision model that
returns typed answers with probabilities and no text. Price checked on 16 Nov 2026 from OpenRouter's model
list: 0.042 USD per million input tokens, output free; every response reports its own cost in usage.cost.
Inside Snowflake the same questions map onto Cortex AI_CLASSIFY (choice) and AI_FILTER (yes or no); that
backend is not written yet because Cortex is not available on the trial account.

Every call:
  - is cached on disk (data/judge_cache/, git-ignored: it holds document text), keyed by model, state and
    questions, so a re-run costs nothing and gives the same answer;
  - adds its reported cost to judge_ledger.jsonl (git-ignored) and stops before a call once the ledger
    reaches SPEND_CAP_USD (environment variable JUDGE_SPEND_CAP_USD, default 0.25);
  - is a trace span "judge" with the questions, the state (cut to telemetry.MAX_TEXT), the answers, the
    cost and whether it came from the cache.
The API key is read from OPENROUTER_API_KEY and never logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from . import telemetry

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "judge_cache"
LEDGER = ROOT / "judge_ledger.jsonl"
URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.14"
USD_PER_INPUT_TOKEN = 0.042 / 1e6           # OpenRouter model list, 16 Nov 2026
log = logging.getLogger("motherlode.judge")


class SpendCapReached(RuntimeError):
    pass


def choice(instructions: str, options: dict[str, str]) -> dict:
    """A question with one answer out of several: options maps each answer key to what it means."""
    return {"type": "choice", "instructions": instructions, "criteria": options}


def noul(instructions: str, true: str, false: str) -> dict:
    """A yes or no question; the answer is the probability of yes."""
    return {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}


FIELD_WORDS = {"cash_payments_value": "cash payment", "exp_comit_value": "exploration expenditure commitment",
               "shares_amount": "share consideration (number of shares)", "shares_value": "share consideration",
               "shares_value_scraped": "share consideration"}


def split_question(project: str | None, field: str, value: float, candidate) -> tuple[dict, dict]:
    """The state and question for one split candidate (motherlode.validate.SplitCandidate): does the text
    share this total between n projects, this one among them? The rule it restates is the extraction rules'
    'shared commitments: if a value is shared between N projects, the project's value is RAW_VALUE / N'."""
    what = FIELD_WORDS.get(field, field)
    state = {"project": project, "total": f"{candidate.total:,.0f}", "projects_sharing_it": candidate.n,
             "this_projects_share": f"{value:,.0f}", "passage": candidate.passage}
    question = noul(
        f"Does `passage` describe the {candidate.total:,.0f} {what} as covering {candidate.n} projects or assets, "
        f"one of them the project named in `project`, so that its equal share is {value:,.0f}?",
        f"Yes: the {what} of {candidate.total:,.0f} is paid for {candidate.n} projects or assets together, and the "
        "project named in `project` is one of them.",
        f"No: the {candidate.total:,.0f} is for this project alone, for other projects, for a different number of "
        "projects, or it is not this kind of consideration.")
    return state, {"split": question}


def judge_split(project: str | None, field: str, value: float, candidate, post=None) -> float | None:
    """P(yes) that the candidate total is shared between its n projects; for validate.resolve_splits."""
    state, questions = split_question(project, field, value, candidate)
    kwargs = {"post": post} if post else {}
    answer = ask(state, questions, label=f"split:{project}:{field}", **kwargs).get("split") or {}
    return answer.get("noul")


def spent() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(json.loads(line).get("cost", 0) for line in LEDGER.read_text().splitlines() if line.strip())


def cap() -> float:
    return float(os.environ.get("JUDGE_SPEND_CAP_USD", "0.25"))


def _key(model: str, state, questions: dict) -> str:
    blob = json.dumps({"model": model, "state": state, "questions": questions}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _post(body: dict) -> dict:
    import requests

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    for attempt in range(4):
        response = requests.post(URL, json=body, timeout=120, headers={"Authorization": f"Bearer {key}"})
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        if response.status_code != 200:
            # the body can echo the request, never the key; keep it short in the error
            raise RuntimeError(f"Decisions API {response.status_code}: {response.text[:300]}")
        return response.json()
    raise RuntimeError("unreachable")


def ask(state, questions: dict, model: str = MODEL, label: str = "", post=_post) -> dict:
    """The answers, one per question key: for a choice {"choice", "probabilities", "confidence"}, for a noul
    {"noul": probability of yes}. `post` is the transport, replaced in tests."""
    key = _key(model, state, questions)
    cached = CACHE / f"{key}.json"
    with telemetry.span("judge", label=label, model=model, input_questions=questions, input_state=state) as s:
        if cached.exists():
            reply = json.loads(cached.read_text())
            s.set(cached=True)
        else:
            if spent() >= cap():
                raise SpendCapReached(f"judge ledger at {spent():.4f} USD, cap {cap():.2f} USD")
            reply = post({"model": model, "state": state, "questions": questions})
            CACHE.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(reply, ensure_ascii=False))
            cost = float((reply.get("usage") or {}).get("cost") or 0)
            with LEDGER.open("a") as ledger:
                ledger.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model,
                                         "label": label, "cost": cost, "usage": reply.get("usage")}) + "\n")
            s.set(cached=False, cost_usd=cost)
        s.set(output_answers=reply.get("answers"), output_model=reply.get("model"), usage=reply.get("usage"))
        return reply.get("answers") or {}
