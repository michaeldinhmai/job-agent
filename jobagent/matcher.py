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
# It's a display convenience, not a new scoring model: the raw `score` field
# remains the one config.json's min_score and all comparisons operate on.
# Recalibrate whenever the live distribution shifts — e.g. after adding boost
# keywords — or the top of the range flattens into an indistinguishable band
# of 100/100.
#   2026-07-30: 20, from max observed 20, min_score 10 -> 50/100, p90 14 -> 70/100.
#   2026-08-12: 28, after role-priority boosts moved the ceiling. At 20, the top
#               25 of 75 matches all clamped to 100/100 — the entire top third
#               was unrankable on screen. Live now: max 28, p50 17, p95 23.
SCORE_SCALE_MAX = 28


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

    # Hard requirement, not a scoring nudge: with an include list configured,
    # a listing whose title matches none of them is rejected outright, even
    # if keyword boosts alone would otherwise clear min_score — the include
    # list exists to define the target roles, not just to nudge ranking
    # within them.
    include_titles = titles.get("include", [])
    matched = [t for t in include_titles if has(t, title)]

    # A second, looser tier for titles that are genuinely ambiguous across
    # companies — "Customer Success Manager"/"Account Manager"/"Renewal
    # Manager" can be the exact same job as a Technical Account Manager at
    # one company and pure relationship/renewals with zero technical scope
    # at another. Title text alone can't tell them apart, so these only pass
    # if the posting also shows a technical-signal keyword (in the title or
    # the description) — the title picks the track, the signal confirms it's
    # the technical flavor of that track.
    soft_titles = titles.get("include_needs_technical_signal", [])
    soft_matched = [t for t in soft_titles if has(t, title)] if not matched else []
    if soft_matched:
        technical_signal = keywords.get("technical_signal", [])
        signal_hit = next((k for k in technical_signal if has(k, body)), None)
        if not signal_hit:
            return -1, (f"title~{soft_matched[0]!r} but no technical signal "
                         f"in {technical_signal}")
        matched = soft_matched
        reasons.append(f"technical signal: {signal_hit!r}")

    if (include_titles or soft_titles) and not matched:
        return -1, f"title matches none of {include_titles + soft_titles}"

    # Words that are fine as part of a target title but disqualifying when
    # bolted onto one. "Manager" is the motivating case: "Technical Account
    # Manager" is an individual-contributor target role, while "Sales Engineer
    # Enablement Manager" and "Manager, Solutions Architect" are people
    # management. titles.exclude cannot express this — it is evaluated
    # unconditionally and before the include list, so listing "manager" there
    # rejects every TAM too. Here the matched target phrase is removed first
    # and the term is only disqualifying if it survives in the remainder.
    for term in titles.get("exclude_outside_match", []):
        if not has(term, title):
            continue
        remainder = title
        for phrase in matched:
            remainder = remainder.replace(phrase, " ")
        if has(term, remainder):
            return -1, f"excluded: {term!r} outside the matched title"

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

    # Hard requirement, not a scoring nudge: reject anything that isn't
    # clearly remote-eligible AND isn't clearly based in one of your
    # allowed cities. Runs after the US check above, so a non-US "remote"
    # claim never gets here in the first place when united_states_only is on.
    local_cities = locations.get("local_cities")
    if local_cities:
        city, state, country = geo.parse_us_location(job.get("location"))
        is_remote = geo.remote_label(city, state, country) is not None
        is_local = bool(city) and city.lower() in {c.lower() for c in local_cities}
        if not is_remote and not is_local:
            return -1, f"not remote and not in {local_cities}: {job.get('location')!r}"
        if is_local:
            reasons.append(f"local city: {city}")

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
