"""Deterministic checks on an extraction, with no model involved.

For every number the model returned, decide whether the source text supports it:

  FOUND      the value is written in the article (the snippet is kept as evidence)
  DERIVED    not written, but explained by written amounts through one of the extraction rules:
             a sum of payments, a total split across N projects, shares x price, or the sum of the parts
  NOT_FOUND  neither: the model may have invented it, read another deal, or the source is incomplete
  EMPTY      nothing was extracted (recall is measured by the eval set, not here)

`validate()` turns that into a decision the pipeline can route on. Only NOT_FOUND values block a write.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import schema, telemetry

FOUND, DERIVED, NOT_FOUND, EMPTY = "FOUND", "DERIVED", "NOT_FOUND", "EMPTY"

MONEY_FIELDS = ("cash_payments_value", "exp_comit_value")
PERCENT_FIELDS = ("nsr_percent", "interest_acquired_percent")
PLAIN_FIELDS = ("shares_amount", "share_price", "project_area", "drilling_amount")
CHECKED_FIELDS = MONEY_FIELDS + ("shares_amount", "share_price", "shares_value", "aggregate_deal_value") + PERCENT_FIELDS + (
    "project_area", "drilling_amount", "shares_value_scraped")    # the last one was stored but never checked
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen",
         "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_in_words(n: int) -> str | None:
    """1-100 as a regex fragment: 75 -> seventy[- ]five, 100 -> (?:one[- ])?hundred. Press releases often spell percentages."""
    if 1 <= n < 20:
        return _ONES[n]
    if 20 <= n < 100:
        return _TENS[n // 10] + (r"[- ]" + _ONES[n % 10] if n % 10 else "")
    return r"(?:one[- ])?hundred" if n == 100 else None


DRILLING_USD_PER_METRE = 250  # extraction rule: commitments given in metres are converted at 250 USD per metre
METRES_AFTER = r"\s*(?:m\b|metres?\b|meters?\b)"
# A royalty or interest percentage is found only near a word of its kind, not as a grade or a share of drill
# holes (critic, round 2: 7 of 76 royalty matches in the real documents were about something else).
FIELD_NEAR = {"nsr_percent": re.compile(r"royalt|\bnsr\b|\bgrr\b|smelter|gross revenue|\bnpi\b|net (?:value|profit)", re.I),
              "interest_acquired_percent": re.compile(r"interest|stake|equity|owne|earn|acqui|farm|joint venture|\bjv\b|"
                                                      r"hold|option|purchas|sell|sale|transfer|vend", re.I)}


def to_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None
    # NaN or infinity is no number: _forms() raised on them and stopped a whole run (critic finding 7)
    return number if math.isfinite(number) else None


def _forms(x: float) -> set[str]:
    """Ways a number is written: 2000000, 2,000,000, 0.25, .25"""
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    if "." in s:
        whole, frac = s.split(".")
        return {s, f"{int(whole):,}.{frac}"} | ({f".{frac}"} if whole == "0" else set())
    return {s, f"{int(s):,}"}


MONEY, SHARES, OTHER = "money", "shares", "other"
FIELD_KIND = {"cash_payments_value": MONEY, "exp_comit_value": MONEY, "shares_value": MONEY, "aggregate_deal_value": MONEY,
              "shares_amount": SHARES, "shares_value_scraped": MONEY}
_CURRENCY_BEFORE = re.compile(r"(?:[A-Z]{0,2}\$|\b(?:CAD|USD|AUD|EUR|GBP|NZD))\s?$")
_MONEY_AFTER = re.compile(r"\s*(?:million\s+|thousand\s+)?(?:dollars|in cash)\b", re.I)
# "share" in the singular ("1,000,000 share bonus") and "s hares" (a space inserted by PDF text extraction)
# also mark a share count.
_SHARES_AFTER = re.compile(r"\s*(?:million\s+|thousand\s+)?(?:[a-z-]+\s+){0,3}(?:s\s?hares?|units?|warrants?)\b", re.I)


_PHYSICAL_AFTER = re.compile(r"\s*(?:of\s+)?(?:tonnes?|tons?|t\b|ounces?|oz\b|pounds?|lbs?\b|metres?|meters?|m\b|hectares?|"
                             r"ha\b|km|square|acres?|years?|carats?|barrels?|cubic|litres?|liters?|people|g/t)", re.I)


def unit_counts(text: str, end: int, unit: str, kind: str) -> bool:
    """Does a number written with a million or thousand unit count as an amount?

    Changed from the original pipeline, where every such unit counted. The abbreviations m and k count only
    with a money or share context (a currency sign before, or dollars, in cash or shares after), because
    "6m @ 4.79% Cu" is 6 metres and "0.1m" is 10 centimetres. The words million and thousand count unless a
    physical unit follows: "2.3 million ounces" is not money, "3 million more" in a sentence about spending is."""
    if unit.lower() in ("m", "k", "mm", "bn"):
        return kind != OTHER
    return not _PHYSICAL_AFTER.match(text[end:end + 20])


def occurrence_kind(text: str, start: int, end: int) -> str:
    """What a number at text[start:end] counts: money, a share count, or neither."""
    if _CURRENCY_BEFORE.search(text[max(0, start - 5):start]) or _MONEY_AFTER.match(text[end:end + 30]):
        return MONEY
    if _SHARES_AFTER.match(text[end:end + 50]):
        return SHARES
    return OTHER


_NUMBER_WORDS = re.compile(r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
                           r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
                           r"eighty|ninety)[- ]$", re.I)
NEAR_WINDOW = 120


def _written(form: str) -> str:
    """A number form as a pattern that also accepts trailing zeros: 0.1 matches '0.10', 14 matches '14.0'.
    Changed from the original pipeline, which accepted them only for whole numbers, so '$0.10' did not
    match a share price of 0.1 and '$14.0m' did not match 14,000,000 (critic finding, round 2)."""
    return r"(?<![\d.,])" + re.escape(form) + (r"0*" if "." in form else r"(?:\.0+)?")


def not_cash(text: str, end: int) -> bool:
    """Is the money amount ending at `end` something other than a cash payment: "$500,000 worth of
    exploration", "$2m placement", "$75,000 in shares"? Critic (round 2): 8 or more of 66 cash values counted
    as written in the case PDFs were such amounts."""
    after = text[end:end + 60]
    return not re.match(r"\s*(?:in\s+)?cash", after, re.I) and bool(_NOT_CASH_AFTER.match(after))


_NOT_CASH_AFTER = re.compile(r"\s*(?:[a-z-]+\s+){0,3}?(?:exploration|expenditure|drilling|placement|capital rais|"
                             r"raising|rights issue|entitlement|in shares|worth of shares|of shares|in scrip)", re.I)


def find_number(text: str, x: float, percent: bool = False, kind: str | None = None,
                exact_kind: bool = False, near: re.Pattern | None = None,
                skip: Callable[[str, int], bool] | None = None, width: int = 90) -> str | None:
    """Return the surrounding snippet if `x` is written in `text`, else None.

    With `kind`, an occurrence of the other kind does not count: 500,000 written as "500,000 shares"
    is not evidence for a 500,000 cash payment, and "$500,000" is not evidence for a share count.
    With `exact_kind`, only an occurrence of that kind counts (a bare 500,000 is not money).
    With `near`, the pattern must occur within NEAR_WINDOW characters: a royalty percentage near the word
    royalty, not a copper grade. With `skip(text, end)`, an occurrence for which it returns True does not
    count (not_cash: an exploration amount is not a cash payment)."""
    patterns = []
    for form in _forms(x):
        tail = r"\s*(?:%|per\s?cent\b|percent\b)" if percent else r"(?![\d]|[.,]\d)"
        patterns.append(_written(form) + tail)
    if percent and x == int(x) and number_in_words(int(x)):
        # the check on the word before stops "five percent" matching inside "seventy-five percent"
        patterns.append(r"\b(?P<words>" + number_in_words(int(x)) + r")\s*(?:per\s?cent\b|percent\b)")
    if not percent and x >= 1e8:  # "1.2 billion", "$1.2bn"
        patterns += [_written(f) + r"\s*(?P<unit>billion|bn\b)" for f in _forms(x / 1e9)]
    if not percent and x >= 1e5:  # "2 million", "$1.5M", "250K"; whether the unit counts: unit_counts
        patterns += [_written(f) + r"\s*(?P<unit>million|mm\b|m\b)" for f in _forms(x / 1e6)]
        patterns += [_written(f) + r"\s*(?P<unit>thousand|k\b)" for f in _forms(x / 1e3)]
    other = {MONEY: SHARES, SHARES: MONEY}.get(kind)
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            found_kind = occurrence_kind(text, match.start(), match.end())
            if (other and found_kind == other) or (exact_kind and found_kind != kind):
                continue
            if "unit" in match.groupdict() and not unit_counts(text, match.end(), match["unit"], found_kind):
                continue
            if "words" in match.groupdict() and _NUMBER_WORDS.search(text[max(0, match.start() - 12):match.start()]):
                continue
            if near and not near.search(text[max(0, match.start() - NEAR_WINDOW):match.end() + NEAR_WINDOW]):
                continue
            if skip and skip(text, match.end()):
                continue
            start, end = max(0, match.start() - width), min(len(text), match.end() + width)
            return re.sub(r"\s+", " ", text[start:end]).strip()
    return None


_AMOUNT = re.compile(
    r"(?P<cur>(?:[A-Z]{0,2}\$|\b(?:CAD|USD|AUD|EUR|GBP|NZD)\b)\s?)?(?<![\d.,])(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"\s*(?P<unit>billion|million|thousand|bn\b|m\b|k\b)?(?=(?P<after>[^.;\n|]{0,40}))", re.I)


def written_amounts(text: str, exclude: str | None = None, only: str | None = None) -> list[float]:
    """Amounts written in the text, with repeats. A bare number such as a year does not count:
    it needs a thousands separator, a currency sign, or a million/thousand unit.

    Each amount has a kind (money, a share count, or other) so that a sum never mixes dollars with shares;
    `exclude` drops one kind; `only` keeps one kind (a number with no money or share context is "other")."""
    out = []
    for m in _AMOUNT.finditer(text):
        if not (m.group("cur") or "," in m.group("num") or m.group("unit")):
            continue
        value = float(m.group("num").replace(",", "") + (m.group("frac") or ""))
        unit = (m.group("unit") or "").lower()
        value *= {"billion": 1e9, "bn": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}.get(unit, 1)
        if value < 1000:
            continue
        kind = MONEY if m.group("cur") else occurrence_kind(text, m.start("num"), m.end())
        if unit and not unit_counts(text, m.end("unit"), unit, kind):     # "6m @ 4.79% Cu" is 6 metres
            continue
        if not unit and kind == OTHER and _PHYSICAL_AFTER.match(text[m.end("num") + len(m.group("frac") or ""):][:20]):
            continue        # "5,000,000 tonnes" is a quantity, never an amount (critic finding, round 2)
        if kind != exclude and (only is None or kind == only):
            out.append(value)
    return sorted(out)


def sum_of_amounts(target: float, amounts: list[float], max_terms: int = 3) -> tuple[float, ...] | None:
    """Two, or (with max_terms 3) three, of the amounts below the target that add up to it within 0.5, or None.
    An amount is used twice only if it is listed twice.

    Changed from the original pipeline, which tried every combination: that is O(n^3) and took minutes on a
    long PDF with hundreds of amounts. Sorted two-pointer scans give the same yes or no in O(n^2); when
    several combinations fit, the one returned may differ. tests/test_validate.py checks the answer against
    the brute-force search."""
    c = sorted(a for a in amounts if a < target)

    def pair(lo: int, goal: float) -> tuple[float, float] | None:
        i, j = lo, len(c) - 1
        while i < j:
            s = c[i] + c[j]
            if abs(s - goal) <= 0.5:
                return c[i], c[j]
            i, j = (i + 1, j) if s < goal else (i, j - 1)
        return None

    found = pair(0, target)
    if found:
        return found
    if max_terms >= 3:
        for k in range(len(c) - 2):
            found = pair(k + 1, target - c[k])
            if found:
                return (c[k], *found)
    return None


PROJECT_WORDS = re.compile(r"project|tenement|propert|licen[cs]e|claims", re.I)


def explain_by_rules(x: float, amounts: list[float], text: str, kind: str | None = None, split: bool = True) -> str | None:
    """A sum of up to three written amounts, or a written total split across 2-6 projects.
    An amount is used twice only if it is written twice. This is a plausibility test, not a proof.

    Changed from the original pipeline after a critic measured it on 236 real documents: about a third of
    invented round amounts passed. A split now needs the total written as that kind (money or shares,
    not a bare number) with a word such as project or tenement nearby, and a sum of two amounts split
    between projects is no longer accepted. `amounts` should hold one kind only (written_amounts(only=...))."""
    combo = sum_of_amounts(x, amounts, 3)
    if combo:
        return "sum of " + " + ".join(f"{c:,.0f}" for c in combo)
    if not split:       # every split goes to a judge instead (resolve_splits)
        return None
    for n in range(2, 7):
        total = x * n
        # 1e308 x 2 is infinity, and round(infinity) raised OverflowError (critic, round 2)
        if not math.isfinite(total) or total < 10000 or abs(total - round(total)) > 0.01 * n:
            continue
        total = round(total)
        if kind and find_number(text, total, kind=kind, exact_kind=True, near=PROJECT_WORDS):
            return f"{total:,.0f} split across {n} projects"
    return None


@dataclass(frozen=True)
class SplitCandidate:
    """A total written in the text that equals the value times n, with no project word next to it. Whether the
    text shares it between n projects is a reading, not a match, so a judge decides (motherlode.judge)."""
    total: float
    n: int
    passage: str

    @property
    def evidence(self) -> str:
        return f"{self.total:,.0f} split across {self.n} projects (judged)"


def split_candidate(x: float, text: str, kind: str | None) -> SplitCandidate | None:
    """The first n (2-6) for which x * n is written as `kind` (money or shares, not a bare number), when no rule
    accepted the value: the word rule found no project word nearby, or it was switched off so that a judge
    reads every split (resolve_splits). Telfer: Greatland's
    "$167.5 million" of shares for Telfer and Havieron, half of it per project; the text names the two
    assets, not the word project."""
    if not kind:
        return None
    for n in range(2, 7):
        total = x * n
        if not math.isfinite(total) or total < 10000 or abs(total - round(total)) > 0.01 * n:
            continue
        passage = find_number(text, round(total), kind=kind, exact_kind=True, width=400)
        if passage:
            return SplitCandidate(float(round(total)), n, passage)
    return None


def _close(a: float, b: float, tolerance: float = 0.01) -> bool:
    return abs(a - b) <= max(1.0, tolerance * abs(b))


@dataclass
class FieldCheck:
    status: str
    value: float | None = None
    evidence: str | None = None
    candidate: SplitCandidate | None = None     # NOT_FOUND, but a split a judge may accept


@dataclass
class Report:
    fields: dict[str, FieldCheck] = field(default_factory=dict)
    schema_issues: list[str] = field(default_factory=list)

    @property
    def not_found(self) -> list[str]:
        return [name for name, check in self.fields.items() if check.status == NOT_FOUND]

    @property
    def checked(self) -> int:
        return sum(1 for c in self.fields.values() if c.status != EMPTY)

    @property
    def grounded(self) -> int:
        return sum(1 for c in self.fields.values() if c.status in (FOUND, DERIVED))

    @property
    def passed(self) -> bool:
        return not self.not_found and not self.schema_issues

    def feedback(self) -> str:
        """What to tell the model on a retry."""
        lines = [f"- {name}: {self.fields[name].value:g}" for name in self.not_found]
        lines += [f"- {issue}" for issue in self.schema_issues]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed, "checked": self.checked, "grounded": self.grounded, "not_found": self.not_found,
            "schema_issues": self.schema_issues,
            "fields": {k: {"status": c.status, "value": c.value, "evidence": c.evidence,
                           **({"candidate": c.candidate.evidence} if c.candidate else {})}
                       for k, c in self.fields.items()},
        }

    @property
    def candidates(self) -> dict[str, SplitCandidate]:
        """Fields that are NOT_FOUND but could be a total shared between projects, for a judge to read."""
        return {k: c.candidate for k, c in self.fields.items() if c.status == NOT_FOUND and c.candidate}


def validate(extraction: dict, article_text: str, judged: dict[str, str] | None = None, split_rule: bool = True) -> Report:
    """Check every number of an extraction against the text. Traced: the span records each field's value,
    status and evidence, so a wrong decision can be found from the trace (motherlode.telemetry).

    `judged` maps fields to evidence that a judge accepted (resolve_splits); those count as DERIVED, so that an
    aggregate built on them can be supported too. With split_rule=False no total is accepted as shared between
    projects by the word rule: each possible split is left as a candidate for a judge. The word rule is the
    fallback without a judge, and it is weak: Kat Gap's single A$7,000,000 sale passed it, because the word
    "licence" stands next to the total."""
    with telemetry.span("validate", input_text_chars=len(article_text), input_extraction=extraction,
                        input_judged=judged, split_rule=split_rule) as s:
        report = _validate(extraction, article_text, judged or {}, split_rule)
        for name, check in report.fields.items():
            if check.status != EMPTY:
                s.set(**{f"field.{name}.value": check.value, f"field.{name}.status": check.status,
                         f"field.{name}.evidence": check.evidence})
        s.set(output_passed=report.passed, output_not_found=report.not_found, output_schema_issues=report.schema_issues)
        return report


FIELD_SKIP = {"cash_payments_value": not_cash}


def resolve_splits(extraction: dict, article_text: str, project: str | None, judge, threshold: float = 0.5) -> Report:
    """Validate; then ask `judge(project, field, value, candidate) -> probability` about each split candidate,
    and validate again with the ones it accepts. Without candidates the judge is never called."""
    report = validate(extraction, article_text, split_rule=False)
    accepted = {}
    for name, candidate in report.candidates.items():
        with telemetry.span("judge_split", field=name, value=report.fields[name].value, total=candidate.total,
                            n=candidate.n) as s:
            p = judge(project, name, report.fields[name].value, candidate)
            s.set(output_probability=p, output_accepted=p is not None and p >= threshold)
        if p is not None and p >= threshold:
            accepted[name] = candidate.evidence
    return validate(extraction, article_text, accepted, split_rule=False) if accepted else report


def _validate(extraction: dict, article_text: str, judged: dict[str, str] | None = None, split_rule: bool = True) -> Report:
    text = article_text.replace("\xa0", " ").replace("’", "'")
    # one kind per pool: a money sum never uses tonnes, a table count or a bare number (critic, round 2)
    money_amounts, share_amounts = written_amounts(text, only=MONEY), written_amounts(text, only=SHARES)
    report = Report(schema_issues=schema.issues(extraction))
    values = {name: to_number(extraction.get(name)) for name in CHECKED_FIELDS}

    for name in CHECKED_FIELDS:
        x = values[name]
        if x is None or x == 0:
            report.fields[name] = FieldCheck(EMPTY)
            continue
        snippet = find_number(text, x, percent=name in PERCENT_FIELDS, kind=FIELD_KIND.get(name), near=FIELD_NEAR.get(name),
                              skip=FIELD_SKIP.get(name))
        report.fields[name] = FieldCheck(FOUND, x, snippet) if snippet else FieldCheck(NOT_FOUND, x)

    def supported(name: str) -> bool:
        return report.fields[name].status in (FOUND, DERIVED)

    def derive(name: str, evidence: str | None) -> None:
        if evidence and report.fields[name].status == NOT_FOUND:
            report.fields[name] = FieldCheck(DERIVED, values[name], evidence)

    # Payments and share counts are often a sum of tranches, or one total shared by several projects.
    # The shares value can be a stated total too, shared between projects like a cash payment (a review in the
    # original pipeline's 30-deal run, 16 Nov 2026: Telfer and Havieron's $167.5 million, halved per project,
    # was withheld because this loop left the shares value out); so can the shares value stated in the news.
    for name in MONEY_FIELDS + ("shares_amount", "shares_value", "shares_value_scraped"):
        if report.fields[name].status == NOT_FOUND:
            amounts = share_amounts if name == "shares_amount" else money_amounts
            derive(name, explain_by_rules(values[name], amounts, text, FIELD_KIND.get(name), split=split_rule))
        if report.fields[name].status == NOT_FOUND:
            if name in (judged or {}):          # a split a judge accepted (resolve_splits)
                derive(name, judged[name])
            else:                               # left for a judge to read; it does not count as support
                report.fields[name].candidate = split_candidate(values[name], text, FIELD_KIND.get(name))

    # Exploration commitments stated in metres are converted at a fixed rate.
    if report.fields["exp_comit_value"].status == NOT_FOUND:
        metres = values["exp_comit_value"] / DRILLING_USD_PER_METRE
        written = metres == int(metres) and any(re.search(_written(f) + METRES_AFTER, text, re.I) for f in _forms(metres))
        if written:     # the number must be written as metres, not as shares or tonnes (critic, round 2)
            derive("exp_comit_value", f"{metres:,.0f} metres x {DRILLING_USD_PER_METRE} USD")

    # shares_value is stated in the article, or it is shares x price with both inputs supported.
    if values["shares_value"] and values["shares_amount"] and values["share_price"] \
            and supported("shares_amount") and supported("share_price") \
            and _close(values["shares_amount"] * values["share_price"], values["shares_value"]):
        derive("shares_value", "shares_amount x share_price")

    # ...or shares x a per-share price the article states, even when the model did not return that price.
    if values["shares_value"] and values["shares_amount"] and supported("shares_amount") \
            and report.fields["shares_value"].status == NOT_FOUND:
        implied = values["shares_value"] / values["shares_amount"]
        for decimals in (2, 3, 4):
            price = round(implied, decimals)
            if price > 0 and _close(values["shares_amount"] * price, values["shares_value"], 0.001) and find_number(text, price):
                derive("shares_value", f"shares_amount x {price:g} (price written in the article)")
                break

    # The aggregate is the sum of its parts, accepted only when every part is itself supported.
    if values["aggregate_deal_value"]:
        parts = [n for n in ("cash_payments_value", "exp_comit_value", "shares_value") if values[n]]
        if parts and all(supported(n) for n in parts) and _close(sum(values[n] for n in parts), values["aggregate_deal_value"]):
            derive("aggregate_deal_value", " + ".join(parts))

    return report
