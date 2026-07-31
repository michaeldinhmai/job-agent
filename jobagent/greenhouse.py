"""Greenhouse adapter: what's specific to Greenhouse forms.

The browser loop, queueing, submit detection, and CLI live in applyflow.
This module knows: how to find the form (hosted page, else embed endpoint),
Greenhouse's field ids, its combobox behavior, and its screening-question
label -> profile answer mapping.

Usage (or use `python -m jobagent apply/queue`, which routes automatically):
    python -m jobagent.greenhouse apply 634
    python -m jobagent.greenhouse queue 533 223 287
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, TimeoutError as PWTimeout

from .applyflow import Target, make_main

READY_SELECTOR = "#first_name"

# What Greenhouse shows after a successful submit (hosted and embed variants).
CONFIRM_RE = re.compile(
    r"thank you for applying|application has been (?:received|submitted)", re.I
)

# Screening-question label text -> profile.json answer. First match wins;
# anything unmatched is reported NEEDS YOU, never guessed. EEO/demographic
# fields are not question_* inputs and are never touched.
QUESTION_RULES: list[tuple[tuple[str, ...], str]] = [
    (("legally work", "authorized to work", "eligible to work",
      "work authorization", "eligible to legally", "authorized to reside"),
     "work_authorization_us"),
    (("sponsorship", "visa"), "require_sponsorship"),
    (("relocate", "relocation"), "willing_to_relocate"),
    (("linkedin",), "_linkedin"),
    (("how did you hear", "how you heard"), "_source"),
]


def resolve(target: str) -> Target:
    """DB id or greenhouse-ish URL -> candidate form URLs.

    Two candidates: the hosted board page, then the raw embed form — boards
    that disable hosting (Axonius, Cribl) redirect the first back to their own
    site, but /embed/job_app?for=X&token=Y still serves the form directly.
    """
    db_id, label, tailored = None, target, None
    if target.isdigit():
        from . import db

        conn = db.connect()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(target),)).fetchone()
        conn.close()
        if not row:
            raise SystemExit(f"no listing with id {target}")
        if not row["source"].startswith("greenhouse:"):
            raise SystemExit(f"listing {target} is {row['source']!r}, not greenhouse")
        db_id, url = row["id"], row["url"]
        board = row["source"].split(":", 1)[1]
        label = f"[{row['id']}] {row['title']} — {row['company']}"
        tailored = row["resume_path"]
    else:
        url = target
        m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?&]+)", url)
        board = m.group(1) if m else ""

    m = re.search(r"/jobs/(\d+)", url)
    job_id = m.group(1) if m else (parse_qs(urlparse(url).query).get("gh_jid") or [""])[0]
    if not (board and job_id):
        raise SystemExit(
            f"couldn't extract board/job id from {url!r}\n"
            "For postings embedded on a company site (…?gh_jid=…), pass the DB "
            "listing id instead — the board slug comes from the ingest source."
        )
    return Target(
        urls=[
            f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}",
            f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={job_id}",
        ],
        db_id=db_id,
        label=label,
        tailored_resume=tailored,
    )


def _fill(page: Page, selector: str, value: str, label: str, report: list) -> None:
    if not value:
        report.append(("skipped (no value in profile)", label))
        return
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=4000)
        el.fill(value)
        report.append(("filled", label))
    except PWTimeout:
        report.append(("field not found", label))


def _combo(page: Page, selector: str, value: str, label: str, report: list) -> None:
    """Greenhouse selects are comboboxes: click, then pick from the listbox.

    Tries progressively shorter prefixes because ATSes disagree on names for
    the same thing ("United States of America" in Workday is "United States"
    in Greenhouse's country list).
    """
    if not value:
        report.append(("skipped (no value in profile)", label))
        return
    words = value.split()
    candidates = [" ".join(words[:n]) for n in range(len(words), 0, -1)]
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=4000)
        for candidate in candidates:
            el.click()
            el.fill(candidate)
            opt = page.get_by_role("option", name=re.compile(re.escape(candidate), re.I)).first
            try:
                opt.wait_for(state="visible", timeout=2000)
                opt.click()
                report.append(("selected", label))
                return
            except PWTimeout:
                page.keyboard.press("Escape")
        report.append(("could not select", f"{label} (wanted {value!r})"))
    except PWTimeout:
        report.append(("could not select", f"{label} (wanted {value!r})"))


def _answer_for(label_text: str, profile: dict) -> str | None:
    low = label_text.lower()
    for needles, key in QUESTION_RULES:
        if any(n in low for n in needles):
            if key == "_linkedin":
                return profile.get("linkedin", "")
            if key == "_source":
                return profile.get("how_did_you_hear", "")
            value = profile.get("questions", {}).get(key)
            if isinstance(value, bool):
                return "Yes" if value else "No"
            return value
    return None


def _fill_questions(page: Page, profile: dict, report: list) -> None:
    """Walk every #question_* input, match its label, answer or report."""
    inputs = page.locator('input[id^="question_"], textarea[id^="question_"]')
    for i in range(inputs.count()):
        el = inputs.nth(i)
        qid = el.get_attribute("id") or ""
        label_loc = page.locator(f'label[for="{qid}"]')
        label_text = label_loc.first.inner_text().strip() if label_loc.count() else qid
        short = " ".join(label_text.split())[:70]

        answer = _answer_for(label_text, profile)
        if answer is None or answer == "":
            report.append(("NEEDS YOU", short))
            continue
        # Dropdown-style questions expose a listbox; plain text ones don't.
        if (el.get_attribute("role") or "") == "combobox" or el.get_attribute("aria-haspopup"):
            _combo(page, f"#{qid}", answer, short, report)
        else:
            _fill(page, f"#{qid}", answer, short, report)


def fill_form(page: Page, profile: dict, resume: str | None) -> list[tuple[str, str]]:
    report: list[tuple[str, str]] = []
    name = profile.get("name", {})
    phone = profile.get("phone", {})

    _fill(page, "#first_name", name.get("first", ""), "first name", report)
    _fill(page, "#last_name", name.get("last", ""), "last name", report)
    _fill(page, "#email", profile.get("email", ""), "email", report)
    _fill(page, "#phone", phone.get("number", ""), "phone", report)
    _combo(page, "#country", profile.get("address", {}).get("country", ""),
           "country", report)

    resume_file = resume or profile.get("resume_path", "")
    if resume_file and Path(resume_file).exists():
        try:
            page.locator("#resume").set_input_files(resume_file, timeout=5000)
            report.append(("uploaded", f"resume: {Path(resume_file).name}"))
            time.sleep(2)
        except PWTimeout:
            report.append(("field not found", "resume upload"))
    else:
        report.append(("MISSING FILE", f"resume: {resume_file!r}"))

    _fill_questions(page, profile, report)

    # The form is React-controlled and can drop values filled during
    # hydration (observed live: first_name and phone cleared after successful
    # fills). Read back and re-fill anything that got reset.
    recheck = {
        "#first_name": name.get("first", ""),
        "#last_name": name.get("last", ""),
        "#email": profile.get("email", ""),
        "#phone": phone.get("number", ""),
    }
    for sel, wanted in recheck.items():
        if not wanted:
            continue
        try:
            el = page.locator(sel).first
            if el.input_value(timeout=2000) != wanted:
                el.fill(wanted)
                report.append(("re-filled after reset", sel.lstrip("#")))
        except Exception:
            pass

    return report


import sys as _sys

main = make_main(_sys.modules[__name__], __doc__)

if __name__ == "__main__":
    main()
