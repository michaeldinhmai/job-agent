"""SQLite storage for scraped listings.

`Database` owns the connection and schema; `.jobs` and `.contacts` are
repository objects that group the operations for each table. Callers get one
`Database` per unit of work:

    with Database() as d:
        d.jobs.set_status(634, "applied")
        d.commit()
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import locations as geo
from . import salary as sal

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


class JobRepository:
    """CRUD and queries over the `jobs` table."""

    # Whitelist, not user input: SQLite can't parameterize column names, so
    # any dynamic-column query must check against a fixed set like this one
    # rather than interpolating a caller-supplied string directly.
    _FILTER_COLUMNS = ("company", "state", "source")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, job: dict) -> int | None:
        """Insert a listing. Returns its new id, or None if the url was already seen.

        Existing rows keep their status so a re-ingest never resurrects
        something already applied to or ignored.
        """
        city, state, country = geo.parse_us_location(job.get("location"))
        salary_min, salary_max = sal.parse_salary(job.get("description"))
        cur = self.conn.execute(
            """
            INSERT INTO jobs (source, company, title, location, url,
                              description, posted_at, first_seen, city, state, country,
                              salary_min, salary_max)
            VALUES (:source, :company, :title, :location, :url,
                    :description, :posted_at, :first_seen, :city, :state, :country,
                    :salary_min, :salary_max)
            ON CONFLICT(url) DO NOTHING
            """,
            {**job, "first_seen": now(), "city": city, "state": state, "country": country,
             "salary_min": salary_min, "salary_max": salary_max},
        )
        return cur.lastrowid if cur.rowcount > 0 else None

    def backfill_locations(self) -> int:
        """(Re-)parse city/state/country from the raw `location` text for every row.

        Safe to re-run: overwrites city/state/country from the current
        parser, never touches the raw `location` field itself.
        """
        rows = self.conn.execute("SELECT id, location FROM jobs").fetchall()
        for row in rows:
            city, state, country = geo.parse_us_location(row["location"])
            self.conn.execute(
                "UPDATE jobs SET city = ?, state = ?, country = ? WHERE id = ?",
                (city, state, country, row["id"]),
            )
        self.conn.commit()
        return len(rows)

    def backfill_salaries(self) -> int:
        """(Re-)parse salary_min/salary_max from the description for every row.

        Safe to re-run: overwrites salary_min/salary_max from the current
        parser, never touches the raw `description` field itself.
        """
        rows = self.conn.execute("SELECT id, description FROM jobs").fetchall()
        for row in rows:
            salary_min, salary_max = sal.parse_salary(row["description"])
            self.conn.execute(
                "UPDATE jobs SET salary_min = ?, salary_max = ? WHERE id = ?",
                (salary_min, salary_max, row["id"]),
            )
        self.conn.commit()
        return len(rows)

    def set_score(self, job_id: int, score: int, reasons: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET score = ?, reasons = ? WHERE id = ?", (score, reasons, job_id)
        )

    def set_status(self, job_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        applied = now() if status == "applied" else None
        self.conn.execute(
            "UPDATE jobs SET status = ?, applied_at = COALESCE(?, applied_at) WHERE id = ?",
            (status, applied, job_id),
        )

    def set_hiring_manager(self, job_id: int, name: str | None) -> None:
        self.conn.execute("UPDATE jobs SET hiring_manager = ? WHERE id = ?", (name, job_id))

    def set_resume_path(self, job_id: int, path: str | None) -> None:
        self.conn.execute("UPDATE jobs SET resume_path = ? WHERE id = ?", (path, job_id))

    def query(
        self,
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
        return self.conn.execute(sql, params).fetchall()

    def get(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def delete(self, job_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def filter_options(self) -> dict[str, list[str]]:
        """Distinct values for each filterable column, for populating dropdowns."""
        return {col: self._distinct(col) for col in self._FILTER_COLUMNS}

    def _distinct(self, col: str) -> list[str]:
        if col not in self._FILTER_COLUMNS:
            raise ValueError(f"{col!r} is not a whitelisted filter column")
        rows = self.conn.execute(
            f"SELECT DISTINCT {col} FROM jobs WHERE {col} IS NOT NULL AND {col} != '' "
            f"ORDER BY {col} COLLATE NOCASE"
        ).fetchall()
        return [r[0] for r in rows]


class ContactRepository:
    """CRUD and queries over the `contacts` table."""

    _UPDATABLE_FIELDS = ("outcome", "follow_up", "title", "channel", "name", "company")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(
        self,
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
        cur = self.conn.execute(
            """
            INSERT INTO contacts (company, name, title, channel, contacted_at,
                                  outcome, follow_up, listing_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (company, name, title, channel, contacted_at or now()[:10],
             outcome, follow_up, listing_id, now()),
        )
        return cur.lastrowid

    def list(
        self, *, company: str | None = None, channel: str | None = None
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
        return self.conn.execute(sql, params).fetchall()

    def get(self, contact_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()

    def update(self, contact_id: int, **fields) -> None:
        sets, params = [], []
        for k, v in fields.items():
            if k in self._UPDATABLE_FIELDS and v is not None:
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return
        params.append(contact_id)
        self.conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?", params)

    def delete(self, contact_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return cur.rowcount > 0

    def channel_options(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT channel FROM contacts WHERE channel IS NOT NULL AND channel != '' "
            "ORDER BY channel COLLATE NOCASE"
        ).fetchall()
        return [r[0] for r in rows]


class Database:
    """Owns the connection and schema migrations; exposes `.jobs` / `.contacts`."""

    def __init__(self, path: Path = DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        self.jobs = JobRepository(self.conn)
        self.contacts = ContactRepository(self.conn)

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        # Migration: per-listing tailored resume (added 2026-07-26).
        if "resume_path" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN resume_path TEXT")
            self.conn.commit()
        # Migration: hiring manager name, set after `find-hm` research (2026-07-30).
        if "hiring_manager" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN hiring_manager TEXT")
            self.conn.commit()
        # Migration: structured location, parsed from the free-text field (2026-07-30).
        if "city" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN city TEXT")
            self.conn.execute("ALTER TABLE jobs ADD COLUMN state TEXT")
            self.conn.execute("ALTER TABLE jobs ADD COLUMN country TEXT")
            self.conn.commit()
            JobRepository(self.conn).backfill_locations()
        # Migration: salary range, parsed from the JD text (2026-07-31).
        if "salary_min" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN salary_min INTEGER")
            self.conn.execute("ALTER TABLE jobs ADD COLUMN salary_max INTEGER")
            self.conn.commit()
            JobRepository(self.conn).backfill_salaries()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
