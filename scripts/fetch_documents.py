"""Download the announcements of the eval cases as files, ready to be put on a Snowflake stage.

    python scripts/fetch_documents.py [--cache DIR]

A trial account cannot reach the internet from inside Snowflake, so this step runs here. For every case in
evals/screening_cases.jsonl (which includes the 33 extraction cases) it does one plain GET of the public
press-release URL and saves the page as served (HTML or PDF) under data/documents/, which is not committed.

A download is kept only if it is still the announcement: its text must be the recorded text (same hash) or
at least contain the headline's distinctive words. Otherwise, and when the site refuses the request, the text
saved by the original pipeline is used instead (`--cache`, a .txt file), and the manifest says so. Nothing is
done to get around a refusal.

data/manifest.jsonl (committed) lists, per case, the file name, how it was obtained and its hash. It holds
no announcement text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "screening_cases.jsonl"
DOCUMENTS = ROOT / "data" / "documents"
MANIFEST = ROOT / "data" / "manifest.jsonl"
DEFAULT_CACHE = Path.home() / "github-mbessalle" / "motherlode-data-pipeline" / "evals" / ".cache" / "screening"
STOPWORDS = {"the", "and", "for", "with", "from", "into", "its", "announces", "announce", "completes", "agreement",
             "project", "property", "acquisition", "option", "inc", "corp", "ltd", "limited", "resources", "mining"}


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def headline_share(title: str, text: str) -> float:
    """Share of the headline's distinctive words that the text contains."""
    words = {w for w in re.findall(r"[a-z0-9]{3,}", title.lower()) if w not in STOPWORDS}
    low = text.lower()
    return sum(1 for w in words if w in low) / max(1, len(words))


def page_text(body: bytes, is_pdf: bool) -> str:
    if is_pdf:
        return ""          # PDFs are judged after parsing in Snowflake; the headline check is skipped here
    import trafilatura

    return trafilatura.extract(body.decode("utf-8", "replace"), include_tables=True, favor_recall=True) or ""


def fetch(case: dict, cache: Path, get) -> dict:
    entry = {"id": str(case["id"]), "title": case["title"], "source_url": case["source_url"]}
    cached = cache / f"{case['id']}.txt"
    try:
        response = get(case["source_url"], timeout=30)
        body, status = response.content, response.status_code
        content_type = response.headers.get("Content-Type", "")
    except Exception as error:   # a site that does not answer is recorded, not retried
        body, status, content_type = b"", f"{type(error).__name__}", ""
    is_pdf = body[:5] == b"%PDF-" or "pdf" in content_type.lower()
    text = page_text(body, is_pdf) if status == 200 else ""
    same_text = bool(text) and sha16(text.encode()) == case.get("text_sha256_16")
    share = headline_share(case["title"], text) if text else 0.0
    if status == 200 and (is_pdf or same_text or share >= 0.7):
        name = f"{case['id']}.{'pdf' if is_pdf else 'html'}"
        (DOCUMENTS / name).write_bytes(body)
        entry.update(file=name, obtained="downloaded", http=status, same_text_as_recorded=same_text,
                     headline_share=round(share, 2), sha256_16=sha16(body), bytes=len(body))
    elif cached.exists():
        name = f"{case['id']}.txt"
        data = cached.read_bytes()
        (DOCUMENTS / name).write_bytes(data)
        entry.update(file=name, obtained="saved_text", http=status, same_text_as_recorded=sha16(data) == case.get("text_sha256_16"),
                     headline_share=round(share, 2), sha256_16=sha16(data), bytes=len(data))
    else:
        entry.update(file=None, obtained="missing", http=status)
    return entry


def main() -> None:
    import requests

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="texts saved by the original pipeline, used as fallback")
    args = parser.parse_args()
    DOCUMENTS.mkdir(parents=True, exist_ok=True)
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    entries = []
    for case in cases:
        entry = fetch(case, args.cache, requests.get)
        entries.append(entry)
        print(f"{entry['id']:>9}  {entry['obtained']:<11} http {entry['http']!s:<16} {entry.get('file')}")
    MANIFEST.write_text("".join(json.dumps(e) + "\n" for e in entries))
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["obtained"]] = counts.get(e["obtained"], 0) + 1
    print(f"\n{len(entries)} cases: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
