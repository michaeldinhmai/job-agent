"""Listing sources.

Every fetcher returns dicts with the same shape:
    source, company, title, department, location, url, description, posted_at

`department` is best-effort — every ATS-specific fetcher below pulls it
straight from the API when the platform exposes it (all six currently do);
the broad/unscoped feeds (RSS, RemoteOK, Himalayas) leave it blank since
there's no reliable equivalent field.

Only public, machine-readable endpoints are used here — RSS feeds and the job
board APIs that Greenhouse/Lever/Ashby publish for their customers. Nothing in
this module logs in or scrapes behind an auth wall.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from . import locations as geo

TIMEOUT = httpx.Timeout(20.0)
HEADERS = {"User-Agent": "job-agent/0.1 (personal job search)"}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean(text: str | None, limit: int = 4000) -> str:
    """Strip HTML tags and collapse whitespace."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(text))).strip()[:limit]


def _get(client: httpx.Client, url: str) -> httpx.Response:
    resp = client.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def fetch_rss(client: httpx.Client, name: str, url: str, company_from_title: bool):
    """Parse a standard RSS 2.0 feed.

    Feeds disagree about where the employer lives: Remotive publishes a
    <company> element, We Work Remotely packs it into the title as
    "Company: Role" (hence company_from_title), others give you neither.
    """
    root = ET.fromstring(_get(client, url).content)
    for item in root.iterfind(".//item"):

        def text(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        title = text("title")
        company = text("company")
        if not company and company_from_title and ": " in title:
            company, title = (p.strip() for p in title.split(": ", 1))

        link = text("link")
        if not link or not title:
            continue

        yield {
            "source": name,
            "company": company,
            "title": title,
            "location": text("location") or text("region") or text("category"),
            "url": link,
            "description": clean(text("description")),
            "posted_at": text("pubDate"),
        }


def fetch_greenhouse(client: httpx.Client, board: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    for job in _get(client, url).json().get("jobs", []):
        depts = job.get("departments") or []
        yield {
            "source": f"greenhouse:{board}",
            "company": board,
            "title": job.get("title", ""),
            "department": ", ".join(d.get("name", "") for d in depts if d.get("name")),
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
            "description": clean(job.get("content")),
            "posted_at": job.get("updated_at"),
        }


def fetch_lever(client: httpx.Client, company: str):
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    for job in _get(client, url).json():
        cats = job.get("categories") or {}
        yield {
            "source": f"lever:{company}",
            "company": company,
            "title": job.get("text", ""),
            "department": cats.get("department", ""),
            "location": cats.get("location", ""),
            "url": job.get("hostedUrl", ""),
            "description": clean(job.get("descriptionPlain") or job.get("description")),
            "posted_at": _ms_to_iso(job.get("createdAt")),
        }


def fetch_ashby(client: httpx.Client, board: str):
    """Ashby's public job board.

    Two things the obvious implementation gets wrong, both measured against
    live boards rather than assumed:

    1. `location` is the role's ANCHOR office, not where it can be worked
       from. Ashby carries the remote fact separately in `isRemote` /
       `workplaceType`, and roughly half of all postings across the
       configured boards (1226 of 2469 when this was written) are remote
       while naming a city. Reading `location` alone throws that away, and a
       remote-or-local location gate then rejects a genuinely remote role.
       When a posting is remote the anchor city is deliberately dropped in
       favour of "Remote - <country>": locations.remote_label() only returns
       a label when city and state are both None, so keeping the city would
       defeat the very flag being recovered. The city is still in the JD and
       the posting URL.
    2. Compensation lives behind `?includeCompensation=true` and appears in
       a `compensation` object, NOT in the description — so salary parsing
       sees nothing without both the parameter and the append below.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
    for job in _get(client, url).json().get("jobs", []):
        raw_location = job.get("location") or ""
        remote = bool(job.get("isRemote")) or job.get("workplaceType") == "Remote"
        location = raw_location
        if remote:
            _, _, country = geo.parse_us_location(raw_location)
            if country:
                location = f"Remote - {country}"
            elif not raw_location:
                location = "Remote"
            # An unrecognised non-empty location is left as-is rather than
            # guessed at — a wrong country is worse than a missing flag.

        body = clean(job.get("descriptionPlain") or job.get("descriptionHtml"))
        pay = (job.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
        if pay:
            # Fed to salary.parse_salary() via the description, which is the
            # only place it looks.
            body = f"{body} Compensation: {pay}."

        yield {
            "source": f"ashby:{board}",
            "company": board,
            "title": job.get("title", ""),
            "department": job.get("department", ""),
            "location": location,
            "url": job.get("jobUrl", ""),
            "description": body,
            "posted_at": job.get("publishedAt"),
        }


def fetch_workday(
    client: httpx.Client,
    tenant: str,
    wd: int,
    site: str,
    search: str = "",
    max_pages: int = 5,
    descriptions: bool = False,
):
    """Query a Workday-hosted careers site.

    Every Workday employer runs its own tenant, but they all expose the same
    read-only CXS endpoint behind the careers page. No login required — that
    only comes in when you apply.

    Tenant/site come from the careers URL:
        nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
        ^tenant  ^wd                 ^site

    Pass `search` to filter server-side; these tenants list thousands of roles
    and paginate 20 at a time, so an unfiltered crawl is a lot of requests.
    """
    base = f"https://{tenant}.wd{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    limit = 20
    total = None

    for page in range(max_pages):
        resp = client.post(
            api,
            headers=headers,
            json={"appliedFacets": {}, "limit": limit, "offset": page * limit,
                  "searchText": search},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        postings = data.get("jobPostings", [])
        if not postings:
            return

        # Workday reports `total` on the first page only; later pages send 0.
        # Trusting it per-page silently truncates every tenant at 40 results.
        if total is None:
            total = data.get("total") or 0

        for job in postings:
            path = job.get("externalPath", "")
            if not path:
                continue
            body = ""
            if descriptions:
                body = _workday_description(client, base, tenant, site, path)
            yield {
                "source": f"workday:{tenant}",
                "company": tenant,
                "title": job.get("title", ""),
                "location": job.get("locationsText", ""),
                "url": f"{base}/en-US/{site}{path}",
                # The list endpoint carries no description; bulletFields is a
                # short teaser (usually the req id or location).
                "description": body or clean(" ".join(job.get("bulletFields") or [])),
                "posted_at": job.get("postedOn", ""),
            }

        if (page + 1) * limit >= total:
            return


def _workday_description(client, base: str, tenant: str, site: str, path: str) -> str:
    """One extra request per posting — only worth it when you need the body."""
    try:
        url = f"{base}/wday/cxs/{tenant}/{site}{path}"
        info = _get(client, url).json().get("jobPostingInfo", {})
        return clean(info.get("jobDescription"))
    except Exception:
        return ""


def fetch_smartrecruiters(client: httpx.Client, company: str):
    """SmartRecruiters public postings API.

    Paginates via limit/offset — a single `?limit=200` call was fine for
    every company added so far (all well under 200 open reqs), but a
    larger employer would silently truncate, so this loops until
    `totalFound` is covered rather than assuming one page is everyone.

    The list endpoint carries no description text at all — SmartRecruiters
    only exposes that on the per-posting detail endpoint — so this fetches
    one extra request per listing, same tradeoff as Workday's optional
    `descriptions=True`, but always on here: without it, salary parsing and
    every description-based keyword boost would be starved for every listing
    from this source.
    """
    limit = 200
    offset = 0
    total = None
    while total is None or offset < total:
        url = (f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
               f"?limit={limit}&offset={offset}")
        data = _get(client, url).json()
        total = data.get("totalFound", 0)
        content = data.get("content", [])
        if not content:
            return
        for job in content:
            job_id = job.get("id", "")
            if not job_id:
                continue
            loc = job.get("location") or {}
            dept = (job.get("department") or {}).get("label", "")
            yield {
                "source": f"smartrecruiters:{company}",
                "company": company,
                "title": job.get("name", ""),
                "department": dept,
                "location": loc.get("fullLocation") or ("Remote" if loc.get("remote") else ""),
                "url": f"https://jobs.smartrecruiters.com/{company}/{job_id}",
                "description": clean(_smartrecruiters_description(client, company, job_id)),
                "posted_at": job.get("releasedDate"),
            }
        offset += len(content)


def _smartrecruiters_description(client: httpx.Client, company: str, job_id: str) -> str:
    try:
        url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}"
        sections = _get(client, url).json().get("jobAd", {}).get("sections", {})
        return " ".join(s.get("text", "") for s in sections.values() if s.get("text"))
    except Exception:
        return ""


def fetch_recruitee(client: httpx.Client, company: str):
    url = f"https://{company}.recruitee.com/api/offers/"
    for job in _get(client, url).json().get("offers", []):
        if job.get("status") != "published":
            continue
        location = f"Remote - {job.get('country', '')}" if job.get("remote") else (
            job.get("location") or ""
        )
        yield {
            "source": f"recruitee:{company}",
            "company": job.get("company_name") or company,
            "title": job.get("title", ""),
            "department": job.get("department", ""),
            "location": location,
            "url": job.get("careers_url", ""),
            "description": clean(job.get("description")),
            "posted_at": job.get("published_at"),
        }


def fetch_comeet(client: httpx.Client, name: str, uid: str, token: str):
    """Comeet's public careers API.

    `uid` and `token` are the per-company values a Comeet customer embeds in
    its own public careers page (`COMEET.init({...})` in the page source) —
    public widget identifiers, not credentials, the same role a Greenhouse
    board slug plays.

    Two quirks, both found live against a real board rather than assumed:

    1. `&details=true` on the LIST endpoint is the only way to get
       description text in bulk. The per-position detail endpoint returns an
       empty `details` list, so the obvious "fetch each posting" approach
       silently yields no descriptions at all.
    2. A posting open in several cities appears once PER LOCATION, and every
       copy shares the same `url_active_page`. Keying on that would collapse
       them to one row under the url-unique constraint and arbitrarily drop
       the rest — so the per-location `url_comeet_hosted_page` is used
       instead, which is distinct (`18.767` vs `18.767-9B.20A`). That
       matters: it's exactly how a same-req-different-city listing stays
       visible.
    """
    url = (f"https://www.comeet.co/careers-api/2.0/company/{uid}"
           f"/positions?token={token}&details=true")
    for job in _get(client, url).json():
        if job.get("is_internal"):
            continue
        loc = job.get("location") or {}
        remote = job.get("workplace_type") == "Remote" or loc.get("is_remote")
        if remote:
            location = f"Remote - {loc.get('country') or ''}".strip(" -")
        else:
            location = ", ".join(
                p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p
            ) or (loc.get("name") or "")
        # `details` blocks are {name, value}; value is None on empty sections.
        body = " ".join(
            b.get("value") or "" for b in (job.get("details") or [])
        )
        yield {
            "source": f"comeet:{name}",
            "company": job.get("company_name") or name,
            "title": job.get("name", ""),
            "department": job.get("department") or "",
            "location": location,
            "url": job.get("url_comeet_hosted_page", ""),
            "description": clean(body),
            "posted_at": job.get("time_updated"),
        }


def fetch_workable(client: httpx.Client, company: str):
    """`?details=true` is what actually puts a description on each job — the
    plain widget endpoint the docs advertise omits it entirely."""
    url = f"https://apply.workable.com/api/v1/widget/accounts/{company}?details=true"
    for job in _get(client, url).json().get("jobs", []):
        if job.get("telecommuting"):
            location = f"Remote - {job.get('country', '')}"
        else:
            location = ", ".join(
                p for p in (job.get("city"), job.get("state"), job.get("country")) if p
            )
        yield {
            "source": f"workable:{company}",
            "company": company,
            "title": job.get("title", ""),
            "department": job.get("department", ""),
            "location": location,
            "url": job.get("url", ""),
            "description": clean(job.get("description")),
            "posted_at": job.get("published_on"),
        }


def fetch_remoteok(client: httpx.Client):
    """RemoteOK's public feed — global, unfiltered, every field/company.

    Item 0 of the response is a legal/attribution notice, not a job — skip
    it. No title/company scoping like the ATS fetchers: this exists to widen
    the net the same way the RSS feeds do, relying entirely on config.json's
    title/location/keyword rules to do the filtering.
    """
    for job in _get(client, "https://remoteok.com/api").json()[1:]:
        yield {
            "source": "remoteok",
            "company": job.get("company", ""),
            "title": job.get("position", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
            "description": clean(job.get("description")),
            "posted_at": job.get("date"),
        }


def fetch_himalayas(client: httpx.Client, limit: int = 500):
    """Himalayas' public feed. ~95k jobs total — this pulls only the most
    recent `limit`, not the whole index; there's no working server-side
    keyword/category filter as of 2026-07-31 (every query param tried was
    silently ignored), so config.json's rules do all the filtering, same
    as RemoteOK. Occasionally carries an internal test entry ("Backfill
    Test Role") — harmless, the matcher rejects it like anything else that
    doesn't fit.

    The API also silently caps at 20 items per request no matter what
    `?limit=` asks for (it just echoes the request back in the response
    without honoring it) — pagination here steps by however many items a
    page actually returned, not by the requested size, so it can't silently
    skip a stretch of results the way a fixed-size offset step would.
    """
    fetched = 0
    while fetched < limit:
        data = _get(client, f"https://himalayas.app/jobs/api?offset={fetched}").json()
        jobs = data.get("jobs", [])
        if not jobs:
            return
        for job in jobs:
            yield {
                "source": "himalayas",
                "company": job.get("companyName", ""),
                "title": job.get("title", ""),
                "location": ", ".join(job.get("locationRestrictions") or []),
                "url": job.get("guid", ""),
                "description": clean(job.get("description")),
                "posted_at": job.get("pubDate"),
            }
        fetched += len(jobs)


TORRE_SEARCH = "https://search.torre.co/opportunities/_search/"
TORRE_DETAIL = "https://torre.ai/api/suite/opportunities/{}"
TORRE_POST = "https://torre.ai/post/{}"
# Torre rejects an oversized page with HTTP 400 and the message "Request size
# by <user-agent> too large: N". Measured: 36 accepted, 40 refused every time,
# and the exact ceiling is undocumented and appears to vary by caller. 20 is
# comfortably inside it and costs nothing — search pages are a rounding error
# next to the per-posting detail fetches.
TORRE_MAX_PAGE = 20


def _torre_location(job: dict) -> str:
    """Torre states remoteness separately from the country list, the same way
    Ashby does — and for the same reason the country must survive while the
    city does not, see fetch_ashby."""
    countries = [c for c in (job.get("locations") or []) if c]
    place = job.get("place") or {}
    remote = bool(job.get("remote")) or place.get("remote")
    if not remote:
        return ", ".join(countries)
    if place.get("anywhere") or place.get("locationType") == "remote_anywhere":
        # Open to anyone anywhere, which includes the US.
        return "Remote - Worldwide"
    if countries:
        # Multi-country remote roles list every eligible country; keep them all
        # so locations.classify() sees a US signal when the US is among them.
        return "Remote - " + ", ".join(countries)
    return "Remote"


def _torre_pay(job: dict) -> str:
    """Render compensation as text for salary.parse_salary(), but only when
    it's a real USD annual figure. Torre carries SEK/JPY/ILS/EUR ranges too,
    and a 1,119,000 SEK salary read as dollars is far worse than no salary."""
    data = (job.get("compensation") or {}).get("data") or {}
    if data.get("currency") != "USD" or data.get("periodicity") != "yearly":
        return ""
    lo, hi = data.get("minAmount") or 0, data.get("maxAmount") or 0
    if not lo and not hi:
        return ""                      # "to-be-agreed" postings report 0.0
    lo, hi = int(lo), int(hi)
    if lo and hi and hi != lo:
        return f" Compensation: ${lo:,} - ${hi:,} per year."
    return f" Compensation: ${(lo or hi):,} per year."


def fetch_torre(client: httpx.Client, roles: list[str], per_role: int = 60,
                descriptions: bool = True):
    """Torre.ai's public opportunity search. No key or account needed.

    Three things this endpoint does that will bite an obvious implementation,
    all confirmed against the live API:

    1. `offset` is accepted and then IGNORED — `?offset=3` returns byte-for-byte
       the same page as `?offset=0`. Paging is cursor-based via
       `pagination.next`, passed back as `after=`. Trusting offset silently
       re-reads page one forever.
    2. The catalogue is ~305k postings and mostly irrelevant, so the
       server-side `skill/role` filter is not an optimisation, it's the only
       thing making this source usable — "solutions engineer" narrows it to
       ~1.6k.
    3. Descriptions are on a separate endpoint keyed by opportunity id, and
       the search result carries only a one-line `tagline`.
    """
    seen: set[str] = set()
    for role in roles:
        body = {"and": [{"skill/role": {"text": role,
                                        "experience": "potential-to-develop"}}]}
        cursor, pulled = None, 0
        while pulled < per_role:
            size = min(TORRE_MAX_PAGE, per_role - pulled)
            url = f"{TORRE_SEARCH}?size={size}&aggregate=false"
            if cursor:
                url += f"&after={cursor}"
            resp = client.post(url, json=body, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            page = resp.json()
            results = page.get("results", [])
            if not results:
                break
            for job in results:
                jid, slug = job.get("id"), job.get("slug")
                if not jid or not slug or jid in seen:
                    continue
                seen.add(jid)
                body_text = clean(job.get("tagline") or "")
                if descriptions:
                    body_text = _torre_description(client, jid) or body_text
                orgs = [o.get("name") for o in (job.get("organizations") or [])
                        if o.get("name")]
                yield {
                    "source": "torre",
                    "company": orgs[0] if orgs else "",
                    "title": job.get("objective", ""),
                    "department": "",
                    "location": _torre_location(job),
                    "url": TORRE_POST.format(slug),
                    "description": clean(body_text + _torre_pay(job)),
                    "posted_at": job.get("created"),
                }
            pulled += len(results)
            cursor = (page.get("pagination") or {}).get("next")
            if not cursor:
                break


def _torre_description(client: httpx.Client, job_id: str) -> str:
    """One extra request per posting; the search payload has no description."""
    try:
        det = _get(client, TORRE_DETAIL.format(job_id)).json()
        return " ".join(p.get("content") or "" for p in (det.get("details") or []))
    except Exception:
        return ""


def fetch_all(config: dict, only: str | None = None):
    """Yield (source_label, jobs, error) for each configured source."""
    src = config.get("sources", {})
    plan: list[tuple[str, callable]] = []

    for feed in src.get("rss", []):
        plan.append(
            (
                feed["name"],
                lambda c, f=feed: fetch_rss(
                    c, f["name"], f["url"], f.get("company_from_title", False)
                ),
            )
        )
    for board in src.get("greenhouse", []):
        plan.append((f"greenhouse:{board}", lambda c, b=board: fetch_greenhouse(c, b)))
    for company in src.get("lever", []):
        plan.append((f"lever:{company}", lambda c, x=company: fetch_lever(c, x)))
    for board in src.get("ashby", []):
        plan.append((f"ashby:{board}", lambda c, b=board: fetch_ashby(c, b)))
    for wd in src.get("workday", []):
        plan.append(
            (
                f"workday:{wd['tenant']}",
                lambda c, w=wd: fetch_workday(
                    c,
                    w["tenant"],
                    w.get("wd", 5),
                    w["site"],
                    w.get("search", ""),
                    w.get("max_pages", 5),
                    w.get("descriptions", False),
                ),
            )
        )
    for company in src.get("smartrecruiters", []):
        plan.append(
            (f"smartrecruiters:{company}", lambda c, x=company: fetch_smartrecruiters(c, x))
        )
    for company in src.get("recruitee", []):
        plan.append((f"recruitee:{company}", lambda c, x=company: fetch_recruitee(c, x)))
    for company in src.get("workable", []):
        plan.append((f"workable:{company}", lambda c, x=company: fetch_workable(c, x)))
    for cm in src.get("comeet", []):
        plan.append(
            (f"comeet:{cm['name']}",
             lambda c, m=cm: fetch_comeet(c, m["name"], m["uid"], m["token"]))
        )
    if src.get("remoteok"):
        plan.append(("remoteok", fetch_remoteok))
    himalayas = src.get("himalayas")
    if himalayas:
        opts = himalayas if isinstance(himalayas, dict) else {}
        plan.append(("himalayas", lambda c: fetch_himalayas(c, opts.get("limit", 500))))
    torre = src.get("torre")
    if torre and torre.get("roles"):
        plan.append(("torre", lambda c, t=torre: fetch_torre(
            c, t["roles"], t.get("per_role", 60), t.get("descriptions", True))))

    with httpx.Client() as client:
        for label, fn in plan:
            if only and only not in label:
                continue
            try:
                yield label, [j for j in fn(client) if j["url"]], None
            except Exception as exc:  # a dead board shouldn't kill the run
                yield label, [], exc
