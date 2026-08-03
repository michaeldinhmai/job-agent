"""Command line entry point: python -m jobagent <command>"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import textwrap
import webbrowser
from pathlib import Path

from . import matcher, sources
from . import locations as geo
from . import salary as sal
from .db import Database, STATUSES

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        sys.exit(f"missing {path} — copy config.example.json to config.json "
                 "and edit it for your own search")
    return json.loads(path.read_text(encoding="utf-8"))


def _run_ingest(d: Database, config: dict, only: str | None = None):
    """Fetch + score all sources. Returns (new, seen, new_matches)."""
    floor = config.get("min_score", 0)
    new = seen = 0
    new_matches: list[dict] = []

    for label, jobs, error in sources.fetch_all(config, only=only):
        if error:
            print(f"  ! {label}: {type(error).__name__}: {error}")
            continue
        added = 0
        for job in jobs:
            job_id = d.jobs.upsert(job)
            if job_id is not None:
                value, reasons = matcher.score(job, config)
                d.jobs.set_score(job_id, value, reasons)
                added += 1
                if value >= floor:
                    new_matches.append({**job, "id": job_id, "score": value})
        d.commit()
        new, seen = new + added, seen + len(jobs)
        print(f"  + {label}: {added} new / {len(jobs)} listings")

    new_matches.sort(key=lambda j: -j["score"])
    return new, seen, new_matches


def cmd_ingest(args) -> None:
    config = load_config()
    with Database() as d:
        new, seen, _ = _run_ingest(d, config, only=args.source)
        print(f"\n{new} new listings ({seen} seen). Status: {d.jobs.counts()}")


def cmd_digest(args) -> None:
    """Scheduled-run entry point: ingest, then report new matches to a file."""
    from datetime import date

    from . import autotailor
    from .db import ROOT

    config = load_config()
    with Database() as d:
        new, seen, matches = _run_ingest(d, config)

    # Auto-tailor a resume for each new match, and enforce 30-day retention.
    for j in matches:
        try:
            autotailor.generate(j["id"])
        except SystemExit as exc:
            print(f"  ! tailor [{j['id']}]: {exc}")
    purged = autotailor.purge_old()
    if purged:
        print(f"purged {purged} tailored resume(s) past {autotailor.RETENTION_DAYS} days")

    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    out = reports / f"digest-{date.today().isoformat()}.md"

    lines = [
        f"# job-agent digest — {date.today().isoformat()}",
        "",
        f"{new} new listings ingested ({seen} seen). "
        f"**{len(matches)} new match(es)** at score >= {config.get('min_score', 0)}.",
        "",
    ]
    for j in matches:
        lines += [
            f"## [{j['id']}] {j['title']} — {j['company'] or j['source']}  (score {j['score']})",
            f"- {j['location'] or 'location n/a'}",
            f"- {j['url']}",
            f"- apply: `python -m jobagent.greenhouse apply {j['id']}`"
            if j["source"].startswith("greenhouse:") else "",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{len(matches)} new matches -> {out}")


def cmd_rescore(args) -> None:
    with Database() as d:
        n = matcher.rescore_all(d.jobs, load_config())
        d.commit()
    print(f"rescored {n} listings")


def cmd_list(args) -> None:
    config = load_config()
    floor = args.min_score if args.min_score is not None else config.get("min_score", 0)
    with Database() as d:
        rows = d.jobs.query(min_score=floor, status=args.status, limit=args.limit)

    if not rows:
        print("nothing matched — try a lower --min-score, or run ingest first")
        return

    for row in rows:
        company = row["company"] or row["source"]
        pct = matcher.to_percent(row["score"])
        where = (", ".join(p for p in (row["city"], row["state"]) if p)
                 or geo.remote_label(row["city"], row["state"], row["country"])
                 or row["location"] or "location n/a")
        pay = sal.format_salary(row["salary_min"], row["salary_max"])
        print(f"[{row['id']:>4}] match {pct:>3}/100  {row['title']}  —  {company}")
        print(f"        {where}  |  {row['status']}"
              + (f"  |  {pay}" if pay else "")
              + (f"  |  HM: {row['hiring_manager']}" if row["hiring_manager"] else ""))
        print(f"        {row['url']}")
        print(f"        why: {row['reasons']}\n")
    print(f"{len(rows)} shown (match >= {matcher.to_percent(floor)}/100)")


def cmd_show(args) -> None:
    with Database() as d:
        row = d.jobs.get(args.id)
    if not row:
        sys.exit(f"no listing with id {args.id}")
    print(f"{'title':>14}: {row['title']}")
    print(f"{'company':>14}: {row['company']}")
    print(f"{'department':>14}: {row['department'] or '—'}")
    print(f"{'location':>14}: {row['location'] or '—'}")
    print(f"{'city':>14}: {row['city'] or '—'}")
    print(f"{'state':>14}: {row['state'] or '—'}")
    print(f"{'country':>14}: {row['country'] or '—'}")
    print(f"{'salary':>14}: {sal.format_salary(row['salary_min'], row['salary_max']) or '—'}")
    for field in ("source", "reasons", "status", "hiring_manager", "posted_at", "url"):
        value = row[field]
        print(f"{field:>14}: {value if value is not None else '—'}")
    pct = matcher.to_percent(row["score"])
    print(f"{'match':>14}: {pct}/100 (raw {row['score']})")
    print("\n" + textwrap.fill(row["description"] or "", 88))


def cmd_set_hm(args) -> None:
    with Database() as d:
        if not d.jobs.get(args.id):
            sys.exit(f"no listing with id {args.id}")
        name = args.name or None
        d.jobs.set_hiring_manager(args.id, name)
        d.commit()
    print(f"{args.id} -> hiring_manager: {name or '(cleared)'}")


def cmd_mark(args) -> None:
    with Database() as d:
        d.jobs.set_status(args.id, args.status)
        d.commit()
    print(f"{args.id} -> {args.status}")


def cmd_delete(args) -> None:
    with Database() as d:
        row = d.jobs.get(args.id)
        if not row:
            sys.exit(f"no listing with id {args.id}")
        if not args.yes:
            confirm = input(f"Delete [{args.id}] {row['title']} @ {row['company']}? [y/N] ")
            if confirm.strip().lower() != "y":
                print("cancelled")
                return
        d.jobs.delete(args.id)
        d.commit()
    print(f"{args.id} deleted")


def cmd_open(args) -> None:
    with Database() as d:
        row = d.jobs.get(args.id)
    if not row:
        sys.exit(f"no listing with id {args.id}")
    webbrowser.open(row["url"])


ADAPTER_BY_SOURCE = {"greenhouse": "greenhouse", "ashby": "ashby"}
ADAPTER_BY_DOMAIN = {"greenhouse.io": "greenhouse", "ashbyhq.com": "ashby",
                     "icims.com": "icims", "myworkdayjobs.com": "workday"}


def _adapter_name_for(target: str, jobs) -> str:
    """Which ATS adapter handles this DB id or URL? `jobs` is a JobRepository."""
    if target.isdigit():
        row = jobs.get(int(target))
        if not row:
            sys.exit(f"no listing with id {target}")
        prefix = row["source"].split(":", 1)[0]
        name = ADAPTER_BY_SOURCE.get(prefix)
        if not name:
            sys.exit(f"listing {target} is from {row['source']!r} — no queueable "
                     "adapter (workday/icims need their session-login flow)")
        return name
    for domain, name in ADAPTER_BY_DOMAIN.items():
        if domain in target:
            if name in ("icims", "workday"):
                sys.exit(f"{name} applications need a login session first — use "
                         f"python -m jobagent.{name}")
            return name
    sys.exit(f"can't route {target!r} to an adapter")


def cmd_apply(args) -> None:
    """Route mixed targets to the right adapters, preserving order per ATS."""
    import importlib

    from . import applyflow

    with Database() as d:
        groups: dict[str, list[str]] = {}
        for t in args.targets:
            groups.setdefault(_adapter_name_for(t, d.jobs), []).append(t)

    for name, targets in groups.items():
        if len(groups) > 1:
            print(f"\n##### {name} ({len(targets)} application(s))")
        adapter = importlib.import_module(f".{name}", package=__package__)
        applyflow.run(adapter, targets, args.resume,
                      getattr(args, "headless", False),
                      getattr(args, "screenshot", None))


TASK_NAME = "job-agent daily digest"


def cmd_schedule(args) -> None:
    """Switch the daily automation on/off, or check it. Wraps schtasks."""
    import subprocess

    def schtasks(*extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["schtasks", *extra, "/tn", TASK_NAME],
            capture_output=True, text=True,
        )

    if args.state == "status":
        from .db import ROOT

        result = schtasks("/query", "/fo", "list", "/v")
        if result.returncode != 0:
            print("scheduled task not found — recreate it with:\n"
                  '  schtasks /create /f /tn "job-agent daily digest" '
                  f'/tr "{ROOT / "digest.bat"}" /sc daily /st 08:00')
            return
        wanted = ("Status:", "Next Run Time:", "Last Run Time:", "Last Result:",
                  "Scheduled Task State:")
        for line in result.stdout.splitlines():
            if line.strip().startswith(wanted):
                print(" ", line.strip())
        return

    flag = "/enable" if args.state == "on" else "/disable"
    result = schtasks("/change", flag)
    if result.returncode == 0:
        print(f"daily digest automation: {args.state.upper()}")
        if args.state == "off":
            print("(nothing runs by itself now — `python -m jobagent digest` "
                  "still works manually)")
    else:
        print(result.stderr.strip() or result.stdout.strip())


def cmd_tailor(args) -> None:
    """Compare your resume against one listing's job description."""
    from . import resume as rz
    from .db import ROOT

    with Database() as d:
        row = d.jobs.get(args.id)
    if not row:
        sys.exit(f"no listing with id {args.id}")

    config = load_config()
    vocab = config.get("resume_analysis", {}).get("vocab")
    vocab = set(vocab) if vocab else None

    profile_path = ROOT / "profile.json"
    if not profile_path.exists():
        sys.exit(f"missing {profile_path} — copy profile.example.json to "
                 "profile.json and fill in your own info")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    resume_path = args.resume or profile.get("resume_path", "")
    if not resume_path or not Path(resume_path).exists():
        sys.exit(f"resume not found: {resume_path!r} — set resume_path in profile.json")
    resume_text = rz.read_docx(resume_path)

    jd = row["description"] or ""
    if len(jd) < 500 and row["source"].startswith("workday:"):
        # Workday list rows carry no body; pull the posting detail live.
        import httpx

        from . import sources as src

        m = re.match(r"(https://([^.]+)\.wd\d+\.myworkdayjobs\.com)/en-US/([^/]+)(/.*)", row["url"])
        if m:
            base, tenant, site, path = m.groups()
            with httpx.Client() as client:
                jd = src._workday_description(client, base, tenant, site, path) or jd

    if len(jd) < 200:
        sys.exit("no usable job description for this listing")

    result = rz.analyze(resume_text, jd, vocab=vocab)
    print(f"{row['title']}  —  {row['company']}\n{row['url']}\n")
    print(f"JD emphasises {result['jd_terms']} distinct terms; "
          f"your resume already shows {result['coverage']:.0%} of them.\n")

    print("ALREADY ON YOUR RESUME (lead with these):")
    for term, w in result["matched"][:15]:
        print(f"  + {term}")

    print("\nIN THE JD, NOT ON YOUR RESUME:")
    print("  Reword real experience to use these terms where they genuinely apply.")
    print("  Anything you can't honestly claim, leave out — it's interview prep, not padding.\n")
    for term, w in result["missing"][:20]:
        print(f"  - {term}")


def cmd_export(args) -> None:
    config = load_config()
    floor = args.min_score if args.min_score is not None else config.get("min_score", 0)
    with Database() as d:
        rows = d.jobs.query(min_score=floor, status=args.status, limit=10_000)
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "score", "title", "company", "location", "status", "url"])
        for r in rows:
            writer.writerow([r["id"], r["score"], r["title"], r["company"],
                             r["location"], r["status"], r["url"]])
    print(f"wrote {len(rows)} rows to {out}")


# Packaged default — used whenever config.json has no "role_families" section.
# This is one example career-track taxonomy (presales/technical-account-management);
# it's meant to be overridden in config.json for whatever field you're job-hunting
# in. See config.example.json for the schema.
DEFAULT_ROLE_FAMILIES = [
    {"name": "TAM", "terms": [
        "technical account manager", "customer success engineer",
        "technical customer success manager", "client success engineer",
        "enterprise technical account manager", "support account manager",
        "professional services engineer", "post-sales engineer",
        "postsales engineer", "customer success architect",
        "technical relationship manager",
    ], "search_keywords": ["Technical Account Management", "Customer Success",
        "Customer Success Engineering"]},
    {"name": "SE", "terms": [
        "sales engineer", "sales engineering", "solutions engineer",
        "solution engineer", "solutions architect", "solution architect",
        "solutions consultant", "solution consultant", "presales",
        "pre-sales", "technical solutions", "customer engineer",
        "field engineer", "forward deployed engineer", "partner engineer",
        "partner solutions", "application engineer", "deployment engineer",
        "developer relations engineer",
    ], "search_keywords": ["Sales Engineering", "Solutions Engineering", "Presales"]},
    {"name": "Bridge", "terms": [
        "implementation engineer", "implementation consultant",
        "onboarding engineer", "technical support engineer",
        "enterprise support engineer", "customer support engineer",
        "delivery consultant", "professional services consultant",
    ], "search_keywords": ["Professional Services", "Implementation", "Technical Support",
        "Customer Support"]},
]
DEFAULT_FALLBACK_SEARCH_KEYWORDS = ["Sales Engineering", "Solutions Engineering",
                                    "Technical Account Management", "Customer Success"]

REPORT_LINE_RE = re.compile(
    r"[^.]*\b(?:report(?:s|ing)?\s+to|led\s+by|team\s+led\s+by)\b[^.]*\.?",
    re.IGNORECASE,
)


def _role_family(title: str, config: dict | None = None) -> tuple[str, list[str]]:
    cfg = (config or {}).get("role_families", {})
    families = cfg.get("families", DEFAULT_ROLE_FAMILIES)
    fallback = cfg.get("fallback_search_keywords", DEFAULT_FALLBACK_SEARCH_KEYWORDS)
    low = title.lower()
    for family in families:
        if any(matcher.has(t, low) for t in family["terms"]):
            return family["name"], family["search_keywords"]
    return "Unclassified", fallback


def _build_hm_search(job_id: int, title: str, company: str, description: str,
                     config: dict | None = None) -> dict:
    """Build the hiring-manager search package for one listing.

    x_ray_query is safe to actually execute (a sanctioned search, not a scrape
    of google.com or linkedin.com). linkedin_jobs_query / linkedin_posts_query
    stay query-only, by design — see README.md's "No LinkedIn automation" rule.
    """
    family, dept_keywords = _role_family(title or "", config)
    dept_or = " OR ".join(f'"{d}"' for d in dept_keywords)
    hits = [m.group(0).strip() for m in REPORT_LINE_RE.finditer(description or "")]
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "role_family": family,
        "x_ray_query": f'site:linkedin.com/in "{company}" "Manager" ({dept_or})',
        "x_ray_executable": True,
        "linkedin_jobs_query": f'"{title}" "{company}"',
        "linkedin_jobs_executable": False,
        "linkedin_posts_query": f'"{title}" "{company}"  (search under Posts, not Jobs)',
        "linkedin_posts_executable": False,
        "report_line_hits": hits[:3],
    }


def cmd_findhm(args) -> None:
    with Database() as d:
        row = d.jobs.get(args.id)
    if not row:
        sys.exit(f"no listing with id {args.id}")

    pkg = _build_hm_search(args.id, row["title"], row["company"] or "", row["description"] or "",
                          load_config())

    if args.json:
        print(json.dumps(pkg, indent=2))
        return

    print(f"[{pkg['id']}] {pkg['title']}  @  {pkg['company']}")
    print(f"role family: {pkg['role_family']}\n")

    print("Google X-ray search (executable — ask me to run this and I will):")
    print(f"  {pkg['x_ray_query']}\n")

    print("LinkedIn Jobs search (query only — paste into LinkedIn yourself):")
    print(f"  {pkg['linkedin_jobs_query']}\n")

    print("LinkedIn Posts search (query only — paste into LinkedIn yourself):")
    print(f"  {pkg['linkedin_posts_query']}\n")

    if pkg["report_line_hits"]:
        print("Reporting-line mentions found in the JD:")
        for h in pkg["report_line_hits"]:
            print(f"  - {textwrap.fill(h, 88, subsequent_indent='    ')}")
        print()
    else:
        print("No reporting-line language found in the JD text — check the ATS "
              "posting page itself for a named recruiter (often in page furniture "
              "not captured in the scraped description).\n")

    print("If nothing surfaces in ~10-15 min, apply cold and move on — see "
          "docs/outreach-playbook.md.")
    print(f'\nFound a name? Record it: python -m jobagent set-hm {args.id} "Name Here"')


def cmd_contact_add(args) -> None:
    with Database() as d:
        cid = d.contacts.add(
            company=args.company, name=args.name, title=args.title,
            channel=args.channel, contacted_at=args.date, outcome=args.outcome,
            follow_up=args.follow_up, listing_id=args.listing,
        )
        d.commit()
    print(f"logged contact {cid}: {args.name} @ {args.company}")


def cmd_contact_list(args) -> None:
    with Database() as d:
        rows = d.contacts.list(company=args.company, channel=args.channel)
    if not rows:
        print("no contacts logged yet — see: python -m jobagent contact add --help")
        return
    for r in rows:
        listing = f"  [listing {r['listing_id']}]" if r["listing_id"] else ""
        print(f"[{r['id']}] {r['contacted_at']}  {r['name']} ({r['title'] or 'title unknown'})"
              f"  @  {r['company']}{listing}")
        if r["channel"]:
            print(f"      via {r['channel']}")
        if r["outcome"]:
            print(textwrap.fill(r["outcome"], 88, initial_indent="      outcome: ",
                                subsequent_indent="               "))
        if r["follow_up"]:
            print(textwrap.fill(r["follow_up"], 88, initial_indent="      follow-up: ",
                                subsequent_indent="                 "))
        print()


def cmd_contact_show(args) -> None:
    with Database() as d:
        r = d.contacts.get(args.id)
    if not r:
        sys.exit(f"no contact with id {args.id}")
    for field in ("company", "name", "title", "channel", "contacted_at", "listing_id", "created_at"):
        print(f"{field:>12}: {r[field]}")
    print("\noutcome:")
    print(textwrap.fill(r["outcome"] or "(none logged)", 88))
    print("\nfollow-up:")
    print(textwrap.fill(r["follow_up"] or "(none logged)", 88))


def cmd_contact_update(args) -> None:
    with Database() as d:
        if not d.contacts.get(args.id):
            sys.exit(f"no contact with id {args.id}")
        fields = {k: v for k, v in {
            "outcome": args.outcome, "follow_up": args.follow_up,
            "title": args.title, "channel": args.channel,
        }.items() if v is not None}
        if not fields:
            sys.exit("nothing to update — pass at least one of "
                     "--outcome/--follow-up/--title/--channel")
        d.contacts.update(args.id, **fields)
        d.commit()
    print(f"updated contact {args.id}: {', '.join(fields)}")


def cmd_contact_delete(args) -> None:
    with Database() as d:
        row = d.contacts.get(args.id)
        if not row:
            sys.exit(f"no contact with id {args.id}")
        if not args.yes:
            confirm = input(f"Delete contact [{args.id}] {row['name']} @ {row['company']}? [y/N] ")
            if confirm.strip().lower() != "y":
                print("cancelled")
                return
        d.contacts.delete(args.id)
        d.commit()
    print(f"contact {args.id} deleted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobagent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="fetch listings from configured sources")
    p.add_argument("--source", help="only sources whose label contains this string")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("rescore", help="re-apply config.json rules to stored listings")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("digest", help="ingest + write a new-matches report (for the scheduled task)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("schedule", help="daily automation switch: on / off / status")
    p.add_argument("state", choices=["on", "off", "status"])
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("list", help="show ranked matches")
    p.add_argument("--min-score", type=int)
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="full detail for one listing")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("set-hm", help="record the hiring manager's name on a listing")
    p.add_argument("id", type=int)
    p.add_argument("name", help="hiring manager name, or \"\" to clear")
    p.set_defaults(func=cmd_set_hm)

    p = sub.add_parser("mark", help="set status on a listing")
    p.add_argument("id", type=int)
    p.add_argument("status", choices=STATUSES)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("delete", help="permanently delete a listing")
    p.add_argument("id", type=int)
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("open", help="open a listing in your browser")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("apply", help="apply to one listing (auto-routes to the right ATS)")
    p.add_argument("targets", nargs=1, help="DB listing id or job URL")
    p.add_argument("--resume")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--screenshot")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("queue", help="chain applications; mixed ATSes group automatically")
    p.add_argument("targets", nargs="+", help="DB listing ids or job URLs")
    p.add_argument("--resume")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("tailor", help="gap-report your resume against one listing")
    p.add_argument("id", type=int)
    p.add_argument("--resume", help="path to .docx (default: profile.json resume_path)")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("find-hm", help="build hiring-manager search templates for one listing")
    p.add_argument("id", type=int)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_findhm)

    p = sub.add_parser("contact", help="track networking contacts (warm intros, HM conversations)")
    contact_sub = p.add_subparsers(dest="contact_command", required=True)

    ca = contact_sub.add_parser("add", help="log a new contact")
    ca.add_argument("--company", required=True)
    ca.add_argument("--name", required=True)
    ca.add_argument("--title", help="their role/title, if known")
    ca.add_argument("--channel", help="e.g. LinkedIn, Slack, email, referral")
    ca.add_argument("--date", help="YYYY-MM-DD, defaults to today")
    ca.add_argument("--outcome", help="what happened / what they said")
    ca.add_argument("--follow-up", help="what to watch for / when to check back")
    ca.add_argument("--listing", type=int, help="linked jobs.db listing id, if any")
    ca.set_defaults(func=cmd_contact_add)

    cl = contact_sub.add_parser("list", help="show all logged contacts")
    cl.add_argument("--company", help="filter by company (substring match)")
    cl.add_argument("--channel", help="filter by channel (exact match)")
    cl.set_defaults(func=cmd_contact_list)

    cs = contact_sub.add_parser("show", help="full detail for one contact")
    cs.add_argument("id", type=int)
    cs.set_defaults(func=cmd_contact_show)

    cu = contact_sub.add_parser("update", help="update outcome/follow-up on a contact")
    cu.add_argument("id", type=int)
    cu.add_argument("--outcome")
    cu.add_argument("--follow-up")
    cu.add_argument("--title")
    cu.add_argument("--channel")
    cu.set_defaults(func=cmd_contact_update)

    cd = contact_sub.add_parser("delete", help="permanently delete a contact")
    cd.add_argument("id", type=int)
    cd.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    cd.set_defaults(func=cmd_contact_delete)

    p = sub.add_parser("export", help="dump matches to CSV")
    p.add_argument("--out", default="jobs.csv")
    p.add_argument("--min-score", type=int)
    p.add_argument("--status", choices=STATUSES)
    p.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
