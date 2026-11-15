"""Call the Cortex Agent over its REST API, the way an existing system would, and check its answers.

    python scripts/agent.py ask "Which deals involved a royalty above 2%?"
    python scripts/agent.py eval        # evals/agent_questions.jsonl against answers computed by SQL

`eval` is a deterministic check: for each question the true number is computed by a SQL query on the deal
table, and the agent's answer passes only if it contains that number. It complements, and does not replace,
Snowflake's own agent evaluations (EXECUTE_AI_EVALUATION), which judge answers with a model.

NOT YET RUN against an account. Two things are unverified until it has: that the connector's session token
is accepted by the agent endpoint in this form, and the exact shape of the non-streaming response (the
answer text is collected from every "text" value in it).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motherlode import connection, cost  # noqa: E402

AGENT_PATH = "/api/v2/databases/MOTHERLODE/schemas/CORE/agents/DEAL_ANALYST:run"
USD_PER_QUESTION = cost.complete_usd("claude-sonnet-5", 25000, 2000, cost.AGENT_RATES)   # a high estimate


def texts(node) -> list[str]:
    """Every "text" string in a nested response."""
    if isinstance(node, dict):
        found = [node["text"]] if isinstance(node.get("text"), str) else []
        return found + [t for value in node.values() for t in texts(value)]
    if isinstance(node, list):
        return [t for item in node for t in texts(item)]
    return []


def numbers_in(answer: str) -> set[float]:
    found = set()
    for raw in re.findall(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?", answer):
        found.add(float(raw.replace(",", "")))
    return found


def answer_contains(answer: str, truth) -> bool:
    """The true number appears in the answer, allowing '1.5 million' for 1500000 and '2' for 2.0."""
    if truth is None:
        return False
    truth = float(truth)
    candidates = numbers_in(answer)
    scaled = {n * 1e6 for n in candidates if re.search(r"million", answer, re.I)} | {n * 1e3 for n in candidates if re.search(r"thousand", answer, re.I)}
    return any(abs(n - truth) <= 0.005 * max(1.0, abs(truth)) for n in candidates | scaled)


def ask(session, question: str) -> str:
    import requests

    host = session.connection.host
    token = session.connection.rest.token
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}], "stream": False}
    response = requests.post(f"https://{host}{AGENT_PATH}", json=body, timeout=180,
                             headers={"Authorization": f'Snowflake Token="{token}"', "Content-Type": "application/json"})
    response.raise_for_status()
    return "\n".join(texts(response.json()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ask").add_argument("question")
    sub.add_parser("eval")
    args = parser.parse_args()
    session, guard = connection.session(), connection.guard()
    if args.command == "ask":
        guard.authorise("agent ask", USD_PER_QUESTION)
        print(ask(session, args.question))
        return
    questions = [json.loads(line) for line in (ROOT / "evals" / "agent_questions.jsonl").read_text().splitlines() if line.strip()]
    guard.authorise(f"agent eval x{len(questions)}", USD_PER_QUESTION * len(questions))
    passed = 0
    for q in questions:
        truth = session.sql(q["truth_sql"]).collect()[0][0]
        answer = ask(session, q["question"])
        ok = answer_contains(answer, truth)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {q['id']:<20} truth={truth}  answer={answer[:120]!r}")
    print(f"\n{passed} of {len(questions)} answers contain the number computed by SQL")


if __name__ == "__main__":
    main()
