"""US location classification. Run: python test_locations.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobagent import locations  # noqa: E402

# (location string, expected) — all drawn from real listings in the DB
CASES = [
    ("Chicago, IL or San Francisco, CA OR US Remote", "us"),
    ("San Francisco, CA • New York, NY • United States", "us"),
    ("California, United States", "us"),
    ("Remote - US", "us"),
    ("Remote - United States", "us"),
    ("Georgia, United States", "us"),
    ("Austin, TX", "us"),
    ("Remote - US and Canada", "us"),
    ("Worldwide", "us"),
    ("Anywhere", "us"),
    ("North America", "us"),
    ("Remote - United Kingdom", "elsewhere"),
    ("Remote - Spain", "elsewhere"),
    ("London, United Kingdom", "elsewhere"),
    ("Bengaluru, India", "elsewhere"),
    ("Remote - EMEA", "elsewhere"),
    ("Dublin, Ireland", "elsewhere"),
    ("Toronto, Canada", "elsewhere"),
    ("US, CA, Santa Clara", "us"),
    ("San Francisco", "us"),           # bare city, no state or country
    ("New York City", "us"),
    ("Central - United States", "us"),
    ("São Paulo", "elsewhere"),        # accented, names no country
    ("Sao Paulo", "elsewhere"),
    ("Mexico City", "elsewhere"),
    ("Zurich", "elsewhere"),
    ("Remote or Hybrid", "unknown"),   # "OR" must not read as Oregon
    ("Remote", "unknown"),
    ("Hybrid", "unknown"),
    ("2 Locations", "unknown"),        # Workday collapses multi-site postings
    ("", "unknown"),
]


def main() -> int:
    failures = 0
    for text, expected in CASES:
        got = locations.classify(text)
        ok = got == expected
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {got:<10} (want {expected:<10}) {text!r}")

    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
