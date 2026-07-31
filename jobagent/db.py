"""SQLite storage for scraped listings."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import locations as geo

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "jobs.db"

STATUSES = ("new", "shortlist", "applied", "ignored", "rejected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    company     TEXT,
    title       TEXT NOT NULL,
    location    TEXT,
    url         TEXT NOT NULL UNIQUE,
    description TEXT,
    posted_at   TEXT,
    first_seen  TEXT NOT NULL,
    score       INTEGER NOT NULL DEFAULT 0,
    reasons     TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    applied_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY,
    company      TEXT NOT NULL,
    name         TEXT NOT NULL,
    title        TEXT,
    channel      TEXT,
    contacted_at TEXT NOT NULL,
    outcome      TEXT,
    follow_up    TEXT,
    listing_id   INTEGER REFERENCES jobs(id),
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migration: per-listing tailored resume (added 2026-07-26).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "resume_path" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN resume_path TEXT")
        conn.commit()
    # Migration: hiring manager name, set after `find-hm` research (2026-07-30).
    if "hiring_manager" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN hiring_manager TEXT")
        conn.commit()
    # Migration: structured location, parsed from the free-text field (2026-07-30).
    if "city" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN city TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN state TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN country TEXT")
        conn.commit()
        backfill_locations(conn)
    return conn


def backfill_locations(conn: sqlite3.Connection) -> int:
    """(Re-)parse city/state/country from the raw `location` text for every row.

    Safe to re-run: overwrites city/state/country from the current parser,
    it never touches the raw `location` field itself.
    """
    rows = conn.execute("SELECT id, location FROM jobs").fetchall()
    for row in rows:
        city, state, country = geo.parse_us_location(row["location"])
        conn.execute(
            "UPDATE jobs SET city = ?, state = ?, country = ? WHERE id = ?",
            (city, state, country, row["id"]),
        )
    conn.commit()
    return len(rows)


def upsert(conn: sqlite3.Connection, job: dict) -> int | None:
    """Insert a listing. Returns its new id, or None if the url was already seen.

    Existing rows keep their status so a re-ingest never resurrects something
    already applied to or ignored.
    """
    city, state, country = geo.parse_us_location(job.get("location"))
    cur = conn.execute(
        """
        INSERT INTO jobs (source, company, title, location, url,
                          description, posted_at, first_seen, city, state, country)
        VALUES (:source, :company, :title, :location, :url,
                :description, :posted_at, :first_seen, :city, :state, :country)
        ON CONFLICT(url) DO NOTHING
        """,
        {**job, "first_seen": now(), "city": city, "state": state, "country": country},
    )
    return cur.lastrowid if cur.rowcount > 0 else None


def set_score(conn: sqlite3.Connection, job_id: int, score: int, reasons: str) -> None:
    conn.execute(
        "UPDATE jobs SET score = ?, reasons = ? WHERE id = ?", (score, reasons, job_id)
    )


def set_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    applied = now() if status == "applied" else None
    conn.execute(
        "UPDATE jobs SET status = ?, applied_at = COALESCE(?, applied_at) WHERE id = ?",
        (status, applied, job_id),
    )


def set_hiring_manager(conn: sqlite3.Connection, job_id: int, name: str | None) -> None:
    conn.execute("UPDATE jobs SET hiring_manager = ? WHERE id = ?", (name, job_id))


def query(
    conn: sqlite3.Connection,
    *,
    min_score: int | None = None,
    status: str | None = None,
    q: str | None = None,
    company: str | None = None,
    state: str | None = None,
    source: str | None = None,
    has_hiring_manager: bool | None = None,
    limit: int = 25,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM jobs WHERE 1=1"
    params: list = []
    if min_score is not None:
        sql += " AND score >= ?"
        params.append(min_score)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ?)"
        like = f"%{q}%"
        params += [like, like]
    if company:
        sql += " AND company = ?"
        params.append(company)
    if state:
        sql += " AND state = ?"
        params.append(state)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if has_hiring_manager is True:
        sql += " AND hiring_manager IS NOT NULL AND hiring_manager != ''"
    elif has_hiring_manager is False:
        sql += " AND (hiring_manager IS NULL OR hiring_manager = '')"
    sql += " ORDER BY score DESC, first_seen DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def delete_job(conn: sqlite3.Connection, job_id: int) -> bool:
    cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return cur.rowcount > 0


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
    return {r["status"]: r["c"] for r in rows}


def filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Distinct values for each filterable column, for populating dropdowns."""
    def distinct(col: str) -> list[str]:
        rows = conn.execute(
            f"SELECT DISTINCT {col} FROM jobs WHERE {col} IS NOT NULL AND {col} != '' "
            f"ORDER BY {col} COLLATE NOCASE"
        ).fetchall()
        return [r[0] for r in rows]

    return {
        "company": distinct("company"),
        "state": distinct("state"),
        "source": distinct("source"),
    }


def add_contact(
    conn: sqlite3.Connection,
    *,
    company: str,
    name: str,
    title: str | None = None,
    channel: str | None = None,
    contacted_at: str | None = None,
    outcome: str | None = None,
    follow_up: str | None = None,
    listing_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO contacts (company, name, title, channel, contacted_at,
                              outcome, follow_up, listing_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (company, name, title, channel, contacted_at or now()[:10],
         outcome, follow_up, listing_id, now()),
    )
    return cur.lastrowid


def list_contacts(
    conn: sqlite3.Connection, *, company: str | None = None, channel: str | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM contacts WHERE 1=1"
    params: list = []
    if company:
        sql += " AND company LIKE ?"
        params.append(f"%{company}%")
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY contacted_at DESC"
    return conn.execute(sql, params).fetchall()


def get_contact(conn: sqlite3.Connection, contact_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> bool:
    cur = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    return cur.rowcount > 0


def contact_channel_options(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT channel FROM contacts WHERE channel IS NOT NULL AND channel != '' "
        "ORDER BY channel COLLATE NOCASE"
    ).fetchall()
    return [r[0] for r in rows]


def update_contact(conn: sqlite3.Connection, contact_id: int, **fields) -> None:
    allowed = {"outcome", "follow_up", "title", "channel", "name", "company"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    params.append(contact_id)
    conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?", params)
