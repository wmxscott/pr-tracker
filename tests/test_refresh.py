"""The refresh tick against a fake `gh`."""

import fcntl
import json
import os

from conftest import add_pr, check_run, graphql, pr_node, url

from pr_tracker import config, ledger, refresh

CFG = dict(config.DEFAULTS)


def rows(db):
    conn = ledger.connect(db, readonly=True)
    try:
        return {r["number"]: dict(r) for r in conn.execute("SELECT * FROM prs")}
    finally:
        conn.close()


def graphql_calls(fake_gh):
    return [c for c in fake_gh.calls() if c["args"][:2] == ["api", "graphql"]]


def fields(call):
    args = call["args"]
    return dict(a.split("=", 1) for a in args[2:] if "=" in a and not a.startswith("-"))


def record(where, *numbers, repo="o/r", session="s"):
    ledger.record(where.db, session, None, "gh", [(repo, n, url(n, repo)) for n in numbers])


def test_one_query_per_repo(where, fake_gh):
    record(where, 1, 2, 3)
    record(where, 7, repo="o/other")
    fake_gh.set(
        graphql={
            "o/r": graphql(pr_node(1), pr_node(2), pr_node(3)),
            "o/other": graphql(pr_node(7, repo="o/other")),
        }
    )
    report = refresh.tick(where, CFG)
    assert report.ran
    assert report.refreshed == 4
    calls = graphql_calls(fake_gh)
    assert sorted(fields(c)["owner"] + "/" + fields(c)["name"] for c in calls) == [
        "o/other",
        "o/r",
    ]
    query = next(fields(c)["query"] for c in calls if fields(c)["name"] == "r")
    assert all(f"p{n}: pullRequest(number: {n})" in query for n in (1, 2, 3))


def test_large_repos_are_split_into_batches(where, fake_gh, monkeypatch):
    monkeypatch.setattr("pr_tracker.github.BATCH", 2)
    record(where, 1, 2, 3)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1), pr_node(2), pr_node(3))})
    assert refresh.tick(where, CFG).refreshed == 3
    assert len(graphql_calls(fake_gh)) == 2


def test_hydrates_and_is_silent_the_first_time(where, fake_gh):
    record(where, 1)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1, checks=[check_run("lint", "SUCCESS")]))})
    report = refresh.tick(where, CFG)
    row = rows(where.db)[1]
    assert row["title"] == "PR 1"
    assert row["needs_hydrate"] == 0
    assert row["checks_rollup"] == "SUCCESS"
    assert row["last_refreshed_at"]
    assert report.events == 0
    assert ledger.drain(where.db, "s") == []


def test_a_transition_queues_one_event_per_owning_session(where, fake_gh):
    record(where, 1, session="a")
    record(where, 1, session="b")
    fake_gh.set(graphql={"o/r": graphql(pr_node(1, checks=[check_run("lint", None, "QUEUED")]))})
    refresh.tick(where, CFG, force=True)

    red = graphql(pr_node(1, checks=[check_run("lint", "FAILURE")]))
    fake_gh.set(graphql={"o/r": red})
    assert refresh.tick(where, CFG, force=True).events == 2
    # still red across more ticks: nothing new
    assert refresh.tick(where, CFG, force=True).events == 0
    assert refresh.tick(where, CFG, force=True).events == 0

    assert ledger.drain(where.db, "a") == [
        "- checks failing on #1: lint (https://github.com/o/r/pull/1)"
    ]
    assert len(ledger.drain(where.db, "b")) == 1


def test_failure_keeps_stored_values(where, fake_gh):
    record(where, 1)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1, title="first"))})
    refresh.tick(where, CFG, force=True)
    before = rows(where.db)[1]

    fake_gh.set(graphql={"o/r": {"code": 1, "stdout": "", "stderr": "HTTP 401: Bad credentials"}})
    report = refresh.tick(where, CFG, force=True)
    assert report.failed == ["o/r"]
    after = rows(where.db)[1]
    assert after["title"] == "first"
    assert after["last_refreshed_at"] == before["last_refreshed_at"]


def test_failures_are_logged_once_and_recovery_too(where, fake_gh, capsys):
    record(where, 1)
    fake_gh.set(graphql={"o/r": {"code": 1, "stderr": "HTTP 401: Bad credentials"}})
    refresh.tick(where, CFG, force=True)
    refresh.tick(where, CFG, force=True)
    err = capsys.readouterr().err
    assert err.count("o/r: refresh failed: HTTP 401: Bad credentials") == 1

    fake_gh.set(graphql={"o/r": graphql(pr_node(1))})
    refresh.tick(where, CFG, force=True)
    assert "o/r: refreshing again" in capsys.readouterr().err
    conn = ledger.connect(where.db, readonly=True)
    assert ledger.meta_get(conn, "failing:o/r") is None
    conn.close()


def test_without_gh_on_path(where, fake_gh, monkeypatch, capsys):
    record(where, 1)
    monkeypatch.setenv("PATH", "/nonexistent")
    report = refresh.tick(where, CFG, force=True)
    assert report.failed == ["o/r"]
    assert "gh not found on PATH" in capsys.readouterr().err


def test_a_deleted_pr_does_not_block_its_repo(where, fake_gh):
    record(where, 1, 2)
    errors = [
        {
            "type": "NOT_FOUND",
            "path": ["repository", "p2"],
            "message": "Could not resolve to a PullRequest with the number of 2.",
        }
    ]
    body = graphql(pr_node(1), errors=errors)
    body["stdout"]["data"]["repository"]["p2"] = None
    fake_gh.set(graphql={"o/r": body})
    report = refresh.tick(where, CFG, force=True)
    assert report.refreshed == 1
    got = rows(where.db)
    assert got[1]["title"] == "PR 1"
    assert got[2]["state"] == "closed"
    assert got[2]["terminal_at"]
    # and it is no longer asked for
    refresh.tick(where, CFG, force=True)
    assert "p2:" not in fields(graphql_calls(fake_gh)[-1])["query"]


def test_merged_prs_stop_being_polled(where, fake_gh):
    record(where, 1)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1))})
    refresh.tick(where, CFG, force=True)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1, state="MERGED"))})
    assert refresh.tick(where, CFG, force=True).events == 1
    assert rows(where.db)[1]["terminal_at"]
    calls = len(graphql_calls(fake_gh))
    refresh.tick(where, CFG, force=True)
    assert len(graphql_calls(fake_gh)) == calls


def test_no_open_prs_means_no_network(where, fake_gh):
    report = refresh.tick(where, CFG)
    assert report.ran
    assert fake_gh.calls() == []


def test_gating(where, fake_gh):
    record(where, 1)
    fake_gh.set(graphql={"o/r": graphql(pr_node(1))})
    cfg = {**CFG, "jitter_seconds": 0}
    assert refresh.tick(where, cfg, now=1_000_000).ran
    assert not refresh.tick(where, cfg, now=1_000_299).ran
    assert refresh.tick(where, cfg, now=1_000_300).ran
    assert refresh.tick(where, cfg, now=1_000_301, force=True).ran
    assert len(graphql_calls(fake_gh)) == 3


def test_a_held_lock_skips_the_tick(where, fake_gh):
    record(where, 1)
    with open(where.lock, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = refresh.tick(where, CFG, force=True)
    assert report.locked
    assert not report.ran
    assert fake_gh.calls() == []


def test_reaps_even_when_nothing_is_open(where, fake_gh):
    add_pr(where.db, 1, state="merged", needs_hydrate=0, terminal_at=1)
    refresh.tick(where, CFG, force=True)
    assert rows(where.db) == {}


def test_stack_scan_records_the_whole_stack(where, fake_gh, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ledger.record(where.db, "s", str(checkout), "stack", [])
    stack = {
        "branches": [
            {"branch": "a", "pr": {"number": 10, "url": url(10)}},
            {"branch": "b", "pr": {"number": 11, "url": url(11)}},
            {"branch": "c", "pr": {"number": 12, "url": url(12)}},
        ],
        "stack_metadata": {"name": "feature"},
    }
    fake_gh.set(
        stack_view={os.path.realpath(checkout): {"stdout": stack}},
        graphql={"o/r": graphql(pr_node(10), pr_node(11), pr_node(12))},
    )
    refresh.tick(where, CFG, force=True)
    got = rows(where.db)
    assert sorted(got) == [10, 11, 12]
    assert [got[n]["stack_pos"] for n in (10, 11, 12)] == [0, 1, 2]
    assert {got[n]["stack_id"] for n in got} == {"feature"}
    assert ledger.drain(where.db, "s") == []
    conn = ledger.connect(where.db, readonly=True)
    scan = conn.execute("SELECT done_at, found FROM stack_scans").fetchone()
    conn.close()
    assert scan["done_at"]
    assert scan["found"] == 3


def test_stack_scan_outside_a_stack_is_not_an_error(where, fake_gh, tmp_path, capsys):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ledger.record(where.db, "s", str(checkout), "stack", [])
    refresh.tick(where, CFG, force=True)  # the fake gh exits 1, like a branch not in a stack
    conn = ledger.connect(where.db, readonly=True)
    scan = conn.execute("SELECT done_at, found FROM stack_scans").fetchone()
    conn.close()
    assert scan["done_at"]
    assert scan["found"] is None
    assert capsys.readouterr().err == ""


def test_stack_scan_that_finds_nothing_is_logged(where, fake_gh, tmp_path, capsys):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ledger.record(where.db, "s", str(checkout), "stack", [])
    fake_gh.set(stack_view={os.path.realpath(checkout): {"stdout": json.dumps({"new": []})}})
    refresh.tick(where, CFG, force=True)
    assert "output shape may have changed" in capsys.readouterr().err
