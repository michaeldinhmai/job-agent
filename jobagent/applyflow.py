"""Shared machinery for the apply adapters (Greenhouse, Ashby, ...).

An adapter module plugs in by providing:
    READY_SELECTOR: str          — element that proves the form rendered
    CONFIRM_RE: re.Pattern       — what the ATS shows after a real submit
    resolve(target) -> Target    — DB id or URL -> candidate form URLs
    fill_form(page, profile, resume) -> [(status, item), ...]

Everything else — the browser loop, queueing, outcome detection, marking
applied, the CLI — lives here once.

Design rule shared by every adapter: the code fills and reports but NEVER
clicks Submit. Outcome detection is read-only; a confirmation page can only
exist because the human clicked Submit themself.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "profile.json"

# Report statuses that mean "a human must look at this".
ATTENTION_PREFIXES = ("NEEDS YOU", "could not", "field not found", "MISSING")


@dataclass
class Target:
    """One application to open: candidate URLs plus DB bookkeeping."""

    urls: list[str]
    db_id: int | None
    label: str
    tailored_resume: str | None = None


def load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_form(page: Page, urls: list[str], ready_selector: str) -> str:
    """Try candidate URLs until the form actually renders."""
    for url in urls:
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.locator(ready_selector).first.wait_for(state="visible", timeout=12000)
            return url
        except PWTimeout:
            continue
    raise SystemExit(
        "application form never appeared on any candidate URL:\n  " + "\n  ".join(urls)
    )


def print_report(report: list[tuple[str, str]]) -> int:
    print("--- fill report ---")
    for status, item in report:
        print(f"  {status:<26} {item}")
    needs = [i for s, i in report if s.startswith(ATTENTION_PREFIXES)]
    print(f"\n{len(needs)} item(s) need your attention before submitting.")
    return len(needs)


def wait_outcome(page: Page, browser, confirm_re) -> str:
    """Poll until the user submits (confirmation appears) or closes the window."""
    while browser.is_connected():
        try:
            if confirm_re.search(page.locator("body").inner_text(timeout=2000) or ""):
                return "submitted"
        except Exception:
            pass  # mid-navigation; try again next tick
        time.sleep(2)
    return "closed"


def mark_applied(db_id: int) -> None:
    from .db import Database

    with Database() as d:
        d.jobs.set_status(db_id, "applied")
        d.commit()


def keep_session_until_closed(context, browser, path: Path) -> None:
    """Login helper: snapshot cookies until the user closes the window."""
    try:
        while browser.is_connected():
            context.storage_state(path=str(path))
            time.sleep(2)
    except Exception:
        pass  # browser closed — the last snapshot stands


def run(adapter, targets: list[str], resume: str | None = None,
        headless: bool = False, screenshot: str | None = None) -> None:
    """Apply to targets in order through one browser window."""
    profile = load_profile()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_context().new_page()

        for pos, raw in enumerate(targets, 1):
            t: Target = adapter.resolve(raw)
            print(f"\n=== [{pos}/{len(targets)}] {t.label}")
            url = load_form(page, t.urls, adapter.READY_SELECTOR)
            print(f"form: {url}\n")

            # Precedence: explicit --resume > per-listing tailored > profile base.
            use_resume = resume or t.tailored_resume
            if t.tailored_resume and not resume:
                print(f"using tailored resume: {Path(t.tailored_resume).name}")

            report = adapter.fill_form(page, profile, use_resume)
            print_report(report)

            if screenshot:
                page.screenshot(path=screenshot, full_page=True)
                print(f"screenshot: {screenshot}")
            if headless:
                continue  # dry-run: never wait, never mark

            print(
                "\nReview, finish the remaining fields, pass the reCAPTCHA, and "
                "click Submit if it all looks right. I'll detect the "
                "confirmation and mark it applied.\n"
                "Close the browser window to skip/stop instead."
            )
            outcome = wait_outcome(page, browser, adapter.CONFIRM_RE)
            if outcome == "submitted":
                if t.db_id is not None:
                    mark_applied(t.db_id)
                    print(f"confirmation detected — marked {t.db_id} applied.")
                else:
                    print("confirmation detected. (No DB id — nothing to mark.)")
                time.sleep(1)
            else:
                print(
                    "window closed without a detected confirmation — nothing "
                    "marked. If you DID submit, run: python -m jobagent mark "
                    f"{t.db_id if t.db_id is not None else '<id>'} applied"
                )
                remaining = targets[pos:]
                if remaining:
                    print(f"queue stopped; not opened: {', '.join(remaining)}")
                return

        if not headless:
            print("\nqueue finished.")
        browser.close()


def make_main(adapter, doc: str):
    """Standard apply/queue CLI for an adapter module."""

    def main() -> None:
        import argparse
        import sys

        # Reports must reach a redirected log while the browser sits open
        # waiting for the user; block-buffered stdout holds them back.
        sys.stdout.reconfigure(line_buffering=True)

        parser = argparse.ArgumentParser(description=doc)
        sub = parser.add_subparsers(dest="command", required=True)

        p = sub.add_parser("apply", help="fill one application, stop before submit")
        p.add_argument("target", help="DB listing id or a job URL")
        p.add_argument("--resume", help="override the attached resume")
        p.add_argument("--headless", action="store_true",
                       help="selector dry-run: fill, report, close (nothing is sent)")
        p.add_argument("--screenshot", help="save a full-page screenshot here")

        q = sub.add_parser("queue", help="chain several applications through one window")
        q.add_argument("targets", nargs="+")
        q.add_argument("--resume")

        args = parser.parse_args()
        if args.command == "apply":
            run(adapter, [args.target], args.resume, args.headless, args.screenshot)
        else:
            run(adapter, args.targets, args.resume)

    return main
