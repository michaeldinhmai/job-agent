"""Deciding whether a listing is US-based.

Harder than a keyword list, because job boards write location three different
ways and none of them are consistent:

    "Chicago, IL"                       -> US, but never says so
    "Remote - United Kingdom"           -> not US, but says "Remote"
    "Remote - US and Canada"            -> US, despite naming another country
    "San Francisco, CA * New York, NY"  -> US, multi-site
    "Worldwide"                         -> open to US applicants

So: look for a positive US signal first, and only reject when a listing names
somewhere else *and* gives no US signal. That ordering is what keeps
"US and Canada" in and "United Kingdom" out.
"""

from __future__ import annotations

import re
import unicodedata

STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico",
}

# Matched against the ORIGINAL-case string: lowercasing first would make "OR"
# match the word "or" in "Remote or Hybrid", and "IN" match "in".
ABBR = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|"
    "MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|"
    "WI|WY|DC"
)
ABBR_RE = re.compile(r",\s*(?:" + ABBR + r")\b")

COUNTRY_RE = re.compile(r"\b(?:united states|usa|u\.s\.a?\.?|us)\b", re.I)

# Boards often give a bare city with no state or country ("San Francisco").
US_CITIES = {
    "san francisco", "new york city", "nyc", "los angeles", "chicago",
    "boston", "seattle", "austin", "denver", "atlanta", "dallas", "houston",
    "philadelphia", "phoenix", "san diego", "san jose", "santa clara",
    "sunnyvale", "mountain view", "palo alto", "cupertino", "bellevue",
    "redmond", "portland", "miami", "orlando", "tampa", "nashville",
    "charlotte", "raleigh", "durham", "pittsburgh", "detroit", "minneapolis",
    "salt lake city", "las vegas", "kansas city", "st. louis", "columbus",
    "indianapolis", "baltimore", "washington dc", "arlington", "brooklyn",
    "cambridge", "ann arbor", "boulder", "irvine", "oakland", "sacramento",
}

# Named elsewhere. Only rejects when no US signal is present.
ELSEWHERE = {
    "united kingdom", "uk", "england", "scotland", "ireland", "france",
    "germany", "spain", "portugal", "italy", "netherlands", "belgium",
    "sweden", "norway", "denmark", "finland", "poland", "romania", "czech",
    "switzerland", "austria", "greece", "turkey", "israel", "india",
    "singapore", "japan", "china", "hong kong", "korea", "australia",
    "new zealand", "canada", "mexico", "brazil", "argentina", "colombia",
    "chile", "costa rica", "south africa", "nigeria", "kenya", "egypt",
    "dubai", "uae", "emea", "apac", "latam", "europe", "asia", "africa",
    "latin america", "south america", "middle east", "london", "berlin",
    "paris", "amsterdam", "dublin", "bengaluru", "bangalore", "toronto",
    "vancouver", "sydney", "melbourne", "tokyo", "singapore city",
    # Cities that name no country of their own. "Sao Paulo" is the one that
    # actually leaked through in testing.
    "sao paulo", "rio de janeiro", "mexico city", "guadalajara",
    "buenos aires", "bogota", "santiago", "lima", "montreal", "ottawa",
    "calgary", "madrid", "barcelona", "lisbon", "milan", "rome", "munich",
    "frankfurt", "hamburg", "cologne", "dusseldorf", "stuttgart", "zurich",
    "geneva", "vienna", "brussels", "rotterdam", "copenhagen", "stockholm",
    "oslo", "helsinki", "warsaw", "krakow", "prague", "budapest",
    "bucharest", "sofia", "athens", "istanbul", "kyiv", "tel aviv",
    "manchester", "birmingham", "leeds", "edinburgh", "glasgow", "belfast",
    "cork", "cardiff", "mumbai", "delhi", "new delhi", "hyderabad",
    "chennai", "pune", "gurgaon", "noida", "seoul", "shanghai", "beijing",
    "shenzhen", "taipei", "bangkok", "manila", "jakarta", "kuala lumpur",
    "ho chi minh", "hanoi", "osaka", "brisbane", "perth", "adelaide",
    "auckland", "wellington", "johannesburg", "cape town", "lagos",
    "nairobi", "cairo", "riyadh", "doha", "abu dhabi",
}

# No country named, but a US applicant can take the job.
GLOBAL_OK = {"worldwide", "anywhere", "global", "north america", "americas"}

# US regional descriptors with no city/state/country token of their own
# ("West Coast (Remote)", "Southeast Territory") — real strings seen on
# Greenhouse boards. Treated as a US signal for country resolution; a
# non-US company using these same words for its own region is exceedingly
# rare on US-focused job boards.
US_REGIONS = {
    "west coast", "east coast", "gulf coast", "pacific northwest",
    "midwest", "northeast", "southeast", "southwest", "mid-atlantic",
}

# Non-US countries worth naming explicitly for the "Remote, <country>" display.
# Deliberately excludes ambiguous names that collide with US states in this
# dataset ("Georgia" the country vs. the US state — the state reading always
# wins here since job-agent is a US-focused search).
COUNTRY_CODES = {
    "united states": "US", "united kingdom": "UK", "canada": "CA",
    "ireland": "IE", "germany": "DE", "france": "FR", "india": "IN",
    "australia": "AU", "israel": "IL", "singapore": "SG", "mexico": "MX",
    "brazil": "BR", "japan": "JP", "netherlands": "NL", "spain": "ES",
    "poland": "PL", "portugal": "PT", "italy": "IT", "south korea": "KR",
    "new zealand": "NZ", "south africa": "ZA", "switzerland": "CH",
    "austria": "AT", "belgium": "BE", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "uruguay": "UY", "colombia": "CO",
    "argentina": "AR", "chile": "CL", "costa rica": "CR",
    "united arab emirates": "AE", "uae": "AE",
}

STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}

# Curated hub-city -> state fallback, for postings that name a city with no
# state ("USA - Austin (dbt)", "San Francisco"). Same city list as US_CITIES.
CITY_STATE = {
    "san francisco": "CA", "new york city": "NY", "nyc": "NY",
    "los angeles": "CA", "chicago": "IL", "boston": "MA", "seattle": "WA",
    "austin": "TX", "denver": "CO", "atlanta": "GA", "dallas": "TX",
    "houston": "TX", "philadelphia": "PA", "phoenix": "AZ",
    "san diego": "CA", "san jose": "CA", "santa clara": "CA",
    "sunnyvale": "CA", "mountain view": "CA", "palo alto": "CA",
    "cupertino": "CA", "bellevue": "WA", "redmond": "WA", "portland": "OR",
    "miami": "FL", "orlando": "FL", "tampa": "FL", "nashville": "TN",
    "charlotte": "NC", "raleigh": "NC", "durham": "NC", "pittsburgh": "PA",
    "detroit": "MI", "minneapolis": "MN", "salt lake city": "UT",
    "las vegas": "NV", "kansas city": "MO", "st. louis": "MO",
    "columbus": "OH", "indianapolis": "IN", "baltimore": "MD",
    "washington dc": "DC", "arlington": "VA", "brooklyn": "NY",
    "cambridge": "MA", "ann arbor": "MI", "boulder": "CO", "irvine": "CA",
    "oakland": "CA", "sacramento": "CA",
}

_REMOTE_TOKENS = {"remote", "us", "usa", "united states", "north america",
                   "anywhere", "worldwide", "hq"}

# .title() mangles multi-word names with an abbreviation-like part
# ("washington dc" -> "Washington Dc", "nyc" -> "Nyc").
_CITY_DISPLAY = {"nyc": "New York City", "washington dc": "Washington DC"}


def _clean_city(s: str) -> str | None:
    s = s.strip()
    s = re.sub(r"^\(?\s*remote\s*[\(\-:]*\s*", "", s, flags=re.I)
    s = re.sub(r"^\(?\s*u\.?s\.?a?\.?\s*[\-:]\s*", "", s, flags=re.I)
    s = s.strip(" -:()").strip()
    if not s or _fold(s) in _REMOTE_TOKENS:
        return None
    return s


def parse_us_location(location: str | None) -> tuple[str | None, str | None, str | None]:
    """Best-effort split of a free-text listing location into (city, state, country).

    Heuristic, not a geocoder — job boards write location a dozen different
    ways (see the docstring up top). Built and tested against every distinct
    location string actually seen in jobs.db as of 2026-07-30. `state` is
    always a 2-letter USPS abbreviation. Ambiguous or bare "remote"/"global"
    strings intentionally leave fields as None rather than guess.
    """
    raw = (location or "").strip()
    if not raw:
        return (None, None, None)

    seg = raw.split(";")[0].strip()
    city: str | None = None
    state: str | None = None

    # Pass 1: comma-separated tokens — the token right before a recognized
    # state (full name or abbreviation) is the city, if it isn't a remote marker.
    # Start at token 1, never 0: the first token is always the city/remote
    # marker position, never the state — matters for "New York, New York,
    # United States", where the city name is itself also a state name.
    tokens = [t.strip() for t in seg.split(",")]
    for i, tok in enumerate(tokens[1:], start=1):
        cleaned = tok.strip(" ()").strip()
        fold_tok = _fold(cleaned)
        if fold_tok in STATE_ABBR:
            state = STATE_ABBR[fold_tok]
        elif cleaned.upper() in ABBR.split("|"):
            state = cleaned.upper()
        if state:
            city = _clean_city(tokens[i - 1])
            break

    # Pass 2: no comma-adjacent state found — try a known hub city anywhere
    # in the segment (handles "USA - Austin (dbt)", "San Francisco HQ").
    if not state:
        fold_seg = _fold(seg)
        for city_name, abbr in CITY_STATE.items():
            if _word(city_name, fold_seg):
                city, state = _CITY_DISPLAY.get(city_name, city_name.title()), abbr
                break

    # Pass 3: a bare state name/abbreviation with nothing else ("Texas").
    if not state:
        fold_seg = _fold(seg)
        for state_name, abbr in STATE_ABBR.items():
            if _word(state_name, fold_seg):
                state = abbr
                break

    country = None
    fold_raw = _fold(raw)
    if (state or city or COUNTRY_RE.search(raw) or ABBR_RE.search(raw)
            or any(_word(r, fold_raw) for r in US_REGIONS)):
        country = "United States"
    else:
        for name, code in COUNTRY_CODES.items():
            if name in ("united states", "uae") or not _word(name, fold_raw):
                continue
            country = name.title()
            break
        if not country and _word("uae", fold_raw):
            country = "United Arab Emirates"
        if not country and re.search(r"\bUK\b", raw):
            country = "United Kingdom"

    return (city, state, country)


def remote_label(city: str | None, state: str | None, country: str | None) -> str | None:
    """"Remote, US" / "Remote, UK" style label for postings with no specific
    city or state — the common case for fully-remote listings. Returns None
    when a real city/state exists (caller should show those instead) or when
    no country signal was found at all (genuinely unclassifiable, e.g. vague
    multi-region strings like "Northern America, Europe, APAC")."""
    if city or state or not country:
        return None
    code = COUNTRY_CODES.get(_fold(country), country)
    return f"Remote, {code}"


def _word(term: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


def _fold(text: str) -> str:
    """Strip diacritics so "São Paulo" matches "sao paulo"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def classify(location: str | None) -> str:
    """Return "us", "elsewhere", or "unknown"."""
    raw = (location or "").strip()
    if not raw:
        return "unknown"
    low = _fold(raw)

    us = bool(COUNTRY_RE.search(raw)) or bool(ABBR_RE.search(raw))
    if not us:
        us = any(_word(state, low) for state in STATES) or any(
            _word(city, low) for city in US_CITIES
        ) or any(_word(region, low) for region in US_REGIONS)

    elsewhere = any(_word(term, low) for term in ELSEWHERE)

    if us:
        # A US signal wins even alongside other countries ("US and Canada"),
        # since the role is open to US applicants either way.
        return "us"
    if elsewhere:
        return "elsewhere"
    if any(_word(term, low) for term in GLOBAL_OK):
        return "us"
    return "unknown"
