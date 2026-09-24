"""The SQLite ledger: which PRs exist, which sessions own them, and what to tell those sessions.

The hook, the refresh daemon and the picker share this one database and
nothing else. WAL plus a busy timeout lets them overlap.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Bump when the schema changes. Existing databases are carried forward by
# CREATE ... IF NOT EXISTS plus ADDED_COLUMNS, so changes must be additive.
SCHEMA_VERSION = 3

# Columns added after a schema version shipped. CREATE TABLE IF NOT EXISTS is a
# no-op on an existing table, so a new column reaches an existing database only
# through this list.
ADDED_COLUMNS = [
    ("stack_scans", "found", "INTEGER"),  # v2
    ("prs", "body", "TEXT"),  # v3
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
  id                INTEGER PRIMARY KEY,
  repo              TEXT    NOT NULL,
  number            INTEGER NOT NULL,
  url               TEXT    NOT NULL,
  title             TEXT,
  head_ref          TEXT,
  base_ref          TEXT,
  head_sha          TEXT,
  body              TEXT,
  state             TEXT    NOT NULL DEFAULT 'open',
  is_draft          INTEGER NOT NULL DEFAULT 0,
  mergeable         TEXT,
  review_state      TEXT,
  checks_rollup     TEXT,
  checks_pass       INTEGER NOT NULL DEFAULT 0,
  checks_fail       INTEGER NOT NULL DEFAULT 0,
  checks_pending    INTEGER NOT NULL DEFAULT 0,
  checks_skip       INTEGER NOT NULL DEFAULT 0,
  stack_id          TEXT,
  stack_pos         INTEGER,
  local_path        TEXT,
  needs_hydrate     INTEGER NOT NULL DEFAULT 1,
  created_at        INTEGER NOT NULL,
  last_refreshed_at INTEGER,
  terminal_at       INTEGER,
  UNIQUE (repo, number)
);

CREATE TABLE IF NOT EXISTS repos (
  repo           TEXT PRIMARY KEY,
  default_branch TEXT
);

CREATE TABLE IF NOT EXISTS pr_checks (
  pr_id  INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  name   TEXT    NOT NULL,
  state  TEXT    NOT NULL,
  url    TEXT,
  PRIMARY KEY (pr_id, name)
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id        TEXT PRIMARY KEY,
  parent_session_id TEXT,
  agent             TEXT NOT NULL DEFAULT 'claude',
  cwd               TEXT,
  first_seen_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session_prs (
  session_id TEXT    NOT NULL REFERENCES sessions(session_id),
  pr_id      INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  source     TEXT    NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, pr_id)
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  pr_id       INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  session_id  TEXT    NOT NULL,
  kind        TEXT    NOT NULL,
  signature   TEXT    NOT NULL,
  detail      TEXT,
  created_at  INTEGER NOT NULL,
  consumed_at INTEGER,
  UNIQUE (pr_id, session_id, kind, signature)
);

CREATE TABLE IF NOT EXISTS stack_scans (
  id           INTEGER PRIMARY KEY,
  session_id   TEXT    NOT NULL,
  local_path   TEXT    NOT NULL,
  requested_at INTEGER NOT NULL,
  done_at      INTEGER,
  found        INTEGER,
  UNIQUE (session_id, local_path, requested_at)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS events_pending ON events(session_id, consumed_at);
CREATE INDEX IF NOT EXISTS session_prs_pr ON session_prs(pr_id);
"""

EVENTS_HEADING = "Tracked pull request updates:"

PrRef = tuple[str, int, str]


def connect(db: Path, readonly: bool = False, migrate: bool = True) -> sqlite3.Connection:
    """Open the ledger.

    The hook's hot path passes `migrate=False`: it runs on every tool call, and
    a dozen CREATE IF NOT EXISTS statements plus a commit is a write
    transaction contending with the refresh for no reason.
    """
    if readonly and db.exists():
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=3)
    else:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly and migrate:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _apply_added_columns(conn)
        meta_set(conn, "schema_version", SCHEMA_VERSION)
        conn.commit()
    return conn


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def meta_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))


# ── writes ──────────────────────────────────────────────────────────────────


def ensure_session(
    conn: sqlite3.Connection,
    session_id: str,
    cwd: str | None = None,
    agent: str = "claude",
    parent: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, parent_session_id, agent, cwd, first_seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (session_id, parent, agent, cwd, int(time.time())),
    )
    if cwd:
        conn.execute(
            "UPDATE sessions SET cwd = COALESCE(cwd, ?) WHERE session_id = ?", (cwd, session_id)
        )


def upsert_pr(
    conn: sqlite3.Connection, repo: str, number: int, url: str, local_path: str | None = None
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO prs (repo, number, url, local_path, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (repo, number, url, local_path, int(time.time())),
    )
    if local_path:
        conn.execute(
            "UPDATE prs SET local_path = COALESCE(local_path, ?) WHERE repo = ? AND number = ?",
            (local_path, repo, number),
        )
    row = conn.execute(
        "SELECT id FROM prs WHERE repo = ? AND number = ?", (repo, number)
    ).fetchone()
    return row["id"]


def attach(conn: sqlite3.Connection, session_id: str, pr_id: int, source: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO session_prs (session_id, pr_id, source, created_at)"
        " VALUES (?, ?, ?, ?)",
        (session_id, pr_id, source, int(time.time())),
    )


def record(
    db: Path,
    session_id: str,
    cwd: str | None,
    source: str,
    urls: list[PrRef],
    agent: str = "claude",
) -> int:
    """Attach PRs to a session. Returns how many were recorded.

    Offline by design: the row is minimal and flagged `needs_hydrate`, and the
    next refresh fills in the rest. A `gh stack` command also queues a scan of
    its checkout, because the refresh has no working directory of its own to
    run `gh stack view` in.
    """
    if not urls and source != "stack":
        return 0
    conn = connect(db)
    try:
        ensure_session(conn, session_id, cwd, agent)
        for repo, number, url in urls:
            attach(conn, session_id, upsert_pr(conn, repo, number, url, cwd), source)
        if source == "stack" and cwd:
            conn.execute(
                "INSERT OR IGNORE INTO stack_scans (session_id, local_path, requested_at)"
                " VALUES (?, ?, ?)",
                (session_id, cwd, int(time.time())),
            )
        conn.commit()
    finally:
        conn.close()
    return len(urls)


def untrack(db: Path, repo: str, number: int, session_id: str | None) -> bool:
    """Detach a PR from one session, or forget it entirely when `session_id` is None."""
    if not db.exists():
        return False
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT id FROM prs WHERE repo = ? AND number = ?", (repo, number)
        ).fetchone()
        if not row:
            return False
        if session_id is None:
            cur = conn.execute("DELETE FROM prs WHERE id = ?", (row["id"],))
        else:
            cur = conn.execute(
                "DELETE FROM session_prs WHERE pr_id = ? AND session_id = ?",
                (row["id"], session_id),
            )
            conn.execute(
                "DELETE FROM events WHERE pr_id = ? AND session_id = ?", (row["id"], session_id)
            )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


# ── the event queue ─────────────────────────────────────────────────────────


def enqueue(
    conn: sqlite3.Connection, pr_id: int, session_id: str, event: dict[str, str], now: int
) -> None:
    """Queue one event. The UNIQUE constraint drops a re-observation of the same one."""
    conn.execute(
        "INSERT OR IGNORE INTO events (pr_id, session_id, kind, signature, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (pr_id, session_id, event["kind"], event["signature"], event["detail"], now),
    )


def drain(db: Path, session_id: str, peek: bool = False) -> list[str]:
    """Pending event lines for one session, marked consumed unless peeking.

    Called by the hook on every tool call, so the empty case must cost as
    little as possible: no database file means no connection at all.
    """
    if not session_id or not db.exists():
        return []
    conn = connect(db, readonly=True)
    try:
        rows = conn.execute(
            "SELECT e.id, e.detail, p.url FROM events e JOIN prs p ON p.id = e.pr_id"
            " WHERE e.session_id = ? AND e.consumed_at IS NULL ORDER BY e.created_at, e.id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    if not peek:
        writer = connect(db, migrate=False)
        try:
            writer.executemany(
                "UPDATE events SET consumed_at = ? WHERE id = ?",
                [(int(time.time()), r["id"]) for r in rows],
            )
            writer.commit()
        finally:
            writer.close()
    return [f"- {row['detail']} ({row['url']})" for row in rows]


def flush(db: Path, session_id: str | None = None) -> int:
    """Mark pending events consumed without delivering them. Returns how many."""
    if not db.exists():
        return 0
    conn = connect(db, migrate=False)
    try:
        now = int(time.time())
        if session_id:
            cur = conn.execute(
                "UPDATE events SET consumed_at = ? WHERE consumed_at IS NULL AND session_id = ?",
                (now, session_id),
            )
        else:
            cur = conn.execute(
                "UPDATE events SET consumed_at = ? WHERE consumed_at IS NULL", (now,)
            )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ── reads ───────────────────────────────────────────────────────────────────


def rows_for_scope(
    conn: sqlite3.Connection, scope: str, session_id: str | None = None
) -> list[sqlite3.Row]:
    if scope == "session":
        return conn.execute(
            "SELECT p.* FROM prs p JOIN session_prs sp ON sp.pr_id = p.id WHERE sp.session_id = ?",
            (session_id,),
        ).fetchall()
    if scope == "open":
        return conn.execute("SELECT * FROM prs WHERE state = 'open'").fetchall()
    return conn.execute("SELECT * FROM prs").fetchall()


def default_branches(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["repo"]: r["default_branch"]
        for r in conn.execute("SELECT repo, default_branch FROM repos").fetchall()
    }


def build_forest(rows: list[dict], defaults: dict[str, str]) -> list[dict]:
    """Order rows into stack trees, adding `depth`, `is_last` and `orphan` to each.

    A PR sits under another when its base is that PR's head. `gh stack`
    ordering wins when present, since it knows the intended order. A PR whose
    base is neither the repo's default branch nor a tracked head is an orphan:
    it was stacked once and lost its parent.
    """
    by_repo: dict[str, list[dict]] = {}
    for row in rows:
        by_repo.setdefault(row["repo"], []).append(row)

    out: list[dict] = []
    for repo, prs in by_repo.items():
        heads = {p["head_ref"]: p for p in prs if p.get("head_ref")}
        children: dict[int, list[dict]] = {}
        roots = []
        default = defaults.get(repo)

        for pr in prs:
            pr["orphan"] = False
            parent = heads.get(pr.get("base_ref"))
            if parent is not None and parent["id"] != pr["id"]:
                children.setdefault(parent["id"], []).append(pr)
            else:
                pr["orphan"] = bool(pr.get("base_ref") and default and pr["base_ref"] != default)
                roots.append(pr)

        seen: set[int] = set()
        for pr in sorted(roots, key=_sort_key):
            _walk(pr, 0, True, children, seen, out)
        # A cycle (two PRs based on each other's heads) has no root; show it
        # rather than dropping it.
        for pr in sorted(prs, key=_sort_key):
            if pr["id"] not in seen:
                _walk(pr, 0, True, children, seen, out)
    return out


def _sort_key(pr: dict) -> tuple[int, int]:
    if pr.get("stack_pos") is not None:
        return (0, pr["stack_pos"])
    return (1, pr["number"])


def _walk(
    pr: dict,
    depth: int,
    is_last: bool,
    children: dict[int, list[dict]],
    seen: set[int],
    out: list[dict],
) -> None:
    if pr["id"] in seen:
        return
    seen.add(pr["id"])
    pr["depth"] = depth
    pr["is_last"] = is_last
    out.append(pr)
    kids = sorted(children.get(pr["id"], []), key=_sort_key)
    for i, kid in enumerate(kids):
        _walk(kid, depth + 1, i == len(kids) - 1, children, seen, out)


def humanize_age(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
