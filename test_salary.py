"""Salary extraction tests. Run: python test_salary.py

Every case below is a real posting shape that broke the parser at some point.
The parser is intentionally conservative: a wrong number is worse than no
number, because a comp floor gets filtered on it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobagent import salary as sal  # noqa: E402

# (label, jd text, expected (min, max))
CASES = [
    ("plain range",
     "The salary range for this role is $195,000 - $230,000 annually.",
     (195_000, 230_000)),

    ("K-suffixed range",
     "Compensation: $150K to $165K depending on experience.",
     (150_000, 165_000)),

    ("en-dash range",
     "Base salary range is: $130,000 – $208,000 USD.",
     (130_000, 208_000)),

    # Both a base figure and a wider OTE range appear. Base wins: it's what a
    # comp-floor check actually means. Getting this wrong took three attempts —
    # a forward-looking context window let the OTE range read as base-anchored.
    ("base beats OTE when both are present",
     "Total OTE of $220,000-$300,000+. Base Salary: $120,000.",
     (120_000, 120_000)),

    ("OTE alone is still better than nothing",
     "On-Target Earnings (OTE) range is $125,000 - $152,000.",
     (125_000, 152_000)),

    ("single anchored value",
     "Base Salary: $120,000 plus equity.",
     (120_000, 120_000)),

    # A range and a single-value pattern can match the same disclosure. The
    # range is strictly more informative, so the overlapping single is dropped
    # rather than truncating the answer to $130,000-$130,000.
    ("range wins over the single it overlaps",
     "For this position the annual base salary range is: $130,000 – $208,000.",
     (130_000, 208_000)),

    # Found live: a procurement role at Fivetran describing deal sizes. The
    # $450k spread was the tell.
    ("deal size is not compensation",
     "You'll manage contracts ranging from $50K to $500K+ across the portfolio.",
     (None, None)),

    ("ARR is not compensation",
     "Our customers range from $100,000 to $2,000,000 in ARR.",
     (None, None)),

    ("non-USD is skipped rather than read as dollars",
     "Salario: $30,000 MXN/Mes mas prestaciones.",
     (None, None)),

    ("monthly rate falls below the annual floor",
     "This contract pays $2,000 - $2,500 USD per month.",
     (None, None)),

    ("no figure at all",
     "Competitive salary and a generous benefits package.",
     (None, None)),

    ("empty and None inputs are safe",
     "", (None, None)),
]


def main() -> int:
    failures = 0
    for label, text, want in CASES:
        got = sal.parse_salary(text)
        ok = got == want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label:44} got={got} want={want}")

    if sal.parse_salary(None) != (None, None):
        failures += 1
        print("FAIL None input should return (None, None)")

    # Display helper.
    for args, want in [((120_000, 120_000), "$120,000"),
                       ((130_000, 208_000), "$130,000 - $208,000"),
                       ((None, None), None)]:
        got = sal.format_salary(*args)
        ok = got == want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} format_salary{args} -> {got!r}")

    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
