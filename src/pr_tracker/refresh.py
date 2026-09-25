"""The refresh tick: gate, hydrate, diff, enqueue, reap.

A scheduler (a Homebrew service, launchd or cron) runs `pr-tracker refresh`
every minute or so. The tick owns the real schedule: it exits at once unless
`interval_seconds`, give or take `jitter_seconds`, has passed since the last
one, and it makes no network call when nothing is open.
"""

from __future__ import annotations

import fcntl
import os
import random
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pr_tracker import github, ledger
from pr_tracker.paths import Paths
from pr_tracker.scrape import parse_pr_urls


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", file=sys.stderr, flush=True)


def is_tick_due(
    last_tick: int | None,
    now: int,
    interval: float,
    jitter: float,
    rand: Callable[[float, float], float] = random.uniform,
) -> bool:
    if not last_tick:
        return True
    return (now - last_tick) >= interval + rand(-jitter, jitter)


def compute_events(
    old: dict | None,
    new: dict,
    failing: list[str],
    old_failing: list[str] | None = None,
) -> list[dict[str, str]]:
    """Transitions worth telling the owning session about.

    Only transitions: a PR that was already green when we first saw it is not
    news, and neither is one that is still green. Every event also carries a
    signature, and the UNIQUE constraint on (pr_id, session_id, kind,
    signature) drops re-observations. A PR that stays red across twenty ticks
    yields one event; it yields a second only when the set of failing checks
    changes.
    """
    events = []
    sha = new.get("head_sha") or ""
    was = (old or {}).get("checks_rollup")

    if new["state"] in ("merged", "closed") and (not old or old["state"] == "open"):
        events.append(
            {
                "kind": new["state"],
                "signature": new["state"],
                "detail": f"PR #{new['number']} was {new['state']}",
            }
        )

    if new.get("checks_rollup") == "FAILURE":
        names = ",".join(sorted(failing))
        if was != "FAILURE" or set(failing) != set(old_failing or []):
            events.append(
                {
                    "kind": "checks_failed",
                    "signature": names,
                    "detail": f"checks failing on #{new['number']}: {names or 'unknown'}",
                }
            )
    elif new.get("checks_rollup") == "SUCCESS" and was != "SUCCESS":
        events.append(
            {
                "kind": "checks_passed",
                "signature": sha,
                "detail": f"all checks passing on #{new['number']}",
            }
        )

    review = new.get("review_state")
    if review in ("approved", "changes_requested") and review != (old or {}).get("review_state"):
        events.append(
            {
                "kind": "review_changed",
                "signature": f"{review}:{sha}",
                "detail": f"#{new['number']} review: {review.replace('_', ' ')}",
            }
        )
    return events


@dataclass
class TickReport:
    ran: bool = False
    locked: bool = False
    refreshed: int = 0
    repos: int = 0
    failed: list[str] = field(default_factory=list)
    events: int = 0
    reaped: int = 0


def tick(
    paths: Paths, cfg: dict[str, Any], force: bool = False, now: int | None = None
) -> TickReport:
    report = TickReport()
    paths.state.mkdir(parents=True, exist_ok=True)
    with open(paths.lock, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report.locked = True
            return report
        conn = ledger.connect(paths.db)
        try:
            now = int(time.time()) if now is None else now
            last = ledger.meta_get(conn, "last_tick_at")
            if not force and not is_tick_due(
                int(last) if last else None, now, cfg["interval_seconds"], cfg["jitter_seconds"]
            ):
                return report
            report.ran = True
            _apply_stack_scans(conn, now)
            rows = conn.execute(
                "SELECT * FROM prs WHERE state = 'open' OR needs_hydrate = 1"
            ).fetchall()
            by_repo: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_repo.setdefault(row["repo"], []).append(row)
            for repo, prs in by_repo.items():
                _refresh_repo(conn, repo, prs, now, report)
                # Never hold the write lock across the next repo's network call:
                # every hook delivering events needs it too.
                conn.commit()
            report.reaped = _reap(conn, cfg, now)
            ledger.meta_set(conn, "last_tick_at", now)
            conn.commit()
        finally:
            conn.close()
    return report


def _refresh_repo(
    conn: sqlite3.Connection, repo: str, prs: list[sqlite3.Row], now: int, report: TickReport
) -> None:
    report.repos += 1
    data, error = github.fetch_repo(repo, [p["number"] for p in prs])
    key = f"failing:{repo}"
    if data is not None and data.repo_missing:
        # GitHub also says NOT_FOUND after losing access, so a PR it has
        # returned before is kept, and the repo counts as failing.
        for stored in prs:
            if stored["needs_hydrate"]:
                _forget(conn, repo, stored)
        if not any(p["needs_hydrate"] == 0 for p in prs):
            return
        data, error = None, f"Could not resolve to a Repository with the name '{repo}'."
    if data is None:
        # Keep the stored values and their last_refreshed_at, so the picker
        # can show them as stale. Log only the change, not every tick.
        report.failed.append(repo)
        if ledger.meta_get(conn, key) != error:
            log(f"{repo}: refresh failed: {error}")
            ledger.meta_set(conn, key, error)
        return
    if ledger.meta_get(conn, key) is not None:
        log(f"{repo}: refreshing again")
        ledger.meta_delete(conn, key)
    if data.default_branch:
        conn.execute(
            "INSERT INTO repos (repo, default_branch) VALUES (?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET default_branch = excluded.default_branch",
            (repo, data.default_branch),
        )
    for stored in prs:
        node = data.nodes.get(stored["number"])
        if node:
            fresh, checks = github.row_from_node(node)
            report.events += _write_refresh(conn, stored, fresh, checks, now)
            report.refreshed += 1
        elif stored["number"] in data.missing and stored["needs_hydrate"]:
            _forget(conn, repo, stored)
        elif stored["number"] in data.missing:
            # Deleted, or moved somewhere we cannot see. Retire it quietly so
            # it stops being asked for and the TTL reaps it.
            log(f"{repo}#{stored['number']}: not found; no longer tracking")
            conn.execute(
                "UPDATE prs SET state = 'closed', needs_hydrate = 0,"
                " terminal_at = COALESCE(terminal_at, ?) WHERE id = ?",
                (now, stored["id"]),
            )


def _forget(conn: sqlite3.Connection, repo: str, stored: sqlite3.Row) -> None:
    """Drop a PR GitHub has never returned: a URL the hook scraped that was never a real PR."""
    log(f"{repo}#{stored['number']}: not on GitHub; forgotten")
    conn.execute("DELETE FROM prs WHERE id = ?", (stored["id"],))


def _write_refresh(
    conn: sqlite3.Connection,
    stored: sqlite3.Row,
    fresh: dict,
    checks: list[github.Check],
    now: int,
) -> int:
    terminal_at = stored["terminal_at"]
    if fresh["state"] != "open" and not terminal_at:
        terminal_at = now
    if fresh["state"] == "open":
        terminal_at = None
    conn.execute(
        "UPDATE prs SET title = ?, body = ?, head_ref = ?, base_ref = ?, head_sha = ?,"
        " state = ?, is_draft = ?, mergeable = ?, review_state = ?, checks_rollup = ?,"
        " checks_pass = ?, checks_fail = ?, checks_pending = ?, checks_skip = ?,"
        " needs_hydrate = 0, last_refreshed_at = ?, terminal_at = ?,"
        " url = COALESCE(?, url) WHERE id = ?",
        (
            fresh["title"],
            fresh["body"],
            fresh["head_ref"],
            fresh["base_ref"],
            fresh["head_sha"],
            fresh["state"],
            fresh["is_draft"],
            fresh["mergeable"],
            fresh["review_state"],
            fresh["checks_rollup"],
            fresh["checks_pass"],
            fresh["checks_fail"],
            fresh["checks_pending"],
            fresh["checks_skip"],
            now,
            terminal_at,
            fresh["url"],
            stored["id"],
        ),
    )
    old_failing = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM pr_checks WHERE pr_id = ? AND state = 'FAILURE'", (stored["id"],)
        ).fetchall()
    ]
    conn.execute("DELETE FROM pr_checks WHERE pr_id = ?", (stored["id"],))
    conn.executemany(
        "INSERT OR REPLACE INTO pr_checks (pr_id, name, state, url) VALUES (?, ?, ?, ?)",
        [(stored["id"], name, state, url) for name, state, url in checks],
    )

    if stored["needs_hydrate"]:
        # First sight of this PR. There is no transition to report yet, and
        # announcing "all checks passing" for a PR the agent opened moments
        # ago is noise. Record the baseline; notify on what changes from it.
        return 0

    failing = [name for name, state, _ in checks if state == "FAILURE"]
    sessions = conn.execute(
        "SELECT session_id FROM session_prs WHERE pr_id = ?", (stored["id"],)
    ).fetchall()
    count = 0
    for event in compute_events(dict(stored), fresh, failing, old_failing):
        for session in sessions:
            ledger.enqueue(conn, stored["id"], session["session_id"], event, now)
            count += 1
    return count


def _apply_stack_scans(conn: sqlite3.Connection, now: int) -> None:
    """Run `gh stack view --json` in each checkout a `gh stack` command ran in.

    `gh stack view` exits non-zero when the branch is not part of a stack,
    which is the ordinary case and not an error. A scan that succeeds and
    still finds nothing is different: it means the JSON shape moved, so it
    is logged rather than swallowed.
    """
    pending = conn.execute(
        "SELECT id, session_id, local_path FROM stack_scans WHERE done_at IS NULL"
    ).fetchall()
    for scan in pending:
        path = scan["local_path"]
        found = None
        if path and os.path.isdir(path):
            result = github.gh(["stack", "view", "--json"], cwd=path, timeout=30)
            if result.code == 0:
                found = _ingest_stack(conn, scan["session_id"], path, result.out)
                if found == 0:
                    log(
                        f"`gh stack view --json` succeeded in {path} but no PRs were"
                        " recognised; its output shape may have changed"
                    )
        conn.execute(
            "UPDATE stack_scans SET done_at = ?, found = ? WHERE id = ?",
            (now, found, scan["id"]),
        )
        conn.commit()


def _ingest_stack(conn: sqlite3.Connection, session_id: str, path: str, payload: str) -> int:
    parsed = github.stack_entries(payload)
    if parsed is None:
        return 0
    stack_id, urls = parsed
    stack_id = stack_id or path
    found = 0
    for pos, url in enumerate(urls):
        matches = parse_pr_urls(url)
        if not matches:
            continue
        repo, number, clean = matches[0]
        pr_id = ledger.upsert_pr(conn, repo, number, clean, path)
        ledger.attach(conn, session_id, pr_id, "stack")
        conn.execute(
            "UPDATE prs SET stack_id = ?, stack_pos = ? WHERE id = ?", (stack_id, pos, pr_id)
        )
        found += 1
    return found


def _reap(conn: sqlite3.Connection, cfg: dict[str, Any], now: int) -> int:
    cutoff = now - int(cfg["terminal_ttl_days"]) * 86400
    cur = conn.execute(
        "DELETE FROM prs WHERE terminal_at IS NOT NULL AND terminal_at < ?", (cutoff,)
    )
    conn.execute("DELETE FROM session_prs WHERE pr_id NOT IN (SELECT id FROM prs)")
    conn.execute("DELETE FROM stack_scans WHERE done_at IS NOT NULL AND done_at < ?", (cutoff,))
    return cur.rowcount or 0
