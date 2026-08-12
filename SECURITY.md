# Security

## Reporting a vulnerability

Please report privately rather than opening a public issue: use
[GitHub's private vulnerability reporting](https://github.com/michaeldinhmai/job-agent/security/advisories/new)
on this repo. I'll acknowledge within a few days. This is a personal
project, not a funded product — there's no bounty, but credit in the fix
commit is yours if you want it.

Only the current `master` is supported. There are no released versions to
backport to.

## Threat model

job-agent is a **single-user tool that runs on your own machine**. That
shapes what is and isn't defended:

**In scope**

- *Untrusted listing text.* Titles, descriptions, locations, and URLs all
  come from third-party job boards, and they land in the DOM. All rendering
  goes through `escapeHtml()` (which escapes quotes, not just `<>&`, because
  several call sites interpolate into attribute values) and every link goes
  through `safeHref()` (http/https allowlist, so an escaped `javascript:`
  URI can't still execute). Covered by `test_xss.js`.
- *Cross-origin requests to the local dashboard.* Binding to loopback does
  **not** stop a page open in another browser tab from POSTing to
  `127.0.0.1:5151`. Some endpoints are consequential — delete a listing, or
  launch a real browser pre-filled with your real profile data — so
  state-changing methods are rejected unless the `Origin` header matches the
  dashboard. Covered by `test_security.py`.
- *SQL injection.* All user-supplied values are parameterized. Column names
  can't be parameterized in SQLite, so the one place a column name is
  interpolated checks it against a fixed whitelist first. Covered by
  `test_security.py`.
- *Secret leakage into git.* `profile.json` (name, email, phone, LinkedIn),
  `config.json`, `variants.json`, `jobs.db`, `auth/`, `resume/`, and personal
  notes are all git-ignored. Only `.example.json` templates are tracked.

**Explicitly out of scope**

- *No authentication on the dashboard.* It binds to `127.0.0.1` and assumes
  anyone with local access to your account is you. Don't expose it — no
  port-forwarding, no `0.0.0.0`, no reverse proxy. `test_security.py`
  asserts the bind address and that Flask debug mode stays off.
- *Malicious `config.json` / `profile.json`.* These are your own files. They
  are not validated as hostile input.
- *Anything the ATS adapters submit.* By design the apply flow **stops before
  submitting** and hands you a filled form to review. A human reads every
  application before it goes out.

## Credential handling

No passwords are ever stored or read by this code. Where a site needs a
login (Workday, iCIMS), you sign in yourself once in a real browser window
and only the resulting **session cookies** are written to `auth/`, which is
git-ignored. Delete that directory to revoke.

API tokens that appear in `config.json` (for example a Comeet careers-widget
`uid`/`token`) are public identifiers a company embeds in its own public
careers page — the same role a Greenhouse board slug plays. They are not
credentials and grant no access beyond the public job list.

## Automated checks

Every push and pull request runs the four test suites, a CodeQL scan
(`python` and `javascript-typescript`, `security-extended` queries), and
`pip-audit` against `requirements.txt`. Dependabot watches both pip
dependencies and the pinned GitHub Actions.
