"""Drive the pipeline from this machine, with the spending guard in front of every paid step.

    python scripts/run_pipeline.py upload                        # put data/documents on the stage (no AI cost)
    python scripts/run_pipeline.py run --extractor claude-sonnet-5 --limit 3
    python scripts/run_pipeline.py run --extractor AI_EXTRACT
    python scripts/run_pipeline.py export --extractor claude-sonnet-5   # then: python -m evals.score ...

`run` parses, screens and extracts the documents of data/manifest.jsonl. For the 33 eval cases the project
name comes from the case, as in the original eval, so the numbers compare with its baseline.
`export` writes the saved replies in the layout evals/score.py reads.

NOT YET RUN against an account.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motherlode import connection, cost, pipeline  # noqa: E402

DOCUMENTS = ROOT / "data" / "documents"
SCREEN_MODEL = "claude-haiku-4-5"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def manifest() -> list[dict]:
    companies = {str(c["id"]): c.get("company") for c in read_jsonl(ROOT / "evals" / "screening_cases.jsonl")}
    return [{**e, "company": companies.get(e["id"])} for e in read_jsonl(ROOT / "data" / "manifest.jsonl") if e.get("file")]


def slug(extractor: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", extractor.lower()).strip("-")


def upload(session) -> None:
    for entry in manifest():
        session.file.put(str(DOCUMENTS / entry["file"]), pipeline.STAGE, auto_compress=False, overwrite=True)
    session.sql("ALTER STAGE MOTHERLODE.RAW.ANNOUNCEMENTS REFRESH").collect()
    print(f"{len(manifest())} files on {pipeline.STAGE}; {pipeline.register(session, manifest())} newly registered")


def planned_usd(entries: list[dict], extractor: str) -> float:
    """A deliberately high estimate: every document parsed at 10 pages, screened and extracted with a retry."""
    usd = 0.0
    for entry in entries:
        chars = min((DOCUMENTS / entry["file"]).stat().st_size, 40000)
        usd += 0 if entry["file"].endswith(".txt") else cost.parse_usd(10)
        usd += cost.complete_usd(SCREEN_MODEL, 1500, 100) + 2 * cost.function_usd("AI_CLASSIFY", 1200)
        if extractor == pipeline.AI_EXTRACT:
            usd += cost.function_usd("AI_EXTRACT", cost.tokens(chars) + 2500)
        else:
            usd += 2 * cost.complete_usd(extractor, cost.tokens(chars) + 1500, 1500)
    return usd


def run(session, extractor: str, limit: int | None) -> None:
    projects = {c["id"]: c["project"] for c in read_jsonl(ROOT / "evals" / "cases.jsonl")}
    entries = manifest()[:limit]
    estimate = planned_usd(entries, extractor)
    left = connection.guard().authorise(f"run {extractor} x{len(entries)}", estimate)
    print(f"planned at most ${estimate:.2f}; ${left:.2f} left under the cap")
    for entry in entries:
        saved = (DOCUMENTS / entry["file"]).read_text(errors="replace") if entry["file"].endswith(".txt") else None
        text, _pages = pipeline.parse(session, entry["id"], entry["file"], saved)
        if not text:
            print(f"{entry['id']:>9}  unreadable")
            continue
        text = " ".join(text.split())
        screening = pipeline.screen(session, entry["id"], entry["title"], entry.get("company") or "", text, SCREEN_MODEL)
        if entry["id"] in projects:                       # an eval case: extract for the labelled project
            screening = {**screening, "project_name": projects[entry["id"]]}
            result = pipeline.extract_and_store(session, entry["id"], text, screening, extractor)
            print(f"{entry['id']:>9}  {result.outcome} after {result.attempts} attempt(s); deal={screening['is_project_deal']}")
        else:
            print(f"{entry['id']:>9}  screened only; deal={screening['is_project_deal']}")


def export(session, extractor: str) -> None:
    name = pipeline.AI_EXTRACT if extractor == pipeline.AI_EXTRACT else f"AI_COMPLETE {extractor}"
    out = ROOT / "data" / "replies" / slug(extractor)
    rows = session.sql("SELECT DOC_ID, ATTEMPT, EXTRACTION FROM MOTHERLODE.CORE.EXTRACTION_ATTEMPTS WHERE EXTRACTOR = ?",
                       params=[name]).collect()
    for doc_id, attempt, extraction in rows:
        target = out / f"attempt{attempt}" / f"{doc_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(extraction if isinstance(extraction, str) else json.dumps(extraction))
    texts = ROOT / "data" / "replies" / "texts"
    texts.mkdir(parents=True, exist_ok=True)
    for doc_id, text in session.sql("SELECT DOC_ID, TEXT FROM MOTHERLODE.RAW.DOCUMENT_TEXT").collect():
        (texts / f"{doc_id}.txt").write_text(text)
    print(f"{len(rows)} replies in {out}\nscore with: python -m evals.score --texts {texts} --replies {out} --name '{name}' --retry")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("upload")
    for command in ("run", "export"):
        p = sub.add_parser(command)
        p.add_argument("--extractor", required=True, help=f"{pipeline.AI_EXTRACT} or a model: {', '.join(cost.COMPLETE_RATES)}")
        if command == "run":
            p.add_argument("--limit", type=int, help="process only the first N documents")
    args = parser.parse_args()
    if args.command != "upload" and args.extractor != pipeline.AI_EXTRACT and args.extractor not in cost.COMPLETE_RATES:
        raise SystemExit(f"No published rate for '{args.extractor}' in motherlode/cost.py; add it before spending on it.")
    session = connection.session()
    if args.command == "upload":
        upload(session)
    elif args.command == "run":
        run(session, args.extractor, args.limit)
    else:
        export(session, args.extractor)


if __name__ == "__main__":
    main()
