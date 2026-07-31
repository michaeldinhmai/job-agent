"""Listing sources.

Every fetcher returns dicts with the same shape:
    source, company, title, location, url, description, posted_at

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
        yield {
            "source": f"greenhouse:{board}",
            "company": board,
            "title": job.get("title", ""),
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
            "location": cats.get("location", ""),
            "url": job.get("hostedUrl", ""),
            "description": clean(job.get("descriptionPlain") or job.get("description")),
            "posted_at": _ms_to_iso(job.get("createdAt")),
        }


def fetch_ashby(client: httpx.Client, board: str):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    for job in _get(client, url).json().get("jobs", []):
        yield {
            "source": f"ashby:{board}",
            "company": board,
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "url": job.get("jobUrl", ""),
            "description": clean(
                job.get("descriptionPlain") or job.get("descriptionHtml")
            ),
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

    with httpx.Client() as client:
        for label, fn in plan:
            if only and only not in label:
                continue
            try:
                yield label, [j for j in fn(client) if j["url"]], None
            except Exception as exc:  # a dead board shouldn't kill the run
                yield label, [], exc
