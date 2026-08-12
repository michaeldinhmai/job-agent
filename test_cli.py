"""CLI tests. Run: python test_cli.py

cli.py is the biggest module (668 lines) and the one a person actually drives.
Everything here runs against a throwaway database and a fixture config — it
never opens the real jobs.db or config.json.

The most load-bearing assertion in this file is the LinkedIn one: the project
rule is that the X-ray search may be executed but LinkedIn Jobs/Posts queries
are handed to a human, never run. That rule lives in data (`*_executable`
flags), which makes it exactly the kind of thing a refactor can flip by
accident.
"""

import contextlib
import csv
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobagent import cli  # noqa: E402
from jobagent.db import Database  # noqa: E402

CHECKS = []

FIXTURE_CONFIG = {
    "titles": {"include": ["sales engineer"], "exclude": []},
    "keywords": {"boost": {}, "block": []},
    "companies": {"exclude": []},
    "locations": {"united_states_only": True, "unknown_ok": True},
    "min_score": 10,
}


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@contextlib.contextmanager
def sandbox(seed=()):
    """Point cli at a temp DB + fixture config, optionally pre-seeded."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.db"
        with Database(path) as d:
            for job in seed:
                jid = d.jobs.upsert(job)
                d.jobs.set_score(jid, job.get("_score", 15), "seeded")
            d.commit()
        real_db, real_cfg = cli.Database, cli.load_config
        cli.Database = lambda *a, **k: Database(path)
        cli.load_config = lambda *a, **k: FIXTURE_CONFIG
        try:
            yield Path(tmp)
        finally:
            cli.Database, cli.load_config = real_db, real_cfg


def job(**over):
    base = {"source": "greenhouse:acme", "company": "Acme", "title": "Sales Engineer",
            "location": "Remote - US", "url": "https://example.com/1",
            "description": "", "posted_at": None}
    base.update(over)
    return base


def run(argv):
    """Invoke the CLI the way a person does; return captured stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(argv)
    return buf.getvalue()


def expect_exit(fn, needle=None):
    try:
        fn()
    except SystemExit as e:
        if needle:
            assert needle in str(e), f"exit message {str(e)!r} lacks {needle!r}"
        return
    raise AssertionError("expected SystemExit, none raised")


# ---------- config ----------

@check("load_config exits helpfully when config.json is absent")
def _load_config_missing():
    with tempfile.TemporaryDirectory() as tmp:
        expect_exit(lambda: cli.load_config(Path(tmp) / "nope.json"),
                    "config.example.json")


@check("load_config parses a real file")
def _load_config_ok():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps({"min_score": 7}), encoding="utf-8")
        assert cli.load_config(p)["min_score"] == 7


# ---------- role families / hiring-manager search ----------

@check("_role_family classifies known titles and falls back otherwise")
def _role_family():
    name, kws = cli._role_family("Senior Sales Engineer")
    assert name != "Unclassified", "a Sales Engineer should classify"
    assert kws, "a classified family must carry search keywords"
    name, kws = cli._role_family("Underwater Basket Weaver")
    assert name == "Unclassified", name
    assert kws == cli.DEFAULT_FALLBACK_SEARCH_KEYWORDS


@check("_role_family honours a config override instead of the packaged default")
def _role_family_override():
    cfg = {"role_families": {
        "families": [{"name": "Clinical", "terms": ["nurse"],
                      "search_keywords": ["Nursing"]}],
        "fallback_search_keywords": ["Other"]}}
    assert cli._role_family("Registered Nurse", cfg) == ("Clinical", ["Nursing"])
    assert cli._role_family("Sales Engineer", cfg) == ("Unclassified", ["Other"])


@check("hiring-manager search keeps LinkedIn queries non-executable")
def _hm_linkedin_rule():
    pkg = cli._build_hm_search(1, "Sales Engineer", "Acme", "")
    assert pkg["x_ray_executable"] is True, "the sanctioned X-ray search should run"
    assert pkg["linkedin_jobs_executable"] is False, \
        "LinkedIn Jobs search must stay query-only — see README's no-automation rule"
    assert pkg["linkedin_posts_executable"] is False, \
        "LinkedIn Posts search must stay query-only"
    assert "linkedin.com/in" in pkg["x_ray_query"]
    assert "Acme" in pkg["x_ray_query"] and "Acme" in pkg["linkedin_jobs_query"]


@check("hiring-manager search surfaces reporting lines, capped at three")
def _hm_report_lines():
    desc = ("You will report to the Director of Sales Engineering. "
            "This role reports to the VP of Customer Success. "
            "Reporting to the Head of Solutions. "
            "Also reports to the Manager of Nothing.")
    pkg = cli._build_hm_search(1, "Sales Engineer", "Acme", desc)
    assert len(pkg["report_line_hits"]) <= 3, pkg["report_line_hits"]
    assert pkg["report_line_hits"], "should have found at least one reporting line"


# ---------- adapter routing ----------

@check("_adapter_name_for routes a db id via its source prefix")
def _route_by_id():
    with sandbox(seed=[job()]) as _:
        with cli.Database() as d:
            assert cli._adapter_name_for("1", d.jobs) == "greenhouse"


@check("_adapter_name_for refuses an id whose ATS has no queueable adapter")
def _route_unqueueable():
    with sandbox(seed=[job(source="workday:acme")]):
        with cli.Database() as d:
            expect_exit(lambda: cli._adapter_name_for("1", d.jobs), "no queueable")


@check("_adapter_name_for reports a missing id rather than crashing")
def _route_missing_id():
    with sandbox():
        with cli.Database() as d:
            expect_exit(lambda: cli._adapter_name_for("999", d.jobs), "no listing")


@check("_adapter_name_for routes a URL by domain")
def _route_by_url():
    assert cli._adapter_name_for("https://boards.greenhouse.io/x/jobs/1", None) == "greenhouse"
    assert cli._adapter_name_for("https://jobs.ashbyhq.com/x/abc", None) == "ashby"


@check("login-gated ATSes are refused with the login instruction")
def _route_login_gated():
    for url, name in [("https://acme.wd5.myworkdayjobs.com/x", "workday"),
                      ("https://careers-acme.icims.com/jobs/1", "icims")]:
        expect_exit(lambda u=url: cli._adapter_name_for(u, None), "login session")
        # ...and it should name which adapter to reach for.
        expect_exit(lambda u=url: cli._adapter_name_for(u, None), name)


@check("an unroutable target is rejected")
def _route_unknown():
    expect_exit(lambda: cli._adapter_name_for("https://example.com/job/1", None),
                "can't route")


# ---------- parser ----------

@check("every documented subcommand parses and binds a handler")
def _parser():
    p = cli.build_parser()
    argvs = [
        ["ingest"], ["ingest", "--source", "greenhouse"], ["rescore"], ["digest"],
        ["list"], ["list", "--min-score", "5", "--limit", "3"],
        ["show", "1"], ["mark", "1", "applied"], ["delete", "1", "--yes"],
        ["open", "1"], ["set-hm", "1", "Jane Doe"], ["apply", "1"],
        ["queue", "1", "2"], ["tailor", "1"], ["find-hm", "1"],
        ["export", "--out", "x.csv"], ["schedule", "status"],
        ["contact", "list"], ["contact", "show", "1"],
        ["contact", "delete", "1", "--yes"],
    ]
    for argv in argvs:
        args = p.parse_args(argv)
        assert hasattr(args, "func"), f"{argv} bound no handler"
        assert callable(args.func), f"{argv} handler not callable"


@check("an unknown subcommand is rejected rather than silently ignored")
def _parser_rejects_junk():
    p = cli.build_parser()
    with contextlib.redirect_stderr(io.StringIO()):
        expect_exit(lambda: p.parse_args(["frobnicate"]))


# ---------- commands end to end ----------

@check("list prints seeded matches and says so when there are none")
def _cmd_list():
    with sandbox(seed=[job(title="Sales Engineer", url="https://example.com/a")]):
        out = run(["list"])
        assert "Sales Engineer" in out, out
        assert "Acme" in out, out
    with sandbox():
        assert "nothing matched" in run(["list"])


@check("mark writes the status through to the database")
def _cmd_mark():
    with sandbox(seed=[job()]):
        out = run(["mark", "1", "applied"])
        assert "applied" in out
        with cli.Database() as d:
            row = d.jobs.get(1)
            assert row["status"] == "applied", row["status"]
            assert row["applied_at"], "applied_at should be stamped"


@check("mark refuses a status outside the allowed set")
def _cmd_mark_bad():
    with sandbox(seed=[job()]):
        with contextlib.redirect_stderr(io.StringIO()):
            expect_exit(lambda: cli.main(["mark", "1", "banana"]))


@check("show renders the detail view for a real id and exits for a bad one")
def _cmd_show():
    with sandbox(seed=[job(title="Sales Engineer")]):
        out = run(["show", "1"])
        for field in ("title", "company", "salary", "match"):
            assert field in out, f"{field!r} missing from show output"
        expect_exit(lambda: cli.main(["show", "999"]), "no listing")


@check("delete honours a declined confirmation prompt")
def _cmd_delete_cancel():
    with sandbox(seed=[job()]):
        real_input = __builtins__["input"] if isinstance(__builtins__, dict) \
            else __builtins__.input
        import builtins
        builtins.input = lambda *a, **k: "n"
        try:
            out = run(["delete", "1"])
        finally:
            builtins.input = real_input
        assert "cancelled" in out, out
        with cli.Database() as d:
            assert d.jobs.get(1) is not None, "row deleted despite declining"


@check("delete --yes removes the row without prompting")
def _cmd_delete_yes():
    with sandbox(seed=[job()]):
        run(["delete", "1", "--yes"])
        with cli.Database() as d:
            assert d.jobs.get(1) is None, "row survived an explicit delete"


@check("export writes a CSV with a header and one row per match")
def _cmd_export():
    with sandbox(seed=[job(url="https://example.com/a"),
                       job(url="https://example.com/b", title="Sales Engineer II")]) as tmp:
        out_path = tmp / "out.csv"
        msg = run(["export", "--out", str(out_path)])
        assert "wrote" in msg
        rows = list(csv.reader(out_path.open(encoding="utf-8")))
        assert rows[0][:3] == ["id", "score", "title"], rows[0]
        assert len(rows) == 3, f"header + 2 rows expected, got {len(rows)}"


def main() -> int:
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}\n       {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}\n       {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
