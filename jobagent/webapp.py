"""Local read/track UI for job-agent. Run: python -m jobagent.webapp

Serves a single-page dashboard over jobs.db: browse/filter/search matches,
change status inline, view full listing detail, browse/log contacts, and run
every CLI action (ingest/rescore/digest/schedule/tailor/find-hm/apply) from
the UI. Pipeline actions shell out to the real CLI via subprocess so there's
one code path for both interfaces.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, request

from . import cli, matcher
from . import locations as geo
from . import salary as sal
from .db import ROOT, STATUSES, Database

app = Flask(__name__, static_folder="static", template_folder="templates")

PY = sys.executable

_ALLOWED_ORIGINS = {"http://127.0.0.1:5151", "http://localhost:5151"}


@app.before_request
def _block_cross_origin_writes():
    """CSRF guard: reject state-changing requests whose Origin isn't this
    dashboard. This server binds to 127.0.0.1 only, but that doesn't stop a
    page open in another tab from firing a cross-origin POST/DELETE at it —
    some of these endpoints are consequential (delete, launching a real
    browser with real profile data for Apply), so it's worth the check even
    though GET requests (read-only) are left unrestricted."""
    if request.method in ("POST", "PATCH", "DELETE", "PUT"):
        origin = request.headers.get("Origin")
        if origin is not None and origin not in _ALLOWED_ORIGINS:
            return jsonify({"error": "cross-origin request blocked"}), 403


def _safe_load_config() -> tuple[dict | None, tuple | None]:
    """cli.load_config() calls sys.exit() when config.json is missing — fine
    for a CLI, but SystemExit inside a Flask request thread just kills that
    request silently instead of returning a real error. Convert it here.
    Returns (config, None) on success or (None, (json_response, status)) to
    return directly from the caller."""
    try:
        return cli.load_config(), None
    except SystemExit as e:
        return None, (jsonify({"error": str(e)}), 400)


def _run_cli(*args: str, timeout: int = 120) -> dict:
    """Run `python -m jobagent <args>` and capture the result. Blocking —
    only for commands that finish in seconds (ingest/rescore/digest/schedule).
    Never raises; failures come back as ok=False with returncode/stderr."""
    try:
        proc = subprocess.run(
            [PY, "-m", "jobagent", *args],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "returncode": None,
                "stdout": e.stdout or "", "stderr": f"timed out after {timeout}s"}


def row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def job_to_dict(row) -> dict:
    d = row_to_dict(row)
    d["score_pct"] = matcher.to_percent(row["score"])
    d["remote_label"] = geo.remote_label(row["city"], row["state"], row["country"])
    d["salary_label"] = sal.format_salary(row["salary_min"], row["salary_max"])
    return d


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/status")
def api_status():
    with Database() as d:
        counts = d.jobs.counts()
        n_contacts = d.conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
    return jsonify({
        "counts": {s: counts.get(s, 0) for s in STATUSES},
        "total": sum(counts.values()),
        "contacts": n_contacts,
        "statuses": list(STATUSES),
    })


@app.get("/api/jobs")
def api_jobs_list():
    has_hm = request.args.get("has_hiring_manager")
    with Database() as d:
        rows = d.jobs.query(
            min_score=request.args.get("min_score", type=int),
            status=request.args.get("status") or None,
            q=request.args.get("q") or None,
            company=request.args.get("company") or None,
            state=request.args.get("state") or None,
            source=request.args.get("source") or None,
            has_hiring_manager={"true": True, "false": False}.get(has_hm),
            limit=request.args.get("limit", default=300, type=int),
        )
    return jsonify([job_to_dict(r) for r in rows])


@app.get("/api/jobs/filter-options")
def api_jobs_filter_options():
    with Database() as d:
        opts = d.jobs.filter_options()
    return jsonify(opts)


@app.post("/api/jobs")
def api_job_add():
    body = request.get_json(silent=True) or {}
    for required in ("title", "url"):
        if not body.get(required):
            return jsonify({"error": f"{required} is required"}), 400
    job = {
        "source": "manual",
        "company": body.get("company") or None,
        "title": body["title"],
        "location": body.get("location") or None,
        "url": body["url"],
        "description": body.get("description") or None,
        "posted_at": None,
    }
    config, err = _safe_load_config()
    if err:
        return err
    with Database() as d:
        job_id = d.jobs.upsert(job)
        if job_id is None:
            return jsonify({"error": "a listing with this URL already exists"}), 409
        value, reasons = matcher.score(job, config)
        d.jobs.set_score(job_id, value, reasons)
        d.commit()
        row = d.jobs.get(job_id)
    return jsonify(job_to_dict(row)), 201


@app.get("/api/jobs/<int:job_id>")
def api_job_detail(job_id: int):
    with Database() as d:
        row = d.jobs.get(job_id)
    if not row:
        return jsonify({"error": f"no listing with id {job_id}"}), 404
    return jsonify(job_to_dict(row))


@app.delete("/api/jobs/<int:job_id>")
def api_job_delete(job_id: int):
    with Database() as d:
        ok = d.jobs.delete(job_id)
        d.commit()
    if not ok:
        return jsonify({"error": f"no listing with id {job_id}"}), 404
    return jsonify({"id": job_id, "deleted": True})


@app.patch("/api/jobs/<int:job_id>")
def api_job_update(job_id: int):
    body = request.get_json(silent=True) or {}
    with Database() as d:
        row = d.jobs.get(job_id)
        if not row:
            return jsonify({"error": f"no listing with id {job_id}"}), 404
        if "hiring_manager" in body:
            d.jobs.set_hiring_manager(job_id, body["hiring_manager"] or None)
            d.commit()
        row = d.jobs.get(job_id)
    return jsonify(job_to_dict(row))


@app.get("/api/jobs/<int:job_id>/find-hm")
def api_job_findhm(job_id: int):
    with Database() as d:
        row = d.jobs.get(job_id)
    if not row:
        return jsonify({"error": f"no listing with id {job_id}"}), 404
    config, err = _safe_load_config()
    if err:
        return err
    pkg = cli._build_hm_search(job_id, row["title"], row["company"] or "",
                               row["description"] or "", config)
    return jsonify(pkg)


@app.post("/api/jobs/<int:job_id>/tailor")
def api_job_tailor(job_id: int):
    from . import resume as rz

    with Database() as d:
        row = d.jobs.get(job_id)
    if not row:
        return jsonify({"error": f"no listing with id {job_id}"}), 404

    profile_path = ROOT / "profile.json"
    if not profile_path.exists():
        return jsonify({"error": f"missing {profile_path} — copy profile.example.json "
                        "to profile.json and fill in your own info"}), 400
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    resume_path = profile.get("resume_path", "")
    if not resume_path or not Path(resume_path).exists():
        return jsonify({"error": f"resume not found: {resume_path!r} — "
                        "set resume_path in profile.json"}), 400

    jd = row["description"] or ""
    if len(jd) < 200:
        return jsonify({"error": "no usable job description for this listing"}), 400

    config, err = _safe_load_config()
    if err:
        return err
    vocab = config.get("resume_analysis", {}).get("vocab")
    resume_text = rz.read_docx(resume_path)
    result = rz.analyze(resume_text, jd, vocab=set(vocab) if vocab else None)
    return jsonify({"id": job_id, "title": row["title"], "company": row["company"],
                    **result})


@app.post("/api/jobs/<int:job_id>/apply")
def api_job_apply(job_id: int):
    with Database() as d:
        row = d.jobs.get(job_id)
    if not row:
        return jsonify({"error": f"no listing with id {job_id}"}), 404
    # Fire-and-forget: opens its own browser window for the human to review
    # and submit. Never blocks the web request — this can run for minutes.
    subprocess.Popen([PY, "-m", "jobagent", "apply", str(job_id)], cwd=str(ROOT))
    return jsonify({"id": job_id, "launched": True,
                    "message": "Pre-fill launched in a separate browser window — "
                               "review and submit there."})


@app.post("/api/jobs/<int:job_id>/status")
def api_job_set_status(job_id: int):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in STATUSES:
        return jsonify({"error": f"status must be one of {STATUSES}"}), 400
    with Database() as d:
        if not d.jobs.get(job_id):
            return jsonify({"error": f"no listing with id {job_id}"}), 404
        d.jobs.set_status(job_id, status)
        d.commit()
    return jsonify({"id": job_id, "status": status})


@app.get("/api/contacts")
def api_contacts_list():
    with Database() as d:
        rows = d.contacts.list(
            company=request.args.get("company") or None,
            channel=request.args.get("channel") or None,
        )
    return jsonify([row_to_dict(r) for r in rows])


@app.get("/api/contacts/filter-options")
def api_contacts_filter_options():
    with Database() as d:
        channels = d.contacts.channel_options()
    return jsonify({"channel": channels})


@app.delete("/api/contacts/<int:contact_id>")
def api_contact_delete(contact_id: int):
    with Database() as d:
        ok = d.contacts.delete(contact_id)
        d.commit()
    if not ok:
        return jsonify({"error": f"no contact with id {contact_id}"}), 404
    return jsonify({"id": contact_id, "deleted": True})


@app.get("/api/contacts/<int:contact_id>")
def api_contact_detail(contact_id: int):
    with Database() as d:
        row = d.contacts.get(contact_id)
    if not row:
        return jsonify({"error": f"no contact with id {contact_id}"}), 404
    return jsonify(row_to_dict(row))


@app.post("/api/contacts")
def api_contact_add():
    body = request.get_json(silent=True) or {}
    for required in ("company", "name"):
        if not body.get(required):
            return jsonify({"error": f"{required} is required"}), 400
    with Database() as d:
        cid = d.contacts.add(
            company=body["company"],
            name=body["name"],
            title=body.get("title") or None,
            channel=body.get("channel") or None,
            contacted_at=body.get("contacted_at") or None,
            outcome=body.get("outcome") or None,
            follow_up=body.get("follow_up") or None,
            listing_id=body.get("listing_id") or None,
        )
        d.commit()
        row = d.contacts.get(cid)
    return jsonify(row_to_dict(row)), 201


@app.patch("/api/contacts/<int:contact_id>")
def api_contact_update(contact_id: int):
    body = request.get_json(silent=True) or {}
    with Database() as d:
        if not d.contacts.get(contact_id):
            return jsonify({"error": f"no contact with id {contact_id}"}), 404
        fields = {k: body[k] for k in
                  ("outcome", "follow_up", "title", "channel", "name", "company")
                  if body.get(k) is not None}
        if not fields:
            return jsonify({"error": "no updatable fields provided"}), 400
        d.contacts.update(contact_id, **fields)
        d.commit()
        row = d.contacts.get(contact_id)
    return jsonify(row_to_dict(row))


@app.post("/api/actions/ingest")
def api_action_ingest():
    return jsonify(_run_cli("ingest", timeout=120))


@app.post("/api/actions/rescore")
def api_action_rescore():
    return jsonify(_run_cli("rescore", timeout=60))


@app.post("/api/actions/digest")
def api_action_digest():
    return jsonify(_run_cli("digest", timeout=180))


@app.get("/api/actions/schedule")
def api_action_schedule_status():
    return jsonify(_run_cli("schedule", "status", timeout=30))


@app.post("/api/actions/schedule")
def api_action_schedule_set():
    body = request.get_json(silent=True) or {}
    state = body.get("state")
    if state not in ("on", "off"):
        return jsonify({"error": "state must be 'on' or 'off'"}), 400
    return jsonify(_run_cli("schedule", state, timeout=30))


@app.get("/api/export.csv")
def api_export_csv():
    with Database() as d:
        rows = d.jobs.query(
            min_score=request.args.get("min_score", type=int),
            status=request.args.get("status") or None,
            limit=10_000,
        )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "score", "title", "company", "city", "state",
                     "country", "salary_min", "salary_max", "status", "hiring_manager", "url"])
    for r in rows:
        writer.writerow([r["id"], r["score"], r["title"], r["company"], r["city"],
                         r["state"], r["country"], r["salary_min"], r["salary_max"],
                         r["status"], r["hiring_manager"], r["url"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=jobagent_export.csv"
    })


def main() -> None:
    app.run(host="127.0.0.1", port=5151, debug=False, threaded=True)


if __name__ == "__main__":
    main()
