"""End to end through `main`, against a temp HOME and a fake `gh`."""

import io
import json
import os
import subprocess
import sys

import pytest
from conftest import add_event, add_pr, graphql, pr_node, url

from pr_tracker import __version__, cli, ledger


def run(*args, env=None):
    return cli.main(list(args), env=dict(os.environ) if env is None else env)


def test_version(capsys):
    with pytest.raises(SystemExit) as exit:
        run("--version")
    assert exit.value.code == 0
    assert capsys.readouterr().out.strip() == f"pr-tracker {__version__}"


def test_no_command_prints_help(capsys):
    assert run() == 2
    out = capsys.readouterr().out
    assert "usage: pr-tracker" in out
    for command in ("status", "refresh", "list", "adopt", "hook", "pick"):
        assert command in out


def test_console_scripts_are_installed():
    for script in ("pr-tracker", "prs"):
        result = subprocess.run(
            [os.path.join(os.path.dirname(sys.executable), script), "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == f"{script} {__version__}"


def test_status_before_anything_is_tracked(where, capsys):
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "settings.toml (none; using defaults)" in out
    assert "none tracked yet" in out
    assert not where.db.exists()


def test_status(where, capsys):
    add_event(where.db, add_pr(where.db, 1))
    add_pr(where.db, 2, state="merged")
    conn = ledger.connect(where.db)
    ledger.meta_set(conn, "failing:o/r", "HTTP 401")
    conn.commit()
    conn.close()
    where.config.parent.mkdir(parents=True)
    where.config.write_text("[refresh]\ninterval_seconds = 0\n")
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "2 (1 open)" in out
    assert "1 pending" in out
    assert "last tick   never" in out
    assert "failing     o/r: HTTP 401" in out
    assert "warning" in out and "interval_seconds" in out


def test_init(where, capsys):
    assert run("init") == 0
    assert where.db.exists()
    assert capsys.readouterr().out.strip() == str(where.db)


def test_list(where, capsys):
    assert run("list") == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["prs"] == []
    assert empty["last_tick_at"] is None
    assert empty["config"]["interval_seconds"] == 300

    add_pr(where.db, 1, session="a", head_ref="x", base_ref="main")
    add_pr(where.db, 2, session="a", head_ref="y", base_ref="x")
    add_pr(where.db, 3, session="b", state="merged")
    assert run("list", "--scope", "all") == 0
    prs = json.loads(capsys.readouterr().out)["prs"]
    assert [(p["number"], p["depth"]) for p in prs] == [(1, 0), (2, 1), (3, 0)]
    assert run("list", "--scope", "session", "--session", "b") == 0
    assert [p["number"] for p in json.loads(capsys.readouterr().out)["prs"]] == [3]
    assert run("list") == 0
    assert [p["number"] for p in json.loads(capsys.readouterr().out)["prs"]] == [1, 2]


def test_list_session_scope_needs_a_session(capsys):
    assert run("list", "--scope", "session") == 1
    assert "--scope session needs" in capsys.readouterr().err


def test_session_comes_from_the_environment(where, capsys):
    add_pr(where.db, 1, session="from-env")
    env = {**os.environ, "PR_TRACKER_SESSION_ID": "from-env"}
    assert run("list", "--scope", "session", env=env) == 0
    assert len(json.loads(capsys.readouterr().out)["prs"]) == 1


def test_adopt_by_url(where, fake_gh, capsys):
    assert run("adopt", url(4), "--session", "s") == 0
    assert capsys.readouterr().out.strip() == "tracking o/r#4"
    assert fake_gh.calls() == []
    conn = ledger.connect(where.db, readonly=True)
    assert conn.execute("SELECT source FROM session_prs").fetchone()["source"] == "adopt"
    conn.close()


def test_adopt_current_branch(where, fake_gh, tmp_path, capsys):
    fake_gh.set(pr_view={"stdout": {"url": url(8)}})
    assert run("adopt", "--session", "s", "--cwd", str(tmp_path)) == 0
    assert "tracking o/r#8" in capsys.readouterr().out
    assert fake_gh.calls()[0]["args"] == ["pr", "view", "--json", "url"]

    assert run("adopt", "8", "--session", "s", "--cwd", str(tmp_path)) == 0
    assert fake_gh.calls()[1]["args"] == ["pr", "view", "8", "--json", "url"]


def test_adopt_failures(where, fake_gh, capsys):
    assert run("adopt", url(1)) == 1
    assert "no session" in capsys.readouterr().err
    assert run("adopt", "not-a-pr", "--session", "s") == 1
    assert "not a PR URL or number" in capsys.readouterr().err
    fake_gh.set(pr_view={"code": 1, "stderr": "no pull requests found for branch"})
    assert run("adopt", "--session", "s") == 1
    assert "no pull requests found" in capsys.readouterr().err
    assert not where.db.exists()


def test_record(where, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"made {url(1)} and {url(2)}"))
    assert run("record", "--session", "s", "--text", "-", "--url", url(3)) == 0
    assert capsys.readouterr().out.count("tracking") == 3
    assert run("record", "--session", "s", "--text", "no urls here") == 1


def test_drain_and_flush(where, capsys):
    pr_id = add_pr(where.db, 1)
    add_event(where.db, pr_id, signature="a")
    add_event(where.db, pr_id, signature="b")
    assert run("drain", "--session", "s", "--peek") == 0
    assert capsys.readouterr().out.count("\n") == 2
    assert run("flush") == 0
    assert capsys.readouterr().out.strip() == "flushed 2 pending event(s)"
    assert run("drain", "--session", "s") == 0
    assert capsys.readouterr().out == ""


def test_untrack(where, capsys):
    add_pr(where.db, 1, session="a")
    assert run("untrack", "o/r#1", "--session", "a") == 0
    assert "untracked o/r#1" in capsys.readouterr().out
    assert run("untrack", url(1), "--all-sessions") == 0
    assert run("untrack", url(1), "--all-sessions") == 0
    assert "was not tracked" in capsys.readouterr().out
    assert run("untrack", "junk", "--all-sessions") == 1
    assert run("untrack", url(1)) == 1


def test_refresh(where, fake_gh, capsys):
    add_pr(where.db, 1)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1))})
    assert run("refresh") == 0
    assert run("refresh") == 0  # not due yet
    assert len(fake_gh.calls()) == 1
    assert run("refresh", "--force") == 0
    assert len(fake_gh.calls()) == 2
    assert capsys.readouterr().out == ""  # quiet when not on a terminal


def test_refresh_through_the_console_script(where, fake_gh):
    """The exact command the service runs."""
    add_pr(where.db, 1)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1, title="from the service"))})
    script = os.path.join(os.path.dirname(sys.executable), "pr-tracker")
    result = subprocess.run([script, "refresh"], capture_output=True, text=True, timeout=60)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    conn = ledger.connect(where.db, readonly=True)
    assert conn.execute("SELECT title FROM prs").fetchone()["title"] == "from the service"
    conn.close()


def test_pick_print(where, capsys):
    add_pr(where.db, 1, title="shown", needs_hydrate=0)
    assert run("pick", "--print") == 0
    assert "shown" in capsys.readouterr().out
