"""Best-effort salary extraction from job description text.

Real postings disclose pay in wildly inconsistent ways (see the survey this
was built against — pulled from live jobs.db description text, not assumed):
    "$195,000 - $230,000"                      range, no keyword needed nearby
    "Base Salary Range $75,000 – $90,000 USD"  range, anchored to "base"
    "On-Target Earnings (OTE) range is $125,000 - $152,000"  range, anchored to OTE
    "Base Salary: $120,000"                    single value, no range at all
    "$150K to $165K"                           range, K-suffixed
    "$150K+" (deal size) / "$100+ billion" (co. revenue)     NOT a salary — no
        range separator, or a bare "+" — deliberately not matched
    "$2,000 - $2,500 USD per month"            a real range, but monthly and
        far below any plausible US annual salary — filtered by bounds
    "Total OTE of $220,000-$300,000+. Base Salary: $120,000"  BOTH present —
        prefer the base figure since that's what a comp-floor check means

Heuristic, not authoritative — same spirit as locations.py. Numbers here are
what the posting says, not verified against reality.
"""

from __future__ import annotations

import re

RANGE_RE = re.compile(
    r"\$\s?(\d[\d,]{1,8})\s*([kK])?\s*(?:-|–|—|to)\s*\$?\s?(\d[\d,]{1,8})\s*([kK])?"
)
SINGLE_ANCHORED_RE = re.compile(
    r"(?:base\s+salary|annual\s+salary|salary\s+range|salary(?:\s+is)?|"
    r"compensation(?:\s+is)?|base\s+pay|pay\s+range)[^\$\n]{0,20}"
    r"\$\s?(\d[\d,]{1,8})\s*([kK])?",
    re.I,
)

BASE_KEYWORD_RE = re.compile(r"base\s+(?:salary|pay)", re.I)
GENERAL_KEYWORD_RE = re.compile(
    r"\bsalary\b|\bcompensation\b|pay\s+range|annual\s+pay", re.I
)
OTE_KEYWORD_RE = re.compile(r"\bOTE\b|on[\s-]target\s+earnings", re.I)

# A non-USD marker near the number means skip it — otherwise a CAD/MXN/etc.
# figure gets treated as a USD one, which is misleading rather than useful.
FOREIGN_CURRENCY_RE = re.compile(
    r"\b(CAD|MXN|EUR|GBP|AUD|NZD|PHP|INR|CHF)\b|/\s*mes\b", re.I
)

# A business-metric keyword nearby means the number almost certainly isn't
# compensation, even with no positive salary keyword found either — e.g. a
# sales posting saying "deals ranging from $50K to $500K+" is describing
# deal size, not pay. Found via a real false positive in live data, not
# hypothesized — worth excluding outright rather than just down-ranking.
NON_SALARY_CONTEXT_RE = re.compile(
    r"deal\s*siz|deals?\s+rang|contract\s+(?:value|siz)|contracts?\s+rang|"
    r"\bARR\b|\bquota\b|pipeline|\bbudget\b|\bspend(?:ing)?\b|"
    r"revenue|valuation|funding|raised",
    re.I,
)

# Sanity bounds for a plausible US annual salary — filters out monthly rates,
# hourly rates, bonuses, deal sizes, and company-revenue mentions that happen
# to contain a dollar range.
MIN_PLAUSIBLE = 15_000
MAX_PLAUSIBLE = 2_000_000


def _num(digits: str, k_suffix: str | None) -> int:
    n = int(digits.replace(",", "").strip())
    if k_suffix:
        n *= 1000
    return n


def _tier_window(text: str, start: int, back: int = 150) -> str:
    """Backward-only context for tier classification, trimmed to the current
    sentence/bullet. No forward lookahead: in every real example this was
    built against, the keyword ("base salary", "OTE", ...) always precedes
    the number — looking forward too risks bleeding into the *next*
    sentence's keyword (e.g. "Total OTE of $X. Base Salary: $Y" would
    otherwise make the OTE figure look base-anchored too)."""
    lo = max(0, start - back)
    segment = text[lo:start]
    boundary = max(segment.rfind(". "), segment.rfind("\n"))
    return segment[boundary + 1:] if boundary != -1 else segment


def _tier(window: str) -> int:
    """Lower is better: prefer a figure anchored to 'base', then any general
    salary/compensation keyword, then OTE-only, then no keyword at all."""
    if BASE_KEYWORD_RE.search(window):
        return 0
    if GENERAL_KEYWORD_RE.search(window):
        return 1
    if OTE_KEYWORD_RE.search(window):
        return 2
    return 3


def parse_salary(text: str | None) -> tuple[int | None, int | None]:
    """Extract an annual salary range from JD text as (min, max) whole
    dollars, or (None, None) if nothing plausible was found.

    Every plausible figure — ranges and single anchored values alike — is
    pooled and ranked by the same tier (base > general salary/comp > OTE >
    unanchored), so a single disclosed base figure correctly wins over an
    OTE *range* elsewhere in the same posting, not just over another single
    value. Candidates near a non-USD currency marker are dropped rather than
    silently treated as dollars. A single-value match that falls inside an
    already-found range's span is skipped — same disclosure, and the range
    is strictly more informative.
    """
    if not text:
        return None, None

    candidates: list[tuple[int, int, int, int]] = []  # (tier, pos, lo, hi)
    range_spans: list[tuple[int, int]] = []

    for m in RANGE_RE.finditer(text):
        lo = _num(m.group(1), m.group(2))
        hi = _num(m.group(3), m.group(4))
        if lo > hi:
            lo, hi = hi, lo
        if not (MIN_PLAUSIBLE <= lo <= MAX_PLAUSIBLE and MIN_PLAUSIBLE <= hi <= MAX_PLAUSIBLE):
            continue
        wide_window = text[max(0, m.start() - 100):m.end() + 30]
        if FOREIGN_CURRENCY_RE.search(wide_window) or NON_SALARY_CONTEXT_RE.search(wide_window):
            continue
        range_spans.append((m.start(), m.end()))
        candidates.append((_tier(_tier_window(text, m.start())), m.start(), lo, hi))

    for m in SINGLE_ANCHORED_RE.finditer(text):
        dollar_pos = m.start(1)
        if any(s <= dollar_pos < e for s, e in range_spans):
            continue  # already captured, more fully, by a range match
        val = _num(m.group(1), m.group(2))
        if not (MIN_PLAUSIBLE <= val <= MAX_PLAUSIBLE):
            continue
        wide_window = text[max(0, m.start() - 30):m.end() + 30]
        if FOREIGN_CURRENCY_RE.search(wide_window) or NON_SALARY_CONTEXT_RE.search(wide_window):
            continue
        # The keyword is part of this match itself (unlike a range match,
        # where it's external context) — tier from the match text directly.
        candidates.append((_tier(m.group(0)), m.start(), val, val))

    if not candidates:
        return None, None
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, _, lo, hi = candidates[0]
    return lo, hi


def format_salary(lo: int | None, hi: int | None) -> str | None:
    """Display string for a (min, max) pair, or None if neither is set."""
    if lo is None and hi is None:
        return None
    if lo == hi:
        return f"${lo:,}"
    return f"${lo:,} - ${hi:,}"
