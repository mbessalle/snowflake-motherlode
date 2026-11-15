"""Add today's headlines from the public news sitemap to data/headlines.jsonl.

    python scripts/collect_headlines.py

The sitemap is the feed the listing site publishes for automated readers; its JSON API is disallowed by its
robots.txt and is not used. The feed only holds recent items, so a daily run (the GitHub Actions workflow
collect-headlines.yml) builds up a dataset over time. It is the training data for the planned headline
classifier: is this headline a project transaction or not?

Each line: id, date, company, title, and `deal_words` (whether the original pipeline's headline filter would
keep it). That flag is a weak label made by a regular expression, not a human judgement.
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADLINES = ROOT / "data" / "headlines.jsonl"
NEWS_SITEMAP_URL = "https://mininghub.com/news.xml"
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9", "news": "http://www.google.com/schemas/sitemap-news/0.9"}
_LINK = re.compile(r"/company/(?P<company>[^/]+)/[^/]+/(?P<slug>.+)-(?P<id>\d+)$")
# The original pipeline's headline filter, unchanged.
DEAL_WORDS = re.compile(
    r"acqui|divest|\bsells?\b|\bsale\b|\bsold\b|purchas|\boption|earn[- ]in|joint venture|\bjv\b|farm[- ]?in|farm[- ]?out|"
    r"merger|\bmerge|takeover|arrangement|consolidat|\bstake\b|\binterest in\b|agreement to", re.I)


def parse(xml_text: str) -> list[dict]:
    rows = []
    for url in ET.fromstring(xml_text).findall("sm:url", _NS):
        match = _LINK.search((url.findtext("sm:loc", default="", namespaces=_NS) or "").strip())
        title = (url.findtext("news:news/news:title", default="", namespaces=_NS) or "").strip()
        if not match or not title:
            continue
        rows.append({
            "id": int(match["id"]),
            "date": (url.findtext("news:news/news:publication_date", default="", namespaces=_NS) or "").strip()[:10],
            "company": match["company"].replace("-", " ").strip(),
            "title": title,
            "deal_words": bool(DEAL_WORDS.search(title)),
        })
    return rows


def merge(existing: list[dict], fetched: list[dict]) -> list[dict]:
    """Existing rows plus the fetched ones whose id is new, ordered by id."""
    known = {row["id"] for row in existing}
    return sorted(existing + [row for row in fetched if row["id"] not in known], key=lambda row: row["id"])


def main() -> None:
    import requests

    response = requests.get(NEWS_SITEMAP_URL, headers={"User-Agent": "python-requests (news sitemap reader)"}, timeout=30)
    response.raise_for_status()
    existing = [json.loads(line) for line in HEADLINES.read_text().splitlines() if line.strip()] if HEADLINES.exists() else []
    merged = merge(existing, parse(response.text))
    HEADLINES.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in merged))
    print(f"{len(merged) - len(existing)} new, {len(merged)} total, {sum(r['deal_words'] for r in merged)} with deal words")
    sys.exit(0)


if __name__ == "__main__":
    main()
