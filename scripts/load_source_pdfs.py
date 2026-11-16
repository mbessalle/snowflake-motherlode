"""Put the source PDFs on the Snowflake stage and register them, with the text layer as a stand-in for parsing.

    python scripts/load_source_pdfs.py DIR [--limit N] [--skip-upload]

DIR holds one folder per project (the layout scripts/inventory_pdfs.py reads). Only distinct documents are
loaded; copies listed in data/pdf_inventory.jsonl are skipped.

For each document:
  - the PDF goes to @MOTHERLODE.RAW.ANNOUNCEMENTS/pdfs/<project>/, where AI_PARSE_DOCUMENT and AI_EXTRACT
    will read it once the AI features of the account are enabled;
  - RAW.DOCUMENTS gets its project, page count and what the text-layer check found (digital or mixed);
  - RAW.DOCUMENT_TEXT gets the text layer read here with pypdf, marked "pypdf text layer". This is a
    stand-in: it has no OCR, so image-only pages contribute nothing, and it is to be replaced by
    AI_PARSE_DOCUMENT output. It lets the hierarchical index be built over the full set now.

No AI credits are used: uploads and inserts only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motherlode import connection, cost, pipeline  # noqa: E402

INVENTORY = ROOT / "data" / "pdf_inventory.jsonl"
PARSER = "pypdf text layer"
BATCH = 20


def doc_id(entry: dict) -> str:
    return Path(entry["file"]).stem


def text_layer(path: Path) -> str:
    from pypdf import PdfReader

    return "\n\n".join((page.extract_text() or "").strip() for page in PdfReader(path).pages).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-upload", action="store_true", help="register and load text only; the PDFs are already staged")
    args = parser.parse_args()
    entries = [json.loads(line) for line in INVENTORY.read_text().splitlines() if line.strip()]
    entries = [e for e in entries if "duplicate_of" not in e and e["kind"] != "unreadable"][:args.limit]
    connection.guard().authorise(f"load {len(entries)} source PDFs (warehouse only)", cost.warehouse_usd(0.5))
    session = connection.session()

    if not args.skip_upload:
        for project in sorted({e["project"] for e in entries}):
            session.file.put(str(args.directory / project / "*.pdf"), f"{pipeline.STAGE}/pdfs/{project}",
                             auto_compress=False, overwrite=True, parallel=8)
        session.sql("ALTER STAGE MOTHERLODE.RAW.ANNOUNCEMENTS REFRESH").collect()

    ids = [doc_id(e) for e in entries]
    for start in range(0, len(entries), BATCH):
        batch = entries[start:start + BATCH]
        marks = ", ".join(["?"] * len(batch))
        batch_ids = [doc_id(e) for e in batch]
        session.sql(f"DELETE FROM MOTHERLODE.RAW.DOCUMENTS WHERE DOC_ID IN ({marks})", params=batch_ids).collect()
        session.sql(f"DELETE FROM MOTHERLODE.RAW.DOCUMENT_TEXT WHERE DOC_ID IN ({marks})", params=batch_ids).collect()
        session.sql("INSERT INTO MOTHERLODE.RAW.DOCUMENTS (DOC_ID, FILE_NAME, OBTAINED, PROJECT, KIND, PAGES, PAGES_WITHOUT_TEXT) VALUES "
                    + ", ".join(["(?, ?, 'source_pdf', ?, ?, ?, ?)"] * len(batch)),
                    params=[v for e in batch for v in (doc_id(e), f"pdfs/{e['project']}/{e['file']}", e["project"], e["kind"],
                                                       e["pages"], e.get("pages_without_text", 0))]).collect()
        texts = [(doc_id(e), text_layer(args.directory / e["project"] / e["file"]), e["pages"]) for e in batch]
        texts = [t for t in texts if t[1]]
        if texts:
            session.sql("INSERT INTO MOTHERLODE.RAW.DOCUMENT_TEXT (DOC_ID, TEXT, PAGES, PARSER) VALUES "
                        + ", ".join([f"(?, ?, ?, '{PARSER}')"] * len(texts)), params=[v for t in texts for v in t]).collect()
        print(f"{min(start + BATCH, len(entries))} of {len(entries)} registered")

    staged =session.sql("SELECT COUNT(*) FROM DIRECTORY(@MOTHERLODE.RAW.ANNOUNCEMENTS) WHERE RELATIVE_PATH LIKE 'pdfs/%'").collect()[0][0]
    registered = session.sql("SELECT COUNT(*) FROM MOTHERLODE.RAW.DOCUMENTS WHERE OBTAINED = 'source_pdf'").collect()[0][0]
    with_text = session.sql(f"SELECT COUNT(*) FROM MOTHERLODE.RAW.DOCUMENT_TEXT WHERE PARSER = '{PARSER}'").collect()[0][0]
    print(f"\nPDFs on the stage: {staged}; documents registered: {registered}; with text: {with_text}; asked for: {len(ids)}")


if __name__ == "__main__":
    main()
