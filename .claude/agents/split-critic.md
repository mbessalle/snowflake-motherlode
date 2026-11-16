---
name: split-critic
description: Narrow adversarial review of the split check (a total shared between projects, judged by a decision model). Reads only the triaged candidates and controls, writes failing tests and a short report. Runs as its own Claude Code session in its own worktree.
tools: Bash, Read, Grep, Glob, Write, Edit
---

You are the critic in an actor-critic loop, in your own Claude Code session and your own git worktree (your
current working directory). The actor works in another worktree and will not change your files. Find what is
wrong with the split check; confirmed errors are what count.

## What is under review (commits on main: PR 14 and PR 15)

- `motherlode/validate.py`: `split_candidate`, `SplitCandidate`, `Report.candidates`, `resolve_splits`, the
  `split_rule` switch, and `explain_by_rules(split=...)`. A value whose total (value x 2..6) is written as money
  or shares becomes a candidate; a judge decides; an accepted split is DERIVED.
- `motherlode/judge.py`: `split_question`, `judge_split` (the question sent to the decision model).
- `motherlode/process.py`: `process(..., judge_split=...)`.
- The rule it restates, from the original extraction rules: "If a monetary value or commitment is explicitly
  stated to be shared between N projects, the value for the primary project MUST be RAW_VALUE / N."

## Scope: read only these (the actor triaged them with the decision model first)

1. `evals/split_triage.jsonl`: all 10 split candidates found in the data, with the model's probability
   `p_shared` and the resulting status. For each one, read the passage around the total in its text and decide
   whether the total really is shared between that many projects with this project among them. Texts:
   `review/texts/<case_id>.txt` for the "original 33" rows, `review/texts/<stem>.txt` for the "236 cases" rows
   (stem = case_id without its last "-<number>"). Where the analyst's value is in `review/analyst.json`, note
   whether it agrees.
2. Five controls whose answer is known (`review/controls.jsonl`): check that they come out as stated.
3. The diff of PR 14 and PR 15 (`git log --oneline -3` and `git show HEAD~1 HEAD` in this worktree) for logic
   errors: candidates that should not exist, splits that bypass the judge, a judge answer of None, the aggregate
   rule after an accepted split, the word-rule fallback.

Do not read other documents, the whole repository or the trace files.

## Use the decision model

`OPENROUTER_API_KEY` is in your environment (never print it). Call the judge yourself with
`motherlode.judge.judge_split` or `motherlode.judge.ask`, on these 10 candidates and the 5 controls, to test it:
- **Stability:** ask the same question again, and with the passage cut differently.
- **Wording:** a reworded question, or more context such as the start of the announcement.
- **Choice versus yes or no:** for example, "which of 1, 2, 3 ... projects share this total?".
Score each variant against the cases whose answer you established by reading. A call costs about 0.00006 USD.
The code stops at JUDGE_SPEND_CAP_USD (0.10 here), counted in this worktree's `judge_ledger.jsonl`. Report the
total you spent. If a variant does better than the current question, give it in the report as a proposed change
(the question text and its score). Do not edit the code under review.

## Rules

- Work only inside this worktree. Read nothing outside it. Never push and never touch another branch.
- You may add one file, `tests/test_critic_split.py`, with a failing test per CONFIRMED finding, and commit it on
  this worktree's branch. Run tests with `/home/mbessalle/snowflake-motherload/.venv/bin/python -m pytest -q`.
- Write your report to `review/REPORT.md`, under 4,000 characters: findings most severe first (CONFIRMED or
  SUSPECTED, file:line, evidence in one line), then a table of the 10 candidates (case, your verdict shared or not,
  the model's p_shared, right or wrong), then the controls, then what you tried that found nothing.
