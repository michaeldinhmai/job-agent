"""Workday application assistant (Playwright).

Fills the application wizard and then STOPS. It never clicks Submit — the
browser stays open on the review screen for you to check every field, finish
any question it couldn't map, and submit yourself. That's a deliberate design
boundary, not a missing feature: an application goes out under your name.

Authentication works by session, not password. Run `login` once per employer:
a real browser opens, you sign in (or create the account) yourself, and only
the resulting cookies are saved to auth/<tenant>.json. No credential ever
touches this code or the DB.

Workday tags its DOM with data-automation-id attributes that are stable across
tenants, which is what makes one adapter mostly portable. "Mostly": each
employer configures its own screening questions, so unmapped fields are
expected — the tool reports what it couldn't fill rather than guessing.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = ROOT / "auth"
PROFILE_PATH = ROOT / "profile.json"

# data-automation-id selectors, verified against nvidia.wd5 (July 2026).
AID = {
    "apply": '[data-automation-id="adventureButton"]',
    "apply_manually": '[data-automation-id="applyManually"]',
    "use_last_application": '[data-automation-id="useMyLastApplication"]',
    "description": '[data-automation-id="jobPostingDescription"]',
    "first_name": '[data-automation-id="legalNameSection_firstName"]',
    "last_name": '[data-automation-id="legalNameSection_lastName"]',
    "address_line1": '[data-automation-id="addressSection_addressLine1"]',
    "city": '[data-automation-id="addressSection_city"]',
    "postal": '[data-automation-id="addressSection_postalCode"]',
    "state_dd": '[data-automation-id="addressSection_countryRegion"]',
    "phone_type_dd": '[data-automation-id="phone-device-type"]',
    "phone_number": '[data-automation-id="phone-number"]',
    "source_dd": '[data-automation-id="sourceSection"] button',
    "prev_worked_dd": '[data-automation-id="previousWorker"]',
    "resume_input": '[data-automation-id="file-upload-input-ref"]',
    "next": 'button[data-automation-id="pageFooterNextButton"]',
    "errors": '[data-automation-id="errorBanner"]',
}


def load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def tenant_of(url: str) -> str:
    m = re.match(r"https://([^.]+)\.wd\d+\.myworkdayjobs\.com", url)
    if not m:
        raise ValueError(f"not a myworkdayjobs.com URL: {url}")
    return m.group(1)


def auth_path(tenant: str) -> Path:
    AUTH_DIR.mkdir(exist_ok=True)
    return AUTH_DIR / f"{tenant}.json"


def login(url: str) -> None:
    """Open a real browser for YOU to sign in; save only the session cookies.

    The session is snapshotted every couple of seconds, so finishing is just:
    sign in, then close the browser window. (No terminal interaction — this
    also has to work when driven from a non-interactive shell.)
    """
    tenant = tenant_of(url)
    path = auth_path(tenant)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)
        print(
            f"\nSign in to {tenant}'s careers site in the browser window "
            "(create the account there first if you don't have one).\n"
            "When you're signed in, just CLOSE the browser window — the "
            "session is saved automatically as you go."
        )
        from .applyflow import keep_session_until_closed

        keep_session_until_closed(context, browser, path)
    print(f"session saved to {path} — no password was stored.")


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
        report.append(("not on this page", label))


def _dropdown(page: Page, selector: str, option_text: str, label: str, report: list) -> None:
    """Workday dropdowns are buttons opening a listbox, not <select>."""
    if not option_text:
        report.append(("skipped (no value in profile)", label))
        return
    try:
        btn = page.locator(selector).first
        btn.wait_for(state="visible", timeout=4000)
        btn.click()
        opt = page.get_by_role("option", name=re.compile(re.escape(option_text), re.I)).first
        opt.wait_for(state="visible", timeout=4000)
        opt.click()
        report.append(("selected", label))
    except PWTimeout:
        page.keyboard.press("Escape")
        report.append(("could not select", label))


def apply(url: str, resume_pdf: str | None = None) -> None:
    """Drive the wizard up to — never past — the review step."""
    profile = load_profile()
    tenant = tenant_of(url)
    state = auth_path(tenant)
    if not state.exists():
        raise SystemExit(
            f"no saved session for {tenant!r}. Run:  python -m jobagent.workday login {url}"
        )

    report: list[tuple[str, str]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(state))
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        page.locator(AID["apply"]).first.click()
        # Prefer a fresh manual application; "use last" reuses stale answers.
        try:
            page.locator(AID["apply_manually"]).first.click(timeout=6000)
        except PWTimeout:
            pass  # some tenants go straight to the form

        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)  # Workday hydrates the form after load

        resume = resume_pdf or profile.get("resume_path", "")
        if resume and Path(resume).exists():
            try:
                page.locator(AID["resume_input"]).first.set_input_files(resume, timeout=6000)
                report.append(("uploaded", f"resume: {Path(resume).name}"))
                time.sleep(3)  # let the parser run before overwriting fields
            except PWTimeout:
                report.append(("no upload field on this page", "resume"))

        name = profile.get("name", {})
        addr = profile.get("address", {})
        phone = profile.get("phone", {})
        _fill(page, AID["first_name"], name.get("first", ""), "first name", report)
        _fill(page, AID["last_name"], name.get("last", ""), "last name", report)
        _fill(page, AID["address_line1"], addr.get("line1", ""), "address", report)
        _fill(page, AID["city"], addr.get("city", ""), "city", report)
        _fill(page, AID["postal"], addr.get("postal_code", ""), "postal code", report)
        _dropdown(page, AID["state_dd"], addr.get("state", ""), "state", report)
        _dropdown(page, AID["phone_type_dd"], phone.get("device_type", ""), "phone type", report)
        _fill(page, AID["phone_number"], phone.get("number", ""), "phone number", report)
        _dropdown(page, AID["source_dd"], profile.get("how_did_you_hear", ""), "source", report)

        print("\n--- fill report ---")
        for status, label in report:
            print(f"  {status:<28} {label}")
        print(
            "\nThe browser stays open. Walk the remaining steps yourself — "
            "check what the resume parser prefilled (it mangles dates and "
            "titles routinely), answer the employer's screening questions, "
            "and submit if and when it all looks right.\n"
            "Press Enter here when you're done to close the browser."
        )
        input()
        context.storage_state(path=str(state))  # keep the session fresh
        browser.close()


def main() -> None:
    import argparse
    import sys

    # Reports must reach a redirected log while the browser sits open waiting
    # for the user; block-buffered stdout holds them back until exit.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="sign in yourself; save only the session")
    p.add_argument("url")

    p = sub.add_parser("apply", help="fill the wizard, stop at review")
    p.add_argument("url")
    p.add_argument("--resume", help="override profile resume_path (use the tailored PDF)")

    args = parser.parse_args()
    if args.command == "login":
        login(args.url)
    else:
        apply(args.url, args.resume)


if __name__ == "__main__":
    main()
