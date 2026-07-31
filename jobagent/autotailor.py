"""Automatic per-job resume tailoring — assembly, not authorship.

For a listing, this module: classifies the JD into a domain (security,
observability, data, ai, devtools), then produces a copy of the base resume
with the CORE COMPETENCIES lines reordered so the most relevant ones lead,
and (only if an approved variant exists in variants.json) the summary's first
sentence swapped. The tailored .docx is stored per-listing, recorded on the
DB row, and the apply adapters attach it automatically.

Hard rule: every word on a generated resume comes from the base resume or a
Michael-approved block in variants.json. This program cannot write new claims.
For genuinely new wording (e.g. the asset-inventory bullet), the path is
Claude drafts -> Michael approves -> it lands in the base or variants file.

Retention: tailored files older than 30 days are purged by the daily digest,
except for listings marked applied — those are the record of what was sent.

Usage:
    python -m jobagent.autotailor generate 533       # one listing
    python -m jobagent.autotailor batch --min-score 14   # all matches
"""

from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from .db import Database

ROOT = Path(__file__).resolve().parent.parent
VARIANTS_PATH = ROOT / "variants.json"
TAILORED_DIR = ROOT / "resume" / "tailored"
RETENTION_DAYS = 30


def load_variants() -> dict:
    return json.loads(VARIANTS_PATH.read_text(encoding="utf-8"))


def classify(jd_text: str, variants: dict) -> tuple[str, int]:
    """Pick the domain whose terms appear most in the JD."""
    low = jd_text.lower()
    best, best_hits = "devtools", 0
    for domain, spec in variants.get("domains", {}).items():
        hits = sum(
            1 for t in spec.get("terms", [])
            if re.search(r"\b" + re.escape(t) + r"\b", low)
        )
        if hits > best_hits:
            best, best_hits = domain, hits
    return best, best_hits


def _fetch_jd(row) -> str:
    jd = row["description"] or ""
    if len(jd) >= 500:
        return jd
    if row["source"].startswith("workday:"):
        import httpx

        from . import sources as src

        m = re.match(
            r"(https://([^.]+)\.wd\d+\.myworkdayjobs\.com)/en-US/([^/]+)(/.*)",
            row["url"],
        )
        if m:
            base, tenant, site, path = m.groups()
            with httpx.Client() as client:
                return src._workday_description(client, base, tenant, site, path) or jd
    return jd


def _reorder_competencies(xml: str, order: list[str], labels: list[str]) -> str:
    """Reorder the competency paragraphs to match `order`.

    Each competency line is a <w:p> whose bold lead run starts with
    "<Label>:". Certifications and anything unlisted keep their position at
    the end of the block.
    """
    blocks: dict[str, str] = {}
    spans: list[tuple[int, int, str]] = []
    for m in re.finditer(r"<w:p\b.*?</w:p>", xml, re.S):
        for label in labels:
            # The document is XML: "&" in a label is stored as "&amp;".
            xml_label = label.replace("&", "&amp;")
            if f"{xml_label}: " in m.group(0) or f"{xml_label}:</w:t>" in m.group(0):
                blocks[label] = m.group(0)
                spans.append((m.start(), m.end(), label))
                break
    if len(spans) < 2:
        return xml  # structure not recognized; leave untouched

    spans.sort()
    ordered = [blocks[l] for l in order if l in blocks]
    ordered += [blocks[l] for _, _, l in spans if l not in order]

    out, cursor = [], 0
    for (start, end, _), replacement in zip(spans, ordered):
        out.append(xml[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(xml[cursor:])
    return "".join(out)


def _swap_summary(xml: str, approved_sentence: str, base_first_sentence: str) -> str:
    if approved_sentence and base_first_sentence in xml:
        return xml.replace(base_first_sentence, approved_sentence, 1)
    return xml


def generate(listing_id: int, force: bool = False) -> Path | None:
    variants = load_variants()
    base = ROOT / variants["base_resume"]
    if not base.exists():
        raise SystemExit(f"base resume not found: {base}")

    with Database() as d:
        row = d.jobs.get(listing_id)
        if not row:
            raise SystemExit(f"no listing with id {listing_id}")

        TAILORED_DIR.mkdir(parents=True, exist_ok=True)
        company = re.sub(r"[^\w-]+", "_", (row["company"] or "unknown")).strip("_")
        out = TAILORED_DIR / f"{listing_id}_{company}.docx"
        if out.exists() and not force:
            print(f"[{listing_id}] already tailored: {out.name} (use --force to redo)")
            return out

        jd = _fetch_jd(row)
        if len(jd) < 200:
            print(f"[{listing_id}] no usable JD — using base resume as-is")
            shutil.copy(base, out)
        else:
            domain, hits = classify(jd, variants)
            spec = variants["domains"][domain]
            # Edit a copy of the base: reorder competencies, maybe swap summary.
            shutil.copy(base, out)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
                contents = {n: zf.read(n) for n in names}
            xml = contents["word/document.xml"].decode("utf-8")
            xml = _reorder_competencies(
                xml, spec.get("competency_order", []), variants["competency_labels"]
            )
            if spec.get("summary"):
                xml = _swap_summary(xml, spec["summary"],
                                    variants.get("base_summary_first_sentence", ""))
            contents["word/document.xml"] = xml.encode("utf-8")
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for n in names:
                    zf.writestr(n, contents[n])
            print(f"[{listing_id}] domain={domain} ({hits} term hits) -> {out.name}")

        d.jobs.set_resume_path(listing_id, str(out))
        d.commit()
    return out


def batch(min_score: int) -> None:
    with Database() as d:
        rows = d.conn.execute(
            "SELECT id FROM jobs WHERE score >= ? AND status IN ('new', 'shortlist')",
            (min_score,),
        ).fetchall()
    print(f"tailoring {len(rows)} listings (score >= {min_score})")
    for r in rows:
        generate(r["id"])


def purge_old() -> int:
    """Delete tailored resumes older than RETENTION_DAYS, keeping applied ones."""
    if not TAILORED_DIR.exists():
        return 0
    with Database() as d:
        applied = {
            r["resume_path"]
            for r in d.conn.execute(
                "SELECT resume_path FROM jobs WHERE status = 'applied' AND resume_path IS NOT NULL"
            )
        }
        cutoff = time.time() - RETENTION_DAYS * 86400
        removed = 0
        for f in TAILORED_DIR.glob("*.docx"):
            if str(f) in applied:
                continue  # record of what was actually sent — keep
            if f.stat().st_mtime < cutoff:
                f.unlink()
                d.conn.execute(
                    "UPDATE jobs SET resume_path = NULL WHERE resume_path = ?", (str(f),)
                )
                removed += 1
        d.commit()
    return removed


def main() -> None:
    import argparse
    import sys

    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="tailor the resume for one listing")
    p.add_argument("id", type=int)
    p.add_argument("--force", action="store_true")

    b = sub.add_parser("batch", help="tailor for every unapplied match")
    b.add_argument("--min-score", type=int, default=14)

    sub.add_parser("purge", help="delete tailored resumes past retention")

    args = parser.parse_args()
    if args.command == "generate":
        generate(args.id, args.force)
    elif args.command == "batch":
        batch(args.min_score)
    else:
        print(f"purged {purge_old()} file(s)")


if __name__ == "__main__":
    main()
