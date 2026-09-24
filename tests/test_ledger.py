import sqlite3
import time

from conftest import add_event, add_pr, url

from pr_tracker import ledger, refresh
from pr_tracker.config import DEFAULTS


def count(db, table, where="1"):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    finally:
        conn.close()


def test_record_is_offline_and_minimal(where, fake_gh):
    assert ledger.record(where.db, "s", "/work", "gh", [("o/r", 1, url(1))]) == 1
    conn = ledger.connect(where.db, readonly=True)
    row = dict(conn.execute("SELECT * FROM prs").fetchone())
    session = dict(conn.execute("SELECT * FROM sessions").fetchone())
    link = dict(conn.execute("SELECT * FROM session_prs").fetchone())
    conn.close()
    assert row["needs_hydrate"] == 1
    assert row["title"] is None
    assert row["local_path"] == "/work"
    assert session["cwd"] == "/work"
    assert session["agent"] == "claude"
    assert link["source"] == "gh"
    assert fake_gh.calls() == []


def test_record_nothing_creates_nothing(where):
    assert ledger.record(where.db, "s", "/work", "gh", []) == 0
    assert not where.db.exists()


def test_record_is_idempotent_and_many_to_many(where):
    ledger.record(where.db, "a", None, "gh", [("o/r", 1, url(1))])
    ledger.record(where.db, "a", None, "gh", [("o/r", 1, url(1))])
    ledger.record(where.db, "b", None, "adopt", [("o/r", 1, url(1))])
    assert count(where.db, "prs") == 1
    assert count(where.db, "session_prs") == 2


def test_stack_record_queues_a_scan_even_without_urls(where):
    ledger.record(where.db, "s", "/work", "stack", [])
    assert count(where.db, "stack_scans", "done_at IS NULL") == 1


def test_agent_is_recorded(where):
    ledger.record(where.db, "s", None, "gh", [("o/r", 1, url(1))], agent="codex")
    conn = ledger.connect(where.db, readonly=True)
    assert conn.execute("SELECT agent FROM sessions").fetchone()["agent"] == "codex"
    conn.close()


def test_drain_consumes_once(where):
    pr_id = add_pr(where.db, 1)
    add_event(where.db, pr_id)
    assert len(ledger.drain(where.db, "s", peek=True)) == 1
    assert len(ledger.drain(where.db, "s", peek=True)) == 1  # peek is idempotent
    lines = ledger.drain(where.db, "s")
    assert lines == ["- checks failing: lint (https://github.com/o/r/pull/1)"]
    assert ledger.drain(where.db, "s") == []


def test_drain_is_per_session(where):
    pr_id = add_pr(where.db, 1, session="a")
    add_event(where.db, pr_id, session="a")
    assert ledger.drain(where.db, "b") == []
    assert len(ledger.drain(where.db, "a")) == 1


def test_drain_does_not_create_a_database(where):
    assert ledger.drain(where.db, "s") == []
    assert ledger.drain(where.db, "") == []
    assert not where.db.exists()


def test_flush(where):
    a = add_pr(where.db, 1, session="a")
    b = add_pr(where.db, 2, session="b")
    add_event(where.db, a, session="a")
    add_event(where.db, b, session="b")
    assert ledger.flush(where.db, "a") == 1
    assert ledger.drain(where.db, "b", peek=True)
    assert ledger.flush(where.db) == 1
    assert ledger.drain(where.db, "b") == []


def test_flush_without_a_database(where):
    assert ledger.flush(where.db) == 0
    assert not where.db.exists()


def test_untrack_one_session(where):
    pr_id = add_pr(where.db, 1, session="a")
    ledger.record(where.db, "b", None, "gh", [("o/r", 1, url(1))])
    add_event(where.db, pr_id, session="a")
    assert ledger.untrack(where.db, "o/r", 1, "a")
    assert count(where.db, "prs") == 1
    assert count(where.db, "session_prs") == 1
    assert count(where.db, "events") == 0
    assert not ledger.untrack(where.db, "o/r", 1, "a")


def test_untrack_everywhere(where):
    add_pr(where.db, 1)
    assert ledger.untrack(where.db, "o/r", 1, None)
    assert count(where.db, "prs") == 0
    assert count(where.db, "session_prs") == 0
    assert not ledger.untrack(where.db, "o/r", 99, None)


def test_reap_drops_expired_and_keeps_recent(where):
    now = int(time.time())
    old = add_pr(where.db, 1, state="merged", terminal_at=now - 31 * 86400)
    recent = add_pr(where.db, 2, state="merged", terminal_at=now - 86400)
    live = add_pr(where.db, 3)
    conn = ledger.connect(where.db)
    conn.execute("INSERT INTO pr_checks (pr_id, name, state) VALUES (?, 'lint', 'FAILURE')", (old,))
    conn.commit()
    add_event(where.db, old)
    assert refresh._reap(conn, DEFAULTS, now) == 1
    conn.commit()
    left = {r["id"] for r in conn.execute("SELECT id FROM prs")}
    conn.close()
    assert left == {recent, live}
    # links, checks and events went with it rather than dangling
    assert count(where.db, "session_prs", f"pr_id = {old}") == 0
    assert count(where.db, "pr_checks") == 0
    assert count(where.db, "events") == 0


def test_added_columns_reach_an_existing_database(where):
    conn = ledger.connect(where.db)
    conn.execute("ALTER TABLE stack_scans DROP COLUMN found")
    conn.commit()
    conn.close()
    conn = ledger.connect(where.db)  # CREATE IF NOT EXISTS alone would not fix this
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stack_scans)")}
    assert "found" in cols
    assert ledger.meta_get(conn, "schema_version") == str(ledger.SCHEMA_VERSION)
    conn.close()


def test_opens_a_database_written_by_the_original_script(where):
    # The schema the original script shipped at version 1, before `found` and `body`.
    where.state.mkdir(parents=True)
    conn = sqlite3.connect(where.db)
    old_schema = ledger.SCHEMA.replace("  body              TEXT,\n", "").replace(
        "  found        INTEGER,\n", ""
    )
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO prs (repo, number, url, created_at) VALUES ('o/r', 1, ?, 1)", (url(1),)
    )
    conn.commit()
    conn.close()

    conn = ledger.connect(where.db)
    assert "body" in {r["name"] for r in conn.execute("PRAGMA table_info(prs)")}
    assert conn.execute("SELECT url FROM prs").fetchone()["url"] == url(1)
    conn.close()


def test_readonly_connect_handles_awkward_paths(tmp_path):
    db = tmp_path / "dir with ? and #" / "prs.db"
    ledger.connect(db).close()
    conn = ledger.connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) c FROM prs").fetchone()["c"] == 0
    conn.close()
