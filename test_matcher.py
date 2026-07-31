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

# (title, should_be_rejected)
CASES = [
    ("International Solutions Engineer", False),
    ("Internal Tools Solutions Engineer", False),
    ("Solutions Engineer Intern", True),
    ("Technical Account Manager", False),
    ("Engineering Manager, Solutions", True),
    ("Sales Engineer", False),
    ("Senior Solutions Consultant", False),
    ("Pre-Sales Engineer", False),
    ("Account Executive, Enterprise", True),
]


def main() -> int:
    failures = 0
    for title, should_reject in CASES:
        score, why = matcher.score({"title": title, "location": "Remote - US"}, CONFIG)
        rejected = score < 0
        ok = rejected == should_reject
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {'REJECT' if rejected else f'score {score:>3}':<9} {title:<38} {why}")

    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
