"""Score saved extractions against the eval cases. No model is called here.

    python -m evals.score --texts DIR --replies DIR --name LABEL [--retry]

  --texts    DIR/<case id>.txt            the announcement texts (not committed: they are other people's press releases)
  --replies  DIR/attempt1/<case id>.json  one extraction per case; DIR/attempt2/ holds the corrected retries

The measures are the ones the original pipeline reports, so results from Snowflake compare directly:
  grounding  every extracted number is checked against the announcement by motherlode.validate
  retry      with --retry, a record that failed is replaced by its second attempt
  agreement  extracted values against the analyst's hand-entered values for the same deal

Because the layout is the original pipeline's cache layout, pointing this at that cache must reproduce its
published numbers. tests/test_score.py does exactly that when the cache is on this machine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from motherlode.validate import CHECKED_FIELDS, DERIVED, EMPTY, FOUND, NOT_FOUND, resolve_splits, to_number, validate

HERE = Path(__file__).parent
CASES = HERE / "cases.jsonl"
ANALYST_FIELDS = ("cash_payments_value", "exp_comit_value", "shares_amount", "share_price", "nsr_percent",
                  "interest_acquired_percent", "project_area")


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _agrees(a, b) -> bool:
    a, b = to_number(a), to_number(b)
    return a is not None and b is not None and abs(a - b) <= max(0.011, 0.01 * abs(b))


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def score(cases: list[dict], texts: Path, replies: Path, retry: bool = False, judge_split=None) -> dict:
    """Counts for the cases that have both a text and a first-attempt extraction. With `judge_split`
    (motherlode.judge.judge_split), totals shared between projects are judged, as in the pipeline."""
    def check(extraction: dict, text: str, project: str | None):
        return resolve_splits(extraction, text, project, judge_split) if judge_split else validate(extraction, text)

    results, skipped = [], []
    for case in cases:
        text_file, first = texts / f"{case['id']}.txt", _read(replies / "attempt1" / f"{case['id']}.json")
        if not text_file.exists() or first is None:
            skipped.append(case["id"])
            continue
        text = " ".join(text_file.read_text().split())
        report1 = check(first, text, case.get("project"))
        final, report, retried = first, report1, False
        if retry and not report1.passed:
            second = _read(replies / "attempt2" / f"{case['id']}.json")
            if second is not None:
                final, report, retried = second, check(second, text, case.get("project")), True
        results.append({"case": case, "first": report1, "final": report, "extraction": final, "retried": retried})

    def stage(key: str) -> dict:
        per_field = {}
        for name in CHECKED_FIELDS:
            per_field[name] = {s: sum(1 for r in results if r[key].fields[name].status == s) for s in (FOUND, DERIVED, NOT_FOUND, EMPTY)}
        checked = sum(c[FOUND] + c[DERIVED] + c[NOT_FOUND] for c in per_field.values())
        grounded = sum(c[FOUND] + c[DERIVED] for c in per_field.values())
        return {"grounded": grounded, "checked": checked, "records_passed": sum(1 for r in results if r[key].passed),
                "fields": per_field}

    agreement = {}
    for name in ANALYST_FIELDS:
        both = agree = model_only = analyst_only = 0
        for r in results:
            model_value, analyst_value = to_number(r["extraction"].get(name)), to_number(r["case"]["analyst"].get(name))
            if model_value and analyst_value:
                both += 1
                agree += _agrees(model_value, analyst_value)
            elif model_value:
                model_only += 1
            elif analyst_value:
                analyst_only += 1
        agreement[name] = {"both": both, "agree": agree, "model_only": model_only, "analyst_only": analyst_only}

    retried = [r for r in results if r["retried"]]
    return {
        "records": len(results), "skipped": skipped, "first": stage("first"), "final": stage("final") if retry else None,
        "retried": len(retried), "retried_now_passing": sum(1 for r in retried if r["final"].passed),
        "agreement": agreement,
        "agreement_total": {"both": sum(a["both"] for a in agreement.values()), "agree": sum(a["agree"] for a in agreement.values())},
    }


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def markdown(summary: dict, name: str) -> str:
    out = [f"# Eval: `{name}` on {summary['records']} press releases\n"]
    if summary["skipped"]:
        out.append(f"Skipped (no text or no extraction): {', '.join(summary['skipped'])}\n")
    for key, title in (("first", "First attempt"), ("final", "After the corrected retry")):
        stage = summary[key]
        if not stage:
            continue
        out += [f"## {title}\n", "| Field | Written in article | Derived by a rule | Unsupported | Empty | Supported |", "|---|---|---|---|---|---|"]
        for field, c in stage["fields"].items():
            checked = c[FOUND] + c[DERIVED] + c[NOT_FOUND]
            out.append(f"| `{field}` | {c[FOUND]} | {c[DERIVED]} | {c[NOT_FOUND]} | {c[EMPTY]} | {_percent(c[FOUND] + c[DERIVED], checked)} |")
        out += ["", f"- Values supported by the article: **{stage['grounded']} of {stage['checked']} "
                    f"({_percent(stage['grounded'], stage['checked'])})**",
                f"- Records with every value supported: **{stage['records_passed']} of {summary['records']}**", ""]
    if summary["final"]:
        out.append(f"- Records retried: {summary['retried']}; of those, now fully supported: {summary['retried_now_passing']}\n")
    out += ["## Agreement with the analyst's spreadsheet\n", "| Field | Both filled | Agree | Model only | Analyst only |", "|---|---|---|---|---|"]
    for field, a in summary["agreement"].items():
        out.append(f"| `{field}` | {a['both']} | {a['agree']} | {a['model_only']} | {a['analyst_only']} |")
    total = summary["agreement_total"]
    out.append(f"\n- Where both have a value, they agree on **{total['agree']} of {total['both']} ({_percent(total['agree'], total['both'])})**")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--texts", type=Path, required=True)
    parser.add_argument("--replies", type=Path, required=True)
    parser.add_argument("--name", required=True, help="label for the report, for example the extractor and model")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--out", type=Path, help="write the Markdown report here as well as printing it")
    args = parser.parse_args()
    report = markdown(score(load_cases(), args.texts, args.replies, args.retry), args.name)
    print(report)
    if args.out:
        args.out.write_text(report)


if __name__ == "__main__":
    main()
