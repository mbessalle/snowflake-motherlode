"""What the planned Snowflake steps are expected to cost, from the documents actually on disk. Runs offline.

    python scripts/estimate_cost.py

Token counts are estimated at four characters per token and pages at 3,000 characters per page; the real
counts come from AI_COUNT_TOKENS and the usage views once there is an account. Rates: motherlode/cost.py.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motherlode import cortex, cost  # noqa: E402

OUTPUT_TOKENS_EXTRACT = 1300      # the original pipeline's traced extraction call returned 1,283 tokens
OUTPUT_TOKENS_TRIAGE = 60
CHARS_PER_PAGE = 3000


def document_chars(entry: dict) -> int:
    path = ROOT / "data" / "documents" / entry["file"]
    if entry["file"].endswith(".txt"):
        return len(path.read_text(errors="replace"))
    if entry["file"].endswith(".html"):
        import trafilatura

        return len(trafilatura.extract(path.read_text(errors="replace"), include_tables=True, favor_recall=True) or "")
    return 10 * CHARS_PER_PAGE      # a PDF: assume ten pages until it is parsed


def main() -> None:
    manifest = [json.loads(line) for line in (ROOT / "data" / "manifest.jsonl").read_text().splitlines() if line.strip()]
    eval_ids = {json.loads(line)["id"] for line in (ROOT / "evals" / "cases.jsonl").read_text().splitlines() if line.strip()}
    chars = {e["id"]: document_chars(e) for e in manifest if e.get("file")}
    to_parse = [e for e in manifest if e.get("file") and not e["file"].endswith(".txt")]
    pages = sum(math.ceil(chars[e["id"]] / CHARS_PER_PAGE) for e in to_parse)

    system = cost.tokens(cortex.EXTRACTOR_SYSTEM_PROMPT) + 150
    extract_in = sum(system + cost.tokens(min(chars[i], cortex.ARTICLE_CHAR_LIMIT)) for i in eval_ids if i in chars)
    questions = cost.tokens(json.dumps(cortex.extract_questions("Project")))
    ai_extract_tokens = sum(questions + cost.tokens(chars[i]) + 33 * 20 for i in eval_ids if i in chars)
    triage_in = sum(cost.tokens(cortex.TRIAGE_SYSTEM_PROMPT) + cost.tokens(min(c, cortex.TRIAGE_CHAR_LIMIT)) for c in chars.values())
    classify_tokens = 2 * sum(cost.tokens(min(c, cortex.TRIAGE_CHAR_LIMIT)) + 120 for c in chars.values())
    n = len(eval_ids & set(chars))

    rows = [
        (f"AI_PARSE_DOCUMENT, LAYOUT, {len(to_parse)} files, about {pages} pages", cost.parse_usd(pages)),
        (f"Screening, AI_COMPLETE claude-haiku-4-5, {len(chars)} documents",
         cost.complete_usd("claude-haiku-4-5", triage_in, OUTPUT_TOKENS_TRIAGE * len(chars))),
        (f"Deal type and continent, AI_CLASSIFY, {len(chars)} documents", cost.function_usd("AI_CLASSIFY", classify_tokens)),
        (f"Screening comparison, AI_FILTER, {len(chars)} documents", cost.function_usd("AI_FILTER", classify_tokens // 2)),
        (f"Extraction, AI_COMPLETE claude-sonnet-5, {n} cases, one pass",
         cost.complete_usd("claude-sonnet-5", extract_in, OUTPUT_TOKENS_EXTRACT * n)),
        (f"Extraction, AI_EXTRACT, {n} cases, one pass", cost.function_usd("AI_EXTRACT", ai_extract_tokens)),
    ]
    print(f"Rates as of {cost.RATES_AS_OF}; {len(chars)} documents, {sum(chars.values()):,} characters of text.\n")
    print("| Step | Estimated USD of trial balance |\n|---|---|")
    for name, usd in rows:
        print(f"| {name} | {usd:.2f} |")
    print(f"| **One full pass of the above** | **{sum(usd for _, usd in rows):.2f}** |")
    print(f"| Warehouse, extra small, per hour running | {cost.warehouse_usd(1):.2f} |")


if __name__ == "__main__":
    main()
