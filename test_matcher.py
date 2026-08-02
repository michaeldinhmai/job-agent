"""Matching edge cases. Run: python test_matcher.py

Guards the word-boundary behaviour — plain substring matching rejected
"International Solutions Engineer" because the exclude list contains "intern".
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobagent import matcher  # noqa: E402

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))

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


def main() -> int:
    failures = 0
    for title, description, should_reject in CASES:
        job = {"title": title, "description": description, "location": "Remote - US"}
        score, why = matcher.score(job, CONFIG)
        rejected = score < 0
        ok = rejected == should_reject
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {'REJECT' if rejected else f'score {score:>3}':<9} {title:<38} {why}")

    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
