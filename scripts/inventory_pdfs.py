"""Check every source PDF before anything is extracted from it: does it have a text layer, or is it a scan?

    python scripts/inventory_pdfs.py DIR        # DIR holds one folder per project, each with its PDFs

A PDF made by a program carries its text; a scanned PDF is only page images and needs OCR. The two must be
routed differently (layout parsing against OCR), so this script reads each file with pypdf, counts the
characters of the text layer per page and records the result. No model is involved.

It also finds byte-identical copies, which the source folders contain (the same announcement saved under
two row numbers), so that a document is processed and counted once.

Writes data/pdf_inventory.jsonl: one line per file with project, name, size, pages, characters, kind
(digital, scanned, mixed or unreadable), its hash and, for a copy, the file it duplicates. No document text.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "data" / "pdf_inventory.jsonl"
MIN_CHARS_PER_PAGE = 100      # a page with less text than this is treated as an image


def classify(chars_per_page: list[int]) -> str:
    """digital: every page has text. scanned: none has. mixed: some do, for example a scan with a typed cover."""
    if not chars_per_page:
        return "unreadable"
    with_text = sum(1 for chars in chars_per_page if chars >= MIN_CHARS_PER_PAGE)
    if with_text == len(chars_per_page):
        return "digital"
    return "scanned" if with_text == 0 else "mixed"


def inspect(path: Path) -> dict:
    from pypdf import PdfReader

    data = path.read_bytes()
    entry = {"project": path.parent.name, "file": path.name, "bytes": len(data), "sha256_16": hashlib.sha256(data).hexdigest()[:16]}
    try:
        chars = [len((page.extract_text() or "").strip()) for page in PdfReader(path).pages]
    except Exception as error:   # a damaged file is recorded, not fatal
        return {**entry, "pages": 0, "chars": 0, "kind": "unreadable", "error": type(error).__name__}
    return {**entry, "pages": len(chars), "chars": sum(chars), "kind": classify(chars),
            "pages_without_text": sum(1 for c in chars if c < MIN_CHARS_PER_PAGE)}


def mark_duplicates(entries: list[dict]) -> None:
    first: dict[str, str] = {}
    for entry in entries:
        key = entry["sha256_16"]
        if key in first:
            entry["duplicate_of"] = first[key]
        else:
            first[key] = f"{entry['project']}/{entry['file']}"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    files = sorted(Path(sys.argv[1]).rglob("*.pdf"))
    entries = [inspect(path) for path in files]
    mark_duplicates(entries)
    INVENTORY.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    unique = [e for e in entries if "duplicate_of" not in e]
    kinds: dict[str, int] = {}
    for entry in unique:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    print(f"{len(entries)} PDFs in {len({e['project'] for e in entries})} projects; {len(entries) - len(unique)} are copies")
    print(f"{len(unique)} distinct documents: " + ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items())))
    print(f"pages: {sum(e['pages'] for e in unique)}; pages needing OCR: {sum(e.get('pages_without_text', 0) for e in unique)}")


if __name__ == "__main__":
    main()
