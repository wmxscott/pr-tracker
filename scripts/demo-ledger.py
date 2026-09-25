"""Seed a throwaway ledger with made-up PRs, for the README screenshot.

    python scripts/demo-ledger.py STATE_DIR [--now EPOCH]

Writes STATE_DIR/prs.db through pr-tracker's own ledger module, as if a few
agent sessions had opened these PRs and a refresh had filled them in. Every
repo, branch, title and URL is invented. Nothing here touches the network.

Every timestamp is an offset from one `now`. The picker measures staleness
against the real clock, so seed right before rendering and leave `--now` at
its default; pass it only to make the database itself byte-for-byte stable.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pr_tracker import ledger

SESSION = "demo-session"
OTHER_SESSION = "other-session"
REPOS = ("acme/api", "acme/widgets", "acme/docs-site")

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

PY = ("ci / lint", "ci / typecheck", "ci / test (3.12)", "ci / test (3.13)", "ci / migrations")
WEB = ("ci / lint", "ci / unit", "e2e / chromium", "e2e / webkit", "build / storybook")
DOCS = ("docs / build", "docs / links", "docs / spelling")


def checks(names, fail=(), pending=(), skip=()):
    def state(name):
        if name in fail:
            return "FAILURE"
        if name in pending:
            return "PENDING"
        if name in skip:
            return "SKIPPED"
        return "SUCCESS"

    return [(name, state(name)) for name in names]


# Each PR as a refresh would have left it. `events` are (kind, detail,
# delivered): what the owning session has been told, and what is still queued.
PRS = [
    # A three-PR stack: the root is green and approved, the middle is red.
    {
        "repo": "acme/api",
        "number": 412,
        "title": "Add the Invoice model and migration",
        "head_ref": "billing/invoice-model",
        "base_ref": "main",
        "review_state": "approved",
        "checks": checks((*PY, "security / codeql"), skip=("security / codeql",)),
        "events": [
            ("checks_passed", "all checks passing on #412", True),
            ("review_changed", "#412 review: approved", True),
        ],
        "body": (
            "First of three PRs for invoicing.\n\n"
            "Adds the `Invoice` and `InvoiceLine` models, the migration that "
            "creates both tables, and a factory for the tests that follow.\n\n"
            "- Amounts are stored in minor units, never floats\n"
            "- Currency is a three-letter code, checked at the model layer"
        ),
    },
    {
        "repo": "acme/api",
        "number": 413,
        "title": "Expose invoices over the REST API",
        "head_ref": "billing/invoice-api",
        "base_ref": "billing/invoice-model",
        "review_state": "review_required",
        "checks": checks(PY, fail=("ci / test (3.13)", "ci / typecheck")),
        "events": [
            ("checks_passed", "all checks passing on #413", True),
            (
                "checks_failed",
                "checks failing on #413: ci / test (3.13),ci / typecheck",
                False,
            ),
        ],
        "body": (
            "Second of three. Stacked on #412.\n\n"
            "Adds `GET /invoices`, `GET /invoices/{id}` and `POST /invoices`, "
            "with cursor pagination on the list endpoint.\n\n"
            "Test plan: new endpoint tests, plus a contract test against the "
            "OpenAPI schema."
        ),
    },
    {
        "repo": "acme/api",
        "number": 414,
        "title": "Send a webhook when an invoice is paid",
        "head_ref": "billing/invoice-webhooks",
        "base_ref": "billing/invoice-api",
        "is_draft": 1,
        "review_state": "none",
        "checks": checks(PY, pending=("ci / test (3.12)", "ci / test (3.13)", "ci / migrations")),
        "body": (
            "Third of three. Stacked on #413. Draft until the retry policy is agreed.\n\n"
            "Queues an `invoice.paid` webhook per subscribed endpoint, signed "
            "with the endpoint's secret and retried with backoff."
        ),
    },
    # A standalone PR that has picked up a merge conflict.
    {
        "repo": "acme/api",
        "number": 420,
        "title": "Bump sqlalchemy from 2.0.35 to 2.0.36",
        "head_ref": "deps/sqlalchemy-2.0.36",
        "base_ref": "main",
        "mergeable": "CONFLICTING",
        "review_state": "approved",
        "checks": checks(PY),
        "events": [
            ("checks_passed", "all checks passing on #420", True),
            ("review_changed", "#420 review: approved", True),
        ],
        "body": "Patch release. No breaking changes in the changelog.",
    },
    # A two-PR stack: both green, but the top one has changes requested.
    {
        "repo": "acme/widgets",
        "number": 88,
        "title": "Extract colours into theme tokens",
        "head_ref": "theme/tokens",
        "base_ref": "main",
        "review_state": "approved",
        "checks": checks(WEB),
        "events": [("review_changed", "#88 review: approved", True)],
        "body": (
            "Moves every hard-coded colour into `theme/tokens.ts`, so a second "
            "palette is one file rather than forty.\n\n"
            "No visual change: the Storybook snapshots are identical."
        ),
    },
    {
        "repo": "acme/widgets",
        "number": 89,
        "title": "Add a dark mode toggle to settings",
        "head_ref": "theme/dark-mode",
        "base_ref": "theme/tokens",
        "review_state": "changes_requested",
        "checks": checks(WEB),
        "events": [
            ("checks_passed", "all checks passing on #89", True),
            ("review_changed", "#89 review: changes requested", False),
        ],
        "body": (
            "Stacked on #88.\n\n"
            "Adds a light / dark / system switch to Settings, stored per user "
            "and applied before first paint to avoid a flash."
        ),
    },
    # Standalone, checks still running.
    {
        "repo": "acme/widgets",
        "number": 91,
        "title": "Fix a flaky date-picker test",
        "head_ref": "fix/date-picker-leap-day",
        "base_ref": "main",
        "review_state": "review_required",
        "checks": checks(WEB, pending=("e2e / chromium", "e2e / webkit")),
        "body": (
            "The test built 'one year from today' by adding 365 days, which "
            "fails on 29 February. It now uses the calendar helper."
        ),
    },
    # An orphan: its base branch is neither `main` nor any tracked PR's head.
    {
        "repo": "acme/widgets",
        "number": 86,
        "title": "Migrate the chart legend to theme tokens",
        "head_ref": "theme/chart-legend",
        "base_ref": "theme/tokens-v1",
        "review_state": "review_required",
        "checks": checks(WEB, skip=("build / storybook",)),
        "body": (
            "Was stacked on an earlier cut of the tokens branch, which has "
            "since been deleted. Needs retargeting onto `theme/tokens`."
        ),
    },
    # Its repo's refreshes have been failing for a while, so it shows `stale`.
    {
        "repo": "acme/docs-site",
        "number": 57,
        "title": "Document the invoices API",
        "head_ref": "docs/invoices-api",
        "base_ref": "main",
        "review_state": "review_required",
        "checks": checks(DOCS),
        "refreshed_ago": 2 * HOUR + 10 * MINUTE,
        "body": (
            "Reference pages for the three invoice endpoints, generated from "
            "the OpenAPI schema, plus a short guide to pagination."
        ),
    },
    # Terminal PRs: shown under the `a` filter's merged and all scopes.
    {
        "repo": "acme/api",
        "number": 409,
        "title": "Rate-limit the login endpoint",
        "head_ref": "auth/login-rate-limit",
        "base_ref": "main",
        "state": "merged",
        "review_state": "approved",
        "checks": checks(PY),
        "closed_ago": 3 * DAY,
        "events": [("merged", "PR #409 was merged", True)],
        "body": "Ten attempts per minute per account, and per IP address.",
    },
    {
        "repo": "acme/widgets",
        "number": 84,
        "title": "Try a canvas renderer for sparklines",
        "head_ref": "spike/canvas-sparklines",
        "base_ref": "main",
        "state": "closed",
        "review_state": "none",
        "checks": checks(WEB, fail=("e2e / webkit",)),
        "closed_ago": 6 * DAY,
        "events": [("closed", "PR #84 was closed", True)],
        "body": "Spike. SVG turned out fast enough; closing.",
    },
    # Another session's work, shown when `s` widens to every tracked PR.
    {
        "repo": "acme/api",
        "number": 421,
        "title": "Log queries slower than 250 ms",
        "head_ref": "obs/slow-query-log",
        "base_ref": "main",
        "session": OTHER_SESSION,
        "review_state": "review_required",
        "checks": checks(PY, fail=("ci / lint",)),
        "events": [("checks_failed", "checks failing on #421: ci / lint", False)],
        "body": "Adds a slow-query log with the statement, duration and caller.",
    },
    {
        "repo": "acme/widgets",
        "number": 92,
        "title": "Add shortcuts to the command palette",
        "head_ref": "feat/palette-shortcuts",
        "base_ref": "main",
        "session": OTHER_SESSION,
        "review_state": "approved",
        "checks": checks(WEB),
        "body": "Cmd-K opens the palette; arrows move; Enter runs.",
    },
]


def rollup(pr_checks):
    states = [state for _, state in pr_checks]
    if not states:
        return "NONE"
    if "FAILURE" in states:
        return "FAILURE"
    if "PENDING" in states:
        return "PENDING"
    return "SUCCESS"


def seed(state_dir: Path, now: int | None = None) -> Path:
    """Write the demo ledger into `state_dir` and return the database path."""
    now = int(time.time()) if now is None else now
    db = Path(state_dir) / "prs.db"
    if db.exists():
        raise SystemExit(f"{db} already exists; seed into an empty directory")
    conn = ledger.connect(db)
    try:
        conn.executemany(
            "INSERT INTO repos (repo, default_branch) VALUES (?, 'main')", [(r,) for r in REPOS]
        )
        for session, cwd in ((SESSION, "/work/acme"), (OTHER_SESSION, "/work/acme")):
            ledger.ensure_session(conn, session, cwd)
            conn.execute(
                "UPDATE sessions SET first_seen_at = ? WHERE session_id = ?", (now - DAY, session)
            )

        for age, pr in enumerate(PRS):
            repo, number = pr["repo"], pr["number"]
            url = f"https://github.com/{repo}/pull/{number}"
            session = pr.get("session", SESSION)
            pr_id = ledger.upsert_pr(conn, repo, number, url)
            ledger.attach(conn, session, pr_id, "gh")
            state = pr.get("state", "open")
            closed = now - pr["closed_ago"] if "closed_ago" in pr else None
            refreshed = closed or now - pr.get("refreshed_ago", 90)
            counts = {
                s: sum(1 for _, v in pr["checks"] if v == s)
                for s in ("SUCCESS", "FAILURE", "PENDING", "SKIPPED")
            }
            conn.execute(
                "UPDATE prs SET title = ?, body = ?, head_ref = ?, base_ref = ?, head_sha = ?,"
                " state = ?, is_draft = ?, mergeable = ?, review_state = ?, checks_rollup = ?,"
                " checks_pass = ?, checks_fail = ?, checks_pending = ?, checks_skip = ?,"
                " needs_hydrate = 0, created_at = ?, last_refreshed_at = ?, terminal_at = ?"
                " WHERE id = ?",
                (
                    pr["title"],
                    pr["body"],
                    pr["head_ref"],
                    pr["base_ref"],
                    f"{number:040x}",
                    state,
                    pr.get("is_draft", 0),
                    pr.get("mergeable", "MERGEABLE" if state == "open" else "UNKNOWN"),
                    pr["review_state"],
                    rollup(pr["checks"]),
                    counts["SUCCESS"],
                    counts["FAILURE"],
                    counts["PENDING"],
                    counts["SKIPPED"],
                    now - (len(PRS) - age) * HOUR,
                    refreshed,
                    closed,
                    pr_id,
                ),
            )
            conn.execute(
                "UPDATE session_prs SET created_at = ? WHERE pr_id = ?",
                (now - (len(PRS) - age) * HOUR, pr_id),
            )
            conn.executemany(
                "INSERT INTO pr_checks (pr_id, name, state, url) VALUES (?, ?, ?, ?)",
                [
                    (pr_id, name, value, f"https://github.com/{repo}/actions")
                    for name, value in pr["checks"]
                ],
            )
            for step, (kind, detail, delivered) in enumerate(pr.get("events", [])):
                at = now - (len(pr["events"]) - step) * 20 * MINUTE
                conn.execute(
                    "INSERT INTO events (pr_id, session_id, kind, signature, detail,"
                    " created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pr_id, session, kind, detail, detail, at, at + MINUTE if delivered else None),
                )
        ledger.meta_set(conn, "last_tick_at", now - 90)
        conn.commit()
    finally:
        conn.close()
    return db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("state_dir", type=Path, help="an empty directory for prs.db")
    parser.add_argument("--now", type=int, help="the instant every timestamp is relative to")
    args = parser.parse_args(argv)
    seed(args.state_dir, args.now)
    print(SESSION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
