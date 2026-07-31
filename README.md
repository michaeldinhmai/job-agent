# job-agent

A job-search pipeline for Michael's SE/presales transition. It discovers
listings from public job-board APIs, ranks them against tuned criteria,
tailors a resume per listing, and pre-fills application forms — **while a
human reviews and submits every application**.

Current status and work queue: **[TODO.md](TODO.md)** (the living tracker —
update it as things land).

![Pipeline flow: automated steps in teal, manual steps in purple](docs/flow.svg)

Teal = automated · purple = manual (open [docs/flow.svg](docs/flow.svg) in any
browser if your editor doesn't preview images). The purple boxes are
deliberate design, not gaps: an application goes out under a real name, so a
human reads every one before it does.

## Quickstart

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt && playwright install chromium

python -m jobagent ingest          # pull all sources into jobs.db
python -m jobagent list            # ranked matches
python -m jobagent apply 533       # pre-fill an application (you submit)
python -m jobagent.webapp          # local dashboard at http://127.0.0.1:5151
```

## Daily workflow

1. The **"job-agent daily digest"** scheduled task (8:00 AM) ingests all
   sources, auto-tailors a resume for each new match, and writes
   `reports/digest-YYYY-MM-DD.md` with ready-to-run commands.
2. Pick ids from the digest (or `python -m jobagent list`).
3. `python -m jobagent queue 533 914 223` — one browser window; each form
   arrives pre-filled, you finish the flagged items, pass the reCAPTCHA, and
   click Submit. The next form loads automatically.
4. Submissions are detected and marked `applied` in the DB — no bookkeeping.

## Command reference

All via `python -m jobagent <command>`:

| Command | What it does |
|---|---|
| `ingest [--source X]` | Fetch listings from configured sources into `jobs.db` |
| `digest` | Ingest + auto-tailor new matches + write the daily report (used by the scheduled task) |
| `schedule on\|off\|status` | The automation switch: enable/disable the 8 AM task, or see next/last run |
| `list [--min-score N] [--status S]` | Ranked matches, each with a `why:` score explanation |
| `show <id>` / `open <id>` | Full listing detail / open in browser |
| `apply <id-or-url>` | Pre-fill one application (auto-routes to the right ATS adapter) |
| `queue <ids...>` | Chain applications; mixed ATSes group automatically |
| `mark <id> <status>` | new / shortlist / applied / ignored / rejected |
| `tailor <id>` | Gap-report: which JD terms your resume shows / misses |
| `rescore` | Re-apply config.json rules without re-fetching |
| `export [--out f.csv]` | Dump matches to CSV |
| `find-hm <id>` | Build hiring-manager search templates (Google X-ray, LinkedIn) for one listing |
| `delete <id>` | Permanently delete a listing (`-y` to skip confirmation) |
| `contact add\|list\|show\|update\|delete` | Track networking contacts — separate from the applied/rejected lifecycle on `jobs` |

Adapter-specific (login flows, experimental ATSes):
`python -m jobagent.workday login <url>` · `python -m jobagent.icims login <url>`
· `python -m jobagent.autotailor generate <id> | batch | purge`

## Configuration

Three files, all in the repo root:

- **[config.json](config.json)** — what to search for: title include/exclude
  lists, weighted keywords (negative weights demote, e.g. `"senior": -3`),
  company excludes (big tech is out), US-only location policy, and the
  sources (RSS feeds + Greenhouse/Lever/Ashby/Workday boards). Comments in
  the file explain each knob. After editing: `python -m jobagent rescore`.
- **[profile.json](profile.json)** — your standard application answers:
  name, contact, LinkedIn, work authorization, sponsorship, base resume path.
  **Never passwords** — logins happen in a real browser, only session
  cookies persist (`auth/`, git-ignored).
- **[variants.json](variants.json)** — approved building blocks for the
  auto-tailor. Nothing goes on a generated resume that isn't in the base
  resume or approved here.

### Scoring model (matcher.py)

Title hit +10 · US location +3 · keyword in title = full weight · keyword in
description = +1 capped at +4 (so keyword-stuffed postings can't dominate) ·
word-boundary matching ("intern" doesn't reject "International Solutions
Engineer") · rejects are explained (`excluded title:`, `not US:`, ...).

### US-only classifier (locations.py)

Positive-signal-first: state names, `, XX` abbreviations, bare US cities,
accent-folded foreign cities. "Remote - US and Canada" stays; "Remote -
United Kingdom" goes; "Remote or Hybrid" is unknown (kept by default —
`unknown_ok` in config). Run `python test_locations.py` after touching it.

## Auto-tailored resumes (autotailor.py)

For each new match the digest classifies the JD into a domain — security /
observability / data / ai / devtools — and produces
`resume/tailored/<id>_<company>.docx` with CORE COMPETENCIES reordered to
lead with what that JD cares about. The path is recorded on the listing and
adapters attach it automatically (`--resume` overrides).

- **Assembly, not authorship**: the tailor only rearranges base-resume
  content and inserts variants.json blocks Michael approved. New wording is
  drafted by Claude and approved by Michael before it enters the pool.
- **Retention**: 30 days, purged by the digest — except resumes for
  `applied` listings, kept forever as the record of what was sent.

## ATS adapters

Shared machinery lives in [applyflow.py](jobagent/applyflow.py); each adapter
contributes only its ATS-specific parts (form discovery, field ids, quirks).
All adapters share the same contract:

> Fill what maps confidently, report everything (`NEEDS YOU` = yours),
> never touch EEO/demographic questions, and **never click Submit**. After
> you submit, the confirmation page is detected and the listing marked
> applied — detection only; a confirmation can only exist because you
> clicked.

| Adapter | Login | Status | Quirks handled |
|---|---|---|---|
| [greenhouse.py](jobagent/greenhouse.py) | none | verified live | hosted page vs `/embed/job_app` fallback (Axonius/Cribl disable hosting); React hydration wiping filled fields (read-back + re-fill); country name differences (prefix fallback) |
| [ashby.py](jobagent/ashby.py) | none | verified live | yes/no questions are hidden checkboxes driven by sibling Yes/No buttons — an explicit "No" is clicked, never left blank; custom dropdowns reported |
| [workday.py](jobagent/workday.py) | per-tenant account | parked | `data-automation-id` selectors; `total` only on first page of CXS API |
| [icims.py](jobagent/icims.py) | per-portal account | experimental | nested `icims_content_iframe`; tenant-customized wizards — expect to finish more fields yourself; report misses so it learns |

Login flows (Workday/iCIMS): `python -m jobagent.<ats> login <url>` opens a
real browser, **you** sign in, close the window, and only cookies are saved.

## Safety design (deliberate, not TODO)

- **No auto-submit.** Applications go out under Michael's name; a human reads
  every one. The submit click, the reCAPTCHA, and attestation questions
  (residence, conflicts of interest) are permanently manual.
- **No stored passwords.** Browser-session cookies only, git-ignored.
- **No resume fabrication.** Tailoring surfaces real experience in the JD's
  vocabulary; missing skills are interview prep, not padding.
- **No LinkedIn automation.** ToS ban + account risk; sources are public
  feeds and job-board APIs only.

## Project layout

```
jobagent/
  cli.py         commands + ATS routing        db.py       SQLite storage
  sources.py     RSS/Greenhouse/Lever/Ashby/   matcher.py  scoring rules
                 Workday fetchers              locations.py US classifier
  applyflow.py   shared apply machinery        resume.py   docx read + gap report
  greenhouse.py  ashby.py  workday.py  icims.py             autotailor.py
config.json  profile.json  variants.json  digest.bat
test_matcher.py  test_locations.py            (run both after rule changes)
jobs.db  reports/  logs/  resume/  auth/      (local artifacts, git-ignored)
```

## Troubleshooting

- **A source 404s**: the company moved ATS (Ramp/Plaid left Lever;
  Confluent/HashiCorp left Greenhouse). Remove or re-probe the slug.
- **Ranking looks wrong**: read the `why:` line — it names the rule. Tune
  config.json, `rescore`, repeat.
- **Form fill misses fields**: run with `--headless --screenshot s.png` to
  see what happened without opening a window; add label needles to the
  adapter's QUESTION_RULES.
- **Scheduled task**: `schtasks /query /tn "job-agent daily digest"`; output
  logs to `logs/digest.log`. Runs only while logged in; missed days catch up.
