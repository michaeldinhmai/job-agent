"""Matching edge cases. Run: python test_matcher.py

Guards the word-boundary behaviour — plain substring matching rejected
"International Solutions Engineer" because the exclude list contains "intern".

Runs against a self-contained fixture config so a fresh clone (and CI) can
run it with no setup. If you also have a real config.json, every case is
replayed against it as a second pass, so your live rules stay checked too.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobagent import matcher  # noqa: E402

# Deliberately minimal: exercises the matcher's LOGIC (word boundaries,
# exclude precedence, the two title tiers, negative boosts) rather than any
# one person's job search. Kept in-file so the suite has no data dependency.
FIXTURE = {
    "titles": {
        "include": ["sales engineer", "technical account", "solutions consultant"],
        "include_needs_technical_signal": ["customer success manager", "account manager"],
        "exclude": ["intern", "engineering manager", "account executive"],
    },
    "keywords": {
        # Negative weight demotes without rejecting; that's what keeps
        # "Senior Solutions Consultant" alive at exactly the score floor.
        "boost": {"senior": -3, "pre-sales": 4},
        "technical_signal": ["api", "integration", "sdk"],
        "block": [],
    },
    "companies": {"exclude": []},
    "locations": {"united_states_only": True, "unknown_ok": True},
    "min_score": 10,
}

REAL_CONFIG_PATH = Path(__file__).parent / "config.json"

# (title, description, should_be_rejected)
CASES = [
    ("International Sales Engineer", "", False),
    ("Internal Tools Sales Engineer", "", False),
    ("Solutions Engineer Intern", "", True),
    ("Technical Account Manager", "", False),
    ("Engineering Manager, Solutions", "", True),
    ("Sales Engineer", "", False),
    ("Senior Solutions Consultant", "", False),
    ("Pre-Sales Engineer", "", False),
    ("Account Executive, Enterprise", "", True),
    # Soft title tier (titles.include_needs_technical_signal): a generic
    # post-sale title only passes with a technical-signal keyword hit.
    ("Customer Success Manager",
     "Owns the technical relationship, leads API integration and configuration reviews.",
     False),
    ("Customer Success Manager",
     "Builds relationships, drives renewals, and runs quarterly business reviews.",
     True),
    ("Account Manager", "Manages SDK integration and onboarding for enterprise accounts.", False),
    ("Account Manager", "Owns renewal and expansion revenue for a book of accounts.", True),
]


def run(config: dict, label: str) -> int:
    print(f"--- {label} ---")
    failures = 0
    for title, description, should_reject in CASES:
        job = {"title": title, "description": description, "location": "Remote - US"}
        score, why = matcher.score(job, config)
        rejected = score < 0
        ok = rejected == should_reject
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {'REJECT' if rejected else f'score {score:>3}':<9} {title:<38} {why}")
    return failures


def main() -> int:
    failures = run(FIXTURE, "fixture config")

    # Second pass against the real config when one exists — catches a live
    # rule change that breaks an assumption these cases encode. Skipped on a
    # fresh clone and in CI, where config.json is git-ignored.
    if REAL_CONFIG_PATH.exists():
        real = json.loads(REAL_CONFIG_PATH.read_text(encoding="utf-8"))
        print()
        failures += run(real, "your config.json")
    else:
        print("\n(no config.json — fixture pass only)")

    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
