"""Adapter normalization tests. Run: python test_sources.py

sources.py is the most fragile code here: eleven integrations, each parsing a
third-party response shape nobody controls. Every quirk asserted below was
found by running against a live API and getting it wrong first — they're
regression tests for real bugs, not hypotheticals.

Fixtures under tests/fixtures/ are trimmed real responses, served through an
httpx MockTransport, so this suite is hermetic: no network, same result in CI.
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobagent import sources  # noqa: E402

FIX = Path(__file__).parent / "tests" / "fixtures"
REQUIRED = {"source", "company", "title", "location", "url", "description", "posted_at"}

CHECKS = []
requested: list[str] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def client_for(routes: dict):
    """routes: substring of the URL -> payload (dict/list) or raw bytes."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        for needle, payload in routes.items():
            if needle in url:
                if isinstance(payload, (bytes, bytearray)):
                    return httpx.Response(200, content=payload)
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": f"unrouted {url}"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def assert_schema(rows, *, want_department=True):
    assert rows, "adapter yielded nothing"
    for r in rows:
        missing = REQUIRED - set(r)
        assert not missing, f"missing keys {missing} in {r.get('title')!r}"
        assert isinstance(r["url"], str) and r["url"], "url must be a non-empty string"
        assert isinstance(r["description"], str), "description must be a string"
        if want_department:
            assert "department" in r, "ATS adapters should carry department"


# ---------- per-adapter ----------

@check("greenhouse: unescapes the HTML-escaped content field")
def _greenhouse():
    rows = list(sources.fetch_greenhouse(
        client_for({"boards-api.greenhouse.io": load("greenhouse")}), "honeycomb"))
    assert_schema(rows)
    assert all(r["source"] == "greenhouse:honeycomb" for r in rows)
    # Greenhouse double-escapes: raw content has &lt;p&gt; / &amp;. clean() must
    # unescape *and* strip tags, so no markup or entity survives to the DB.
    for r in rows:
        d = r["description"]
        assert "&lt;" not in d and "&amp;" not in d, f"entities survived: {d[:80]}"
        assert "<p>" not in d and "<div" not in d, f"tags survived: {d[:80]}"


@check("lever: converts epoch-ms createdAt to an ISO timestamp")
def _lever():
    rows = list(sources.fetch_lever(
        client_for({"api.lever.co": load("lever")}), "jumpcloud"))
    assert_schema(rows)
    stamped = [r for r in rows if r["posted_at"]]
    assert stamped, "expected at least one dated posting"
    for r in stamped:
        assert isinstance(r["posted_at"], str) and "T" in r["posted_at"], r["posted_at"]
        assert not r["posted_at"].isdigit(), "raw epoch leaked instead of ISO"


@check("ashby: falls back from descriptionPlain to descriptionHtml")
def _ashby():
    raw = load("ashby")
    for j in raw["jobs"]:
        j["descriptionPlain"] = ""          # force the fallback path
    rows = list(sources.fetch_ashby(client_for({"api.ashbyhq.com": raw}), "render"))
    assert_schema(rows)
    assert any(r["description"] for r in rows), "html fallback produced nothing"


@check("smartrecruiters: follows offset pagination instead of one page")
def _smartrecruiters():
    requested.clear()

    def handler(request):
        url = str(request.url)
        requested.append(url)
        if "/postings/" in url:                      # per-posting detail
            return httpx.Response(200, json=load("smartrecruiters_detail"))
        if "offset=0" in url:
            return httpx.Response(200, json=load("smartrecruiters_page0"))
        return httpx.Response(200, json=load("smartrecruiters_page1"))

    c = httpx.Client(transport=httpx.MockTransport(handler))
    rows = list(sources.fetch_smartrecruiters(c, "AutomationAnywhere1"))
    assert_schema(rows)
    lists = [u for u in requested if "/postings?" in u]
    assert len(lists) >= 2, f"only paged once ({lists}) — a big board would truncate"
    # The description lives only on the detail endpoint.
    assert any(r["description"] for r in rows), "detail fetch produced no description"


@check("recruitee: skips unpublished offers and labels remote ones")
def _recruitee():
    raw = load("recruitee")
    raw["offers"][0]["status"] = "draft"        # must not be yielded
    draft_title = raw["offers"][0]["title"]
    raw["offers"][1]["remote"] = True
    raw["offers"][1]["country"] = "United States"
    rows = list(sources.fetch_recruitee(
        client_for({"recruitee.com": raw}), "aikidosecurity"))
    assert_schema(rows)
    assert all(r["title"] != draft_title for r in rows), "draft offer leaked through"
    assert any(r["location"].startswith("Remote - ") for r in rows)


@check("workable: requests ?details=true, without which descriptions vanish")
def _workable():
    requested.clear()
    rows = list(sources.fetch_workable(
        client_for({"apply.workable.com": load("workable")}), "vasion"))
    assert_schema(rows)
    assert any("details=true" in u for u in requested), \
        "the plain widget endpoint silently omits description entirely"
    assert any(r["description"] for r in rows)


@check("comeet: keeps multi-location variants of one req distinct")
def _comeet():
    rows = list(sources.fetch_comeet(
        client_for({"comeet.co": load("comeet")}), "wiliot", "F6.003", "tok"))
    assert_schema(rows)
    assert len(rows) >= 2, "fixture should hold two location variants"
    urls = [r["url"] for r in rows]
    assert len(set(urls)) == len(urls), (
        "variants share a url — the url-unique constraint would collapse them "
        "and silently drop every city but one")
    locs = {r["location"] for r in rows}
    assert len(locs) >= 2, f"locations should differ: {locs}"


@check("remoteok: skips the legal notice occupying index 0")
def _remoteok():
    rows = list(sources.fetch_remoteok(client_for({"remoteok.com": load("remoteok")})))
    assert_schema(rows, want_department=False)
    raw = load("remoteok")
    assert "legal" in raw[0], "fixture should retain the notice element"
    assert all(r["title"] for r in rows), "notice element parsed as a job"
    assert len(rows) == len(raw) - 1, "wrong number of jobs — notice not skipped"


@check("himalayas: pages by rows actually returned, not the limit requested")
def _himalayas():
    requested.clear()
    page = load("himalayas")
    n = len(page["jobs"])

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json=page)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    rows = list(sources.fetch_himalayas(c, limit=n * 2))
    assert_schema(rows, want_department=False)
    # The API caps pages at 20 no matter what ?limit says. Stepping the offset
    # by the *requested* size skipped most of every page.
    offsets = [u.split("offset=")[1].split("&")[0] for u in requested]
    assert offsets[:2] == ["0", str(n)], \
        f"offset must advance by rows returned ({n}), got {offsets[:2]}"


@check("rss: splits 'Company: Role' titles when company_from_title is set")
def _rss():
    raw = (FIX / "feed.rss").read_bytes()
    rows = list(sources.fetch_rss(
        client_for({"example.test": raw}), "wwr", "https://example.test/f.rss", True))
    assert_schema(rows, want_department=False)
    assert any(r["company"] for r in rows), "company never extracted from title"
    for r in rows:
        assert not r["title"].startswith(f"{r['company']}:"), \
            f"company left embedded in title: {r['title']!r}"


@check("every adapter agrees on the same record shape")
def _common_schema():
    got = {}
    got["greenhouse"] = list(sources.fetch_greenhouse(
        client_for({"greenhouse": load("greenhouse")}), "x"))
    got["ashby"] = list(sources.fetch_ashby(client_for({"ashby": load("ashby")}), "x"))
    got["recruitee"] = list(sources.fetch_recruitee(
        client_for({"recruitee": load("recruitee")}), "x"))
    got["workable"] = list(sources.fetch_workable(
        client_for({"workable": load("workable")}), "x"))
    got["comeet"] = list(sources.fetch_comeet(
        client_for({"comeet": load("comeet")}), "x", "u", "t"))
    keysets = {name: set(rows[0]) for name, rows in got.items() if rows}
    first = next(iter(keysets.values()))
    for name, ks in keysets.items():
        assert ks == first, f"{name} diverges: {ks ^ first}"


def main() -> int:
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}\n       {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}\n       {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
