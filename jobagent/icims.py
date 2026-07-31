"""iCIMS application assistant (Playwright). EXPERIMENTAL.

iCIMS is the messiest of the ATSes here: every employer runs its own portal
(careers-{tenant}.icims.com), the content lives inside a nested
icims_content_iframe, applying requires a per-portal candidate account, and
the wizard is customized per tenant. Verified so far (2026-07-26, live
against careersus-shure.icims.com): portal structure and iframe anchor.
The fill step is best-effort until run against a real target — expect to
finish more fields yourself than on Greenhouse/Ashby, and report gaps so the
adapter can learn.

Auth mirrors the Workday adapter: run `login` once per portal, sign in (or
create the candidate account) YOURSELF in the opened browser, close the
window, and only session cookies are stored (auth/icims-<tenant>.json).
No password ever touches this code.

Usage:
    python -m jobagent.icims login https://careers-acme.icims.com
    python -m jobagent.icims apply <job URL> [--resume path]
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = ROOT / "auth"
PROFILE_PATH = ROOT / "profile.json"

IFRAME = 'iframe#icims_content_iframe'

# Classic iCIMS field names seen across tenants; portals customize heavily,
# so misses are expected and reported rather than fatal.
FIELDS = {
    "input[name*='firstname' i]": ("name", "first"),
    "input[name*='lastname' i]": ("name", "last"),
    "input[name*='email' i]": ("email", None),
    "input[name*='phone' i]": ("phone", "number"),
    "input[name*='addressstreet' i]": ("address", "line1"),
    "input[name*='addresscity' i]": ("address", "city"),
    "input[name*='addresszip' i]": ("address", "postal_code"),
}


def load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def tenant_of(url: str) -> str:
    m = re.match(r"https://([^.]+)\.icims\.com", url)
    if not m:
        raise ValueError(f"not an icims.com URL: {url}")
    return m.group(1)


def auth_path(tenant: str) -> Path:
    AUTH_DIR.mkdir(exist_ok=True)
    return AUTH_DIR / f"icims-{tenant}.json"


def login(url: str) -> None:
    """You sign in / create the candidate account; only cookies are saved."""
    tenant = tenant_of(url)
    path = auth_path(tenant)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)
        print(
            f"\nSign in to the {tenant} candidate portal in the browser window "
            "(use 'Apply for this job online' or the portal's Login link; "
            "create the account there if needed).\n"
            "When you're signed in, CLOSE the browser window — the session "
            "saves automatically as you go."
        )
        from .applyflow import keep_session_until_closed

        keep_session_until_closed(context, browser, path)
    print(f"session saved to {path} — no password was stored.")


def apply(url: str, resume: str | None = None) -> None:
    profile = load_profile()
    tenant = tenant_of(url)
    state = auth_path(tenant)
    if not state.exists():
        raise SystemExit(
            f"no saved session for {tenant!r}. Run:\n"
            f"  python -m jobagent.icims login https://{tenant}.icims.com"
        )

    report: list[tuple[str, str]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(state))
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        # The portal chrome hosts the real content in a nested iframe.
        frame = page.frame_locator(IFRAME)
        try:
            apply_btn = frame.get_by_role(
                "link", name=re.compile("apply for this job", re.I)
            ).first
            apply_btn.click(timeout=10000)
            report.append(("clicked", "Apply for this job online"))
        except PWTimeout:
            report.append(("not found — click Apply yourself", "apply button"))

        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)  # wizard hydrates slowly
        frame = page.frame_locator(IFRAME)  # re-anchor after navigation

        # Resume upload, if the first step offers it.
        resume_file = resume or profile.get("resume_path", "")
        if resume_file and Path(resume_file).exists():
            try:
                frame.locator("input[type='file']").first.set_input_files(
                    resume_file, timeout=5000
                )
                report.append(("uploaded", f"resume: {Path(resume_file).name}"))
                time.sleep(3)
            except PWTimeout:
                report.append(("no upload field on this step", "resume"))

        for selector, (key, sub) in FIELDS.items():
            value = profile.get(key, {})
            value = (value.get(sub, "") if sub else value) if isinstance(value, dict) else value
            if not value:
                continue
            try:
                el = frame.locator(selector).first
                el.wait_for(state="visible", timeout=2500)
                el.fill(str(value))
                report.append(("filled", selector))
            except PWTimeout:
                report.append(("not on this step", selector))

        print("\n--- fill report (experimental adapter) ---")
        for status, item in report:
            print(f"  {status:<34} {item}")
        print(
            "\niCIMS wizards are multi-step and tenant-specific: walk the "
            "remaining steps yourself, review everything, and submit when "
            "ready. Close the browser window when done.\n"
            "Please note which fields it missed — each one becomes a rule."
        )
        from .applyflow import keep_session_until_closed

        keep_session_until_closed(context, browser, state)


def main() -> None:
    import argparse
    import sys

    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="sign in yourself; save only the session")
    p.add_argument("url")

    p = sub.add_parser("apply", help="best-effort fill; you finish the wizard")
    p.add_argument("url")
    p.add_argument("--resume")

    args = parser.parse_args()
    if args.command == "login":
        login(args.url)
    else:
        apply(args.url, args.resume)


if __name__ == "__main__":
    main()
