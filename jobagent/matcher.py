"""Rule-based scoring.

Deliberately dumb: substring matching on lowercased text. Run it, read the
output, and adjust config.json until the ranking looks like your judgement.
Only reach for an LLM pass once these rules stop being able to express what
you want.
"""

from __future__ import annotations

import re
from functools import lru_cache

from . import locations as geo

TITLE_HIT = 10
LOCATION_HIT = 3

# Display-only normalization of the raw additive score to a 0-100 scale.
# 20 was picked as the reference ceiling from the live score distribution
# (2026-07-30: max observed 20, config min_score threshold 10 -> 50/100,
# p90 14 -> 70/100) — it's a display convenience, not a new scoring model.
# The raw `score` field remains the one config.json's min_score and all
# comparisons actually operate on; adjust SCORE_SCALE_MAX here if the live
# distribution shifts (e.g. after adding more boost keywords).
SCORE_SCALE_MAX = 20


def to_percent(score: int) -> int:
    """Map a raw additive score onto a 0-100 display scale, clamped both ends."""
    return max(0, min(100, round(score / SCORE_SCALE_MAX * 100)))

# A keyword in the title means the job *is* that thing. The same keyword in the
# description usually just means it appeared in a tech-stack list or a benefits
# blurb, so it earns a flat point and the total is capped — otherwise employers
# with long, keyword-dense postings crowd out everyone else.
# Boost weights may be NEGATIVE: a title hit subtracts, which demotes without
# rejecting (e.g. "senior": -3 ranks senior roles below mid-level ones).
DESCRIPTION_POINT = 1
DESCRIPTION_CAP = 4


@lru_cache(maxsize=1024)
def _pattern(term: str) -> re.Pattern:
    """Match on word boundaries, not raw substrings.

    Plain `in` checks misfire badly on these titles: "intern" would reject
    "International Solutions Engineer", and "poc" would hit inside unrelated
    words. Boundaries are only applied where the term actually ends in a word
    character, so "c++" and "pre-sales" still match.
    """
    prefix = r"\b" if term[:1].isalnum() else ""
    suffix = r"\b" if term[-1:].isalnum() else ""
    return re.compile(prefix + re.escape(term) + suffix)


def has(term: str, text: str) -> bool:
    term = term.lower().strip()
    return bool(term) and _pattern(term).search(text) is not None


def score(job: dict, config: dict) -> tuple[int, str]:
    """Return (score, human-readable reasons). Negative score means rejected."""
    titles = config.get("titles", {})
    keywords = config.get("keywords", {})
    locations = config.get("locations", {})
    companies = config.get("companies", {})

    company = (job.get("company") or "").lower()
    for term in companies.get("exclude", []):
        if has(term, company):
            return -1, f"excluded company: {term!r}"

    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    body = f"{title} {description}"

    for term in titles.get("exclude", []):
        if has(term, title):
            return -1, f"excluded title: {term!r}"
    for term in keywords.get("block", []):
        if has(term, body):
            return -1, f"blocked keyword: {term!r}"

    total = 0
    reasons: list[str] = []

    matched = [t for t in titles.get("include", []) if has(t, title)]
    if matched:
        total += TITLE_HIT
        reasons.append(f"title~{matched[0]!r} +{TITLE_HIT}")

    if locations.get("united_states_only"):
        where = geo.classify(job.get("location"))
        if where == "elsewhere":
            return -1, f"not US: {job.get('location')!r}"
        if where == "unknown" and not locations.get("unknown_ok", True):
            return -1, "location unknown"
        if where == "us":
            total += LOCATION_HIT
            reasons.append(f"US location +{LOCATION_HIT}")

    description_total = 0
    for word, weight in keywords.get("boost", {}).items():
        word, weight = word.lower(), int(weight)
        if has(word, title):
            total += weight
            reasons.append(f"{word}(title) {weight:+d}")
        elif weight > 0 and has(word, description) and description_total < DESCRIPTION_CAP:
            # Negative weights are title-only demotions; "senior" in a
            # description blurb shouldn't penalize a mid-level posting.
            description_total += DESCRIPTION_POINT
            reasons.append(f"{word}(desc) +{DESCRIPTION_POINT}")
    total += description_total

    return total, ", ".join(reasons) or "no signals"


def rescore_all(jobs, config: dict) -> int:
    """Re-run scoring over every stored row. Returns rows touched.

    `jobs` is a JobRepository (its `.conn` is used for the full-table scan;
    no `query()` filters apply here since this rescans everything)."""
    rows = jobs.conn.execute("SELECT * FROM jobs").fetchall()
    for row in rows:
        value, reasons = score(dict(row), config)
        jobs.set_score(row["id"], value, reasons)
    return len(rows)
