"""Security regression tests. Run: python test_security.py

These guard hardening that is easy to delete by accident, because nothing
about the app looks broken when it's gone:

  * the CSRF Origin check on state-changing requests
  * the whitelist that keeps a column name out of an f-string SQL query
  * parameterization of user-supplied filter/search values
  * the status whitelist on writes
  * the dashboard binding to loopback rather than all interfaces

Everything runs against a throwaway database in a temp dir — it never opens
the real jobs.db.
"""

import inspect
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobagent import webapp  # noqa: E402
from jobagent.db import Database  # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ---------- CSRF ----------

@check("cross-origin POST/PATCH/DELETE are rejected with 403")
def _csrf_blocks_writes():
    client = webapp.app.test_client()
    evil = "https://evil.example.com"
    for method, path in [("post", "/api/jobs"),
                         ("patch", "/api/jobs/1"),
                         ("delete", "/api/jobs/1"),
                         ("post", "/api/actions/ingest")]:
        resp = getattr(client, method)(path, headers={"Origin": evil}, json={})
        assert resp.status_code == 403, f"{method.upper()} {path} -> {resp.status_code}, want 403"
        assert "cross-origin" in resp.get_json()["error"]


@check("same-origin writes are not blocked by the CSRF check")
def _csrf_allows_same_origin():
    client = webapp.app.test_client()
    for origin in ("http://127.0.0.1:5151", "http://localhost:5151"):
        # Empty body fails validation with 400 — the point is that it reaches
        # the view at all rather than being turned away at 403.
        resp = client.post("/api/jobs", headers={"Origin": origin}, json={})
        assert resp.status_code == 400, f"{origin} -> {resp.status_code}, want 400 not 403"


@check("read-only GETs are not blocked cross-origin")
def _csrf_ignores_reads():
    client = webapp.app.test_client()
    resp = client.get("/api/status", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code != 403, "GET should not be CSRF-blocked"


# ---------- SQL ----------

@check("_distinct refuses a column name outside the whitelist")
def _sql_column_whitelist():
    with tempfile.TemporaryDirectory() as tmp:
        with Database(Path(tmp) / "t.db") as d:
            for bad in ["url", "description", "company; DROP TABLE jobs--",
                        "1) OR 1=1--", ""]:
                try:
                    d.jobs._distinct(bad)
                except ValueError:
                    continue
                raise AssertionError(f"_distinct({bad!r}) was allowed through")
            # ...and the legitimate ones still work.
            for good in d.jobs._FILTER_COLUMNS:
                d.jobs._distinct(good)


@check("filter and search values are parameterized, not interpolated")
def _sql_values_parameterized():
    with tempfile.TemporaryDirectory() as tmp:
        with Database(Path(tmp) / "t.db") as d:
            d.jobs.upsert({"source": "test", "company": "Acme", "title": "Sales Engineer",
                           "location": "Remote - US", "url": "https://example.com/1",
                           "description": "", "posted_at": None})
            d.commit()
            payload = "' OR 1=1; DROP TABLE jobs--"
            # Each of these lands in a different WHERE clause.
            d.jobs.query(q=payload)
            d.jobs.query(company=payload)
            d.jobs.query(state=payload)
            d.jobs.query(source=payload)
            d.jobs.query(status=payload)
            # The table is still there and the injected OR 1=1 matched nothing.
            assert d.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
            assert d.jobs.query(company=payload) == []


@check("set_status rejects a status outside the allowed set")
def _status_whitelist():
    with tempfile.TemporaryDirectory() as tmp:
        with Database(Path(tmp) / "t.db") as d:
            job_id = d.jobs.upsert({"source": "test", "company": "Acme", "title": "SE",
                                    "location": "Remote - US", "url": "https://example.com/2",
                                    "description": "", "posted_at": None})
            for bad in ["deleted", "'; DROP TABLE jobs--", "APPLIED", ""]:
                try:
                    d.jobs.set_status(job_id, bad)
                except ValueError:
                    continue
                raise AssertionError(f"set_status accepted {bad!r}")


# ---------- exposure ----------

@check("a missing config doesn't leak the filesystem path over HTTP")
def _no_path_in_error():
    real = webapp.cli.load_config
    secret_path = Path("/home/someone/private/job-agent/config.json")
    webapp.cli.load_config = lambda *a, **k: sys.exit(f"missing {secret_path} — copy ...")
    try:
        with webapp.app.app_context():   # jsonify() needs one
            _, err = webapp._safe_load_config()
        assert err is not None, "expected an error tuple"
        body, status = err
        text = body.get_json()["error"]
        assert status == 400, status
        assert str(secret_path) not in text, f"path leaked: {text}"
        assert "someone" not in text, f"path leaked: {text}"
        assert "config.json" in text, "message should still be actionable"
    finally:
        webapp.cli.load_config = real


@check("dashboard binds to loopback, not 0.0.0.0")
def _binds_loopback():
    src = inspect.getsource(webapp.main)
    assert "127.0.0.1" in src, "webapp.main should bind 127.0.0.1"
    assert "0.0.0.0" not in src, "webapp.main must not bind all interfaces"
    assert "debug=True" not in src, "Flask debug server must not be enabled"


def main() -> int:
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}\n       {exc}")
        except Exception as exc:  # noqa: BLE001 - report, don't mask
            failures += 1
            print(f"ERROR {name}\n       {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
