"""Ashby adapter: what's specific to Ashby forms.

The browser loop, queueing, submit detection, and CLI live in applyflow.
This module knows Ashby's anatomy (verified live on jobs.ashbyhq.com/render,
2026-07-26):
- standard fields use _systemfield_* ids (name/email/resume/location)
- custom questions have UUID ids, identified only by their <label for=...>
- yes/no questions are hidden checkboxes whose real controls are sibling
  Yes/No BUTTONS — an explicit "No" must be clicked, not left blank
- EEOC demographics are radio groups — never touched
- reCAPTCHA at submit, which is yours

Usage (or use `python -m jobagent apply/queue`, which routes automatically):
    python -m jobagent.ashby apply 914
    python -m jobagent.ashby queue 914 823 847
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout

from .applyflow import Target, make_main

READY_SELECTOR = "#_systemfield_name"

CONFIRM_RE = re.compile(
    r"application (?:was |has been )?submitted|thank you for applying|"
    r"successfully submitted", re.I
)

EEOC_MARKER = "_systemfield_eeoc"

# label text -> profile answer. Yes/No answers drive the button pair.
QUESTION_RULES: list[tuple[tuple[str, ...], str]] = [
    (("legally authorized", "authorized to work", "eligible to work",
      "work authorization", "legally work"), "work_authorization_us"),
    (("sponsorship", "visa"), "require_sponsorship"),
    (("relocate", "relocation"), "willing_to_relocate"),
    (("linkedin",), "_linkedin"),
    (("website", "portfolio"), "_website"),
    (("how did you hear", "how you heard"), "_source"),
    (("phone",), "_phone"),
]


def resolve(target: str) -> Target:
    db_id, label, tailored = None, target, None
    if target.isdigit():
        from .db import Database

        with Database() as d:
            row = d.jobs.get(int(target))
        if not row:
            raise SystemExit(f"no listing with id {target}")
        if not row["source"].startswith("ashby:"):
            raise SystemExit(f"listing {target} is {row['source']!r}, not ashby")
        db_id, url = row["id"], row["url"]
        label = f"[{row['id']}] {row['title']} — {row['company']}"
        tailored = row["resume_path"]
    else:
        url = target
    if "ashbyhq.com" not in url:
        raise SystemExit(f"not an ashby URL: {url!r}")
    if not url.rstrip("/").endswith("/application"):
        url = url.rstrip("/") + "/application"
    return Target(urls=[url], db_id=db_id, label=label, tailored_resume=tailored)


def _answer_for(label_text: str, profile: dict) -> str | None:
    low = label_text.lower()
    for needles, key in QUESTION_RULES:
        if any(n in low for n in needles):
            if key == "_linkedin":
                return profile.get("linkedin", "")
            if key == "_website":
                return profile.get("website", "")
            if key == "_source":
                return profile.get("how_did_you_hear", "")
            if key == "_phone":
                return profile.get("phone", {}).get("number", "")
            value = profile.get("questions", {}).get(key)
            if isinstance(value, bool):
                return "Yes" if value else "No"
            return value
    return None


def _answer_yesno(page: Page, el, answer: str, short: str, report: list) -> None:
    """Ashby yes/no: the checkbox is display:none state storage; the real
    controls are sibling Yes/No buttons. An explicit "No" must CLICK No —
    leaving it untouched is unanswered, not answered-no."""
    try:
        container = el.locator("xpath=ancestor::div[1]")
        btn = container.get_by_role("button", name=re.compile(rf"^{answer}$", re.I)).first
        btn.click(timeout=3000)
        time.sleep(0.3)
        ok = el.is_checked() if answer == "Yes" else True
        report.append(
            (f"answered {answer}", short) if ok else ("could not answer", short)
        )
    except Exception:
        report.append(("could not answer", f"{short} (wanted {answer})"))


def fill_form(page: Page, profile: dict, resume: str | None) -> list[tuple[str, str]]:
    report: list[tuple[str, str]] = []
    name = profile.get("name", {})

    # Standard fields.
    full_name = f"{name.get('first', '')} {name.get('last', '')}".strip()
    try:
        page.locator("#_systemfield_name").first.fill(full_name, timeout=10000)
        report.append(("filled", "name"))
    except PWTimeout:
        report.append(("field not found", "name"))
    try:
        page.locator("#_systemfield_email").first.fill(profile.get("email", ""), timeout=4000)
        report.append(("filled", "email"))
    except PWTimeout:
        report.append(("field not found", "email"))

    resume_file = resume or profile.get("resume_path", "")
    if resume_file and Path(resume_file).exists():
        try:
            page.locator("#_systemfield_resume").set_input_files(resume_file, timeout=5000)
            report.append(("uploaded", f"resume: {Path(resume_file).name}"))
            time.sleep(2)
        except PWTimeout:
            report.append(("field not found", "resume upload"))
    else:
        report.append(("MISSING FILE", f"resume: {resume_file!r}"))

    # Location is an autocomplete; fill only if the profile has a city.
    addr = profile.get("address", {})
    try:
        loc = page.locator("#_systemfield_location").first
        loc.wait_for(state="attached", timeout=2000)
        if addr.get("city"):
            loc.fill(f"{addr['city']}, {addr.get('state', '')}".strip(", "))
            report.append(("filled", "location"))
        else:
            report.append(("NEEDS YOU", "location (no city in profile.json)"))
    except PWTimeout:
        pass

    # Everything else is label-driven: walk each label, act by input type.
    labels = page.locator("label[for]")
    for i in range(labels.count()):
        lab = labels.nth(i)
        target_id = lab.get_attribute("for") or ""
        if target_id.startswith("_systemfield_") or EEOC_MARKER in target_id:
            continue  # standard fields handled above; EEOC never touched
        text = " ".join((lab.inner_text() or "").split())
        if not text:
            continue
        short = text[:70]

        # Ashby quirk: checkboxes carry name= but no id=, and dropdowns are
        # custom widgets with neither. Try id, then name, then report.
        el = page.locator(f'[id="{target_id}"]').first
        try:
            el.wait_for(state="attached", timeout=1500)
        except PWTimeout:
            el = page.locator(f'[name="{target_id}"]').first
            try:
                el.wait_for(state="attached", timeout=1500)
            except PWTimeout:
                report.append(("NEEDS YOU (custom widget)", short))
                continue

        kind = (el.get_attribute("type") or el.evaluate("e => e.tagName") or "").lower()
        if kind == "radio":
            continue  # option labels / demographics
        answer = _answer_for(text, profile)

        if kind == "checkbox":
            if answer in ("Yes", "No"):
                _answer_yesno(page, el, answer, short, report)
            else:
                report.append(("NEEDS YOU", short))
        elif kind in ("text", "tel", "email", "textarea"):
            if answer:
                el.fill(answer)
                report.append(("filled", short))
            elif answer is None:
                report.append(("NEEDS YOU", short))
            else:
                report.append(("skipped (no value in profile)", short))
        elif kind == "file":
            continue  # cover letter etc. — yours to attach if wanted
        else:
            report.append(("NEEDS YOU", short))

    return report


import sys as _sys

main = make_main(_sys.modules[__name__], __doc__)

if __name__ == "__main__":
    main()
