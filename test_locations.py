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
    ("West Coast (Remote)", "us"),      # regional descriptor, no country token
    ("East Coast, Remote", "us"),
    ("Southeast Territory", "us"),
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
    # A US city name abroad must not beat the named country. Found live:
    # Workato listed a role across Mexico + "San Jose, Costa Rica" and the
    # bare-city match made the whole posting read as US.
    ("Remote - Worldwide", "us"),        # Torre's remote_anywhere shape
    ("Hybrid - US", "us"),               # US, but see says_onsite: not remote
    ("On-site - US", "us"),
    ("Remote - Anywhere", "us"),
    ("San Jose, Costa Rica", "elsewhere"),
    ("Guadalajara, Jalisco, Mexico; San Jose, Costa Rica", "elsewhere"),
    ("Birmingham, United Kingdom", "elsewhere"),
    ("San Jose, CA", "us"),            # ...but the real one still resolves
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


def _onsite_checks() -> int:
    """says_onsite() is what stops "Hybrid - US" reading as fully remote: it
    parses to country=United States with no city, which remote_label() calls
    "Remote, US". Ashby reports isRemote=true on hybrid roles, so this is the
    backstop for a whole class of false remotes."""
    failures = 0
    for text, want in [("Hybrid - US", True), ("Hybrid - San Francisco, CA", True),
                       ("On-site - US", True), ("Onsite - New York", True),
                       ("In-office, Austin TX", True),
                       ("Remote - United States", False), ("Dallas, TX", False),
                       ("Remote - Worldwide", False), ("", False), (None, False)]:
        got = locations.says_onsite(text)
        ok = got == want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} says_onsite({text!r:32}) = {got}")
    return failures


def _onsite_risk_checks() -> int:
    """Advisory flag for office requirements buried in the JD body. Measured
    at roughly 1-in-3 precision on live data, which is why it warns instead of
    rejecting — see locations.onsite_risk."""
    failures = 0
    cases = [
        ("able to be in-office 2-3 days/week", True),
        ("within commutable distance of our NYC office", True),
        ("required to be in-office Tuesdays and Thursdays", True),
        ("We offer a hybrid schedule, 3 days in office", True),
        # "hybrid" describing the ROLE, not the work arrangement.
        ("This is a hybrid consulting + builder role", False),
        ("Fully remote, work from anywhere in the US", False),
        ("remote-first company; offices optional", False),
        ("", False),
        (None, False),
    ]
    for text, want in cases:
        got = bool(locations.onsite_risk(text))
        ok = got == want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} onsite_risk({(text or '')[:44]!r:46}) = {got}")
    # A posting that names offices but stays open to remote should say so.
    both = locations.onsite_risk(
        "within commutable distance of our SF office, though we're open to "
        "remote candidates for this role")
    ok = any("remote is OK" in r for r in both)
    failures += not ok
    print(f"{'ok  ' if ok else 'FAIL'} contradictory posting is marked as such: {both}")
    return failures


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
