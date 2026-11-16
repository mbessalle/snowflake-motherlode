"""Query the local trace files that motherlode.telemetry writes (traces/*.spans.jsonl and *.logs.jsonl).

    python scripts/traces.py runs                                  # every run: service, spans, errors, logs
    python scripts/traces.py spans [--run FILE] [--name NAME] [--where KEY=TEXT|KEY~TEXT ...] [--keys PREFIX] [--limit N]
    python scripts/traces.py tree TRACE_OR_SPAN_ID [--run FILE]    # one span with its children, indented
    python scripts/traces.py logs [--run FILE] [--level WARNING]

--run defaults to the newest run of every service, for spans and for logs. --where KEY=TEXT keeps spans
with an attribute KEY equal to TEXT, KEY~TEXT those where it contains TEXT (both case-insensitive). KEY may
use * to match several attributes, e.g. --where 'field.*.verdict=disagree' or --where 'case_id~glenfine'.
--keys PREFIX prints only attributes starting with PREFIX. Output is JSON lines, so it pipes into jq.

The same spans go to Jaeger when OTEL_EXPORTER_OTLP_ENDPOINT is set; this tool is for reading them with code.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path

TRACES = Path(__file__).resolve().parent.parent / "traces"


def latest_runs() -> list[Path]:
    """The newest spans file of each service (the part of the name before the time stamp)."""
    latest: dict[str, Path] = {}
    for path in sorted(TRACES.glob("*.spans.jsonl"), key=lambda p: p.stat().st_mtime):
        latest[path.name.rsplit("-", 1)[0]] = path
    if not latest:
        raise SystemExit(f"no spans files in {TRACES}")
    return list(latest.values())


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def matches(span: dict, conditions: list[str]) -> bool:
    """KEY=TEXT: some matching attribute equals TEXT (case-insensitive). KEY~TEXT: some contains TEXT.
    Equality is the default because 'verdict=agree' must not match 'disagree' (critic, round 2)."""
    for condition in conditions:
        contains = "~" in condition and ("=" not in condition or condition.index("~") < condition.index("="))
        key, _, text = condition.partition("~" if contains else "=")
        values = [v for k, v in span["attributes"].items() if fnmatch.fnmatchcase(k, key)] + \
                 ([span["name"]] if key == "name" else [])
        wanted = text.lower()
        found = [str(v).lower() for v in values]
        if not any((wanted in v) if contains else (v == wanted) for v in found):
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("runs", "spans", "tree", "logs"))
    parser.add_argument("id", nargs="?")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--where", action="append", default=[])
    parser.add_argument("--keys")
    parser.add_argument("--level")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if args.command == "runs":
        for path in sorted(TRACES.glob("*.spans.jsonl"), key=lambda p: p.stat().st_mtime):
            spans = load(path)
            logs_path = path.with_name(path.name.replace(".spans.", ".logs."))
            logs = load(logs_path) if logs_path.exists() else []
            names: dict[str, int] = {}
            for s in spans:
                names[s["name"]] = names.get(s["name"], 0) + 1
            print(json.dumps({"run": path.name, "spans": names, "errors": sum(s["status"] == "ERROR" for s in spans),
                              "warnings": sum(entry["level"] == "WARNING" for entry in logs), "logs": len(logs)}))
        return

    if args.command == "logs":
        runs = [args.run] if args.run else latest_runs()
        for spans_path in runs:
            path = spans_path.with_name(spans_path.name.replace(".spans.", ".logs."))
            for entry in load(path) if path.exists() else []:
                if not args.level or entry["level"] == args.level.upper():
                    print(json.dumps({"run": path.name, **entry}, ensure_ascii=False))
        return

    spans = load(args.run) if args.run else [s for path in latest_runs() for s in load(path)]
    if args.command == "tree":
        if not args.id:
            raise SystemExit("tree needs a trace id or span id")
        children: dict[str | None, list[dict]] = {}
        for s in spans:
            children.setdefault(s["parent_id"], []).append(s)
        roots = [s for s in spans if s["span_id"] == args.id] or [s for s in spans if s["trace_id"] == args.id and not s["parent_id"]]

        def show(s: dict, depth: int) -> None:
            attributes = {k: v for k, v in s["attributes"].items() if not args.keys or k.startswith(args.keys)}
            print("  " * depth + json.dumps({"name": s["name"], "span_id": s["span_id"], "ms": round(s["duration_ms"], 2),
                                             "status": s["status"], "attributes": attributes, "events": s["events"]}, ensure_ascii=False))
            for child in sorted(children.get(s["span_id"], []), key=lambda c: c["start"]):
                show(child, depth + 1)

        for root in roots:
            show(root, 0)
        return

    shown = 0
    for s in spans:
        if (args.name and s["name"] != args.name) or not matches(s, args.where):
            continue
        attributes = {k: v for k, v in s["attributes"].items() if not args.keys or k.startswith(args.keys)}
        print(json.dumps({"name": s["name"], "trace_id": s["trace_id"], "span_id": s["span_id"], "parent_id": s["parent_id"],
                          "status": s["status"], "attributes": attributes, "events": s["events"]}, ensure_ascii=False))
        shown += 1
        if args.limit and shown >= args.limit:
            break


if __name__ == "__main__":
    main()
