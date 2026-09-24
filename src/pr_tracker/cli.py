"""Command-line interface."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

SESSION_VARS = ("PR_TRACKER_SESSION_ID", "CLAUDE_SESSION_ID")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    # The hook runs on every agent tool call: skip argparse and everything
    # else it does not need.
    if argv[:1] == ["hook"]:
        from pr_tracker import hook

        return hook.run(argv[1:], env=env)
    return _main(argv, env)


def _main(argv: list[str], env: Mapping[str, str]) -> int:
    import argparse

    from pr_tracker import __version__

    parser = argparse.ArgumentParser(
        prog="pr-tracker",
        description="Track the pull requests your coding agents open.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def session_arg(p: argparse.ArgumentParser, help: str) -> None:
        p.add_argument("--session", help=f"{help} (default: $PR_TRACKER_SESSION_ID)")

    p = sub.add_parser("status", help="show the ledger, the last refresh and any problems")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "refresh", help="refresh tracked PRs if a refresh is due; what the service runs"
    )
    p.add_argument("--force", action="store_true", help="refresh now, whenever the last one was")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("list", help="print tracked PRs as JSON, stacked PRs under their base")
    session_arg(p, "the session for --scope session")
    p.add_argument("--scope", choices=["session", "open", "all"], default="open")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("adopt", help="attach an existing PR to a session")
    p.add_argument("ref", nargs="?", help="PR URL or number; default: the current branch's PR")
    session_arg(p, "the session to attach it to")
    p.add_argument("--cwd", help="repository to resolve a number or branch in")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("untrack", help="detach a PR from a session, or forget it")
    p.add_argument("ref", help="PR URL, or owner/repo#number")
    session_arg(p, "the session to detach it from")
    p.add_argument(
        "--all-sessions", action="store_true", help="forget the PR entirely, for every session"
    )
    p.set_defaults(func=cmd_untrack)

    p = sub.add_parser("pick", help="browse tracked PRs in fzf; the same as `prs`")
    session_arg(p, "start scoped to this session")
    p.add_argument("--print", dest="print_rows", action="store_true", help="print the list, no fzf")
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser(
        "hook",
        help="agent hook entry point: post-bash, post-mcp or stop, reading hook JSON on stdin",
    )
    p.add_argument("mode", nargs="?", choices=["post-bash", "post-mcp", "stop", "auto"])
    p.add_argument("--agent", help="agent name recorded with new sessions (default: claude)")

    p = sub.add_parser("record", help="attach PR URLs to a session, for custom integrations")
    session_arg(p, "the session to attach them to")
    p.add_argument("--cwd", help="the checkout the PRs came from")
    p.add_argument("--source", default="gh", choices=["gh", "stack", "mcp", "adopt"])
    p.add_argument("--agent", default="claude")
    p.add_argument("--text", help="text to find PR URLs in; '-' reads stdin")
    p.add_argument("--url", action="append", default=[], help="a PR URL; repeatable")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("drain", help="print a session's pending events and mark them delivered")
    session_arg(p, "the session")
    p.add_argument("--peek", action="store_true", help="print without marking them delivered")
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("flush", help="mark pending events delivered without printing them")
    p.add_argument("--session", help="only this session's; default: every session's")
    p.set_defaults(func=cmd_flush)

    p = sub.add_parser("init", help="create or upgrade the ledger")
    p.set_defaults(func=cmd_init)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args, env)


def _err(message: str) -> None:
    print(f"pr-tracker: {message}", file=sys.stderr)


def _session(args, env: Mapping[str, str]) -> str:
    if getattr(args, "session", None):
        return args.session
    for var in SESSION_VARS:
        if env.get(var):
            return env[var]
    return ""


def _require_session(args, env: Mapping[str, str]) -> str | None:
    session = _session(args, env)
    if not session:
        _err("no session: pass --session or set PR_TRACKER_SESSION_ID")
        return None
    return session


def _home(path) -> str:
    text = str(path)
    home = os.path.expanduser("~")
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


def cmd_init(args, env) -> int:
    from pr_tracker import ledger, paths

    where = paths.resolve(env)
    ledger.connect(where.db).close()
    print(where.db)
    return 0


def cmd_record(args, env) -> int:
    from pr_tracker import ledger, paths
    from pr_tracker.scrape import parse_pr_urls

    session = _require_session(args, env)
    if not session:
        return 1
    text = sys.stdin.read() if args.text == "-" else (args.text or "")
    urls = parse_pr_urls("\n".join([text, *args.url]))
    count = ledger.record(
        paths.resolve(env).db,
        session,
        args.cwd or os.getcwd(),
        args.source,
        urls,
        args.agent,
    )
    for repo, number, _ in urls:
        print(f"tracking {repo}#{number}")
    return 0 if count or args.source == "stack" else 1


def cmd_adopt(args, env) -> int:
    import json

    from pr_tracker import github, ledger, paths
    from pr_tracker.scrape import parse_pr_urls

    session = _require_session(args, env)
    if not session:
        return 1
    cwd = args.cwd or os.getcwd()
    urls = parse_pr_urls(args.ref or "")
    if not urls:
        if args.ref and not args.ref.isdigit():
            _err(f"not a PR URL or number: {args.ref}")
            return 1
        selector = [args.ref] if args.ref else []
        result = github.gh(["pr", "view", *selector, "--json", "url"], cwd=cwd, timeout=30)
        if result.code != 0:
            _err(result.err.strip() or "no PR found")
            return 1
        try:
            urls = parse_pr_urls(json.loads(result.out).get("url", ""))
        except (ValueError, AttributeError):
            urls = []
    if not urls:
        _err("nothing to adopt")
        return 1
    ledger.record(paths.resolve(env).db, session, cwd, "adopt", urls)
    for repo, number, _ in urls:
        print(f"tracking {repo}#{number}")
    return 0


def cmd_refresh(args, env) -> int:
    from pr_tracker import config, paths, refresh

    where = paths.resolve(env)
    cfg, problems = config.read(where.config)
    if args.force:
        for problem in problems:
            _err(problem)
    report = refresh.tick(where, cfg, force=args.force)
    if args.force and sys.stdout.isatty():
        if report.locked:
            print("another refresh is running")
        else:
            failed = f", {len(report.failed)} failed" if report.failed else ""
            print(
                f"refreshed {report.refreshed} PR(s) in {report.repos} repo(s){failed};"
                f" {report.events} new event(s)"
            )
    return 0


def cmd_drain(args, env) -> int:
    from pr_tracker import ledger, paths

    session = _require_session(args, env)
    if not session:
        return 1
    for line in ledger.drain(paths.resolve(env).db, session, peek=args.peek):
        print(line)
    return 0


def cmd_flush(args, env) -> int:
    from pr_tracker import ledger, paths

    print(f"flushed {ledger.flush(paths.resolve(env).db, args.session)} pending event(s)")
    return 0


def cmd_list(args, env) -> int:
    import json

    from pr_tracker import config, ledger, paths

    session = _session(args, env)
    if args.scope == "session" and not session:
        _err("--scope session needs --session or PR_TRACKER_SESSION_ID")
        return 1
    where = paths.resolve(env)
    cfg = config.load(where.config)
    out: dict = {"prs": [], "last_tick_at": None, "config": cfg}
    if where.db.exists():
        conn = ledger.connect(where.db, readonly=True)
        try:
            rows = [dict(r) for r in ledger.rows_for_scope(conn, args.scope, session)]
            out["prs"] = ledger.build_forest(rows, ledger.default_branches(conn))
            last = ledger.meta_get(conn, "last_tick_at")
            out["last_tick_at"] = int(last) if last else None
        finally:
            conn.close()
    print(json.dumps(out, default=str))
    return 0


def _parse_ref(ref: str) -> tuple[str, int] | None:
    import re

    from pr_tracker.scrape import parse_pr_urls

    found = parse_pr_urls(ref)
    if found:
        return found[0][0], found[0][1]
    match = re.fullmatch(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)", ref.strip())
    return (match.group(1), int(match.group(2))) if match else None


def cmd_untrack(args, env) -> int:
    from pr_tracker import ledger, paths

    parsed = _parse_ref(args.ref)
    if not parsed:
        _err(f"not a PR URL or owner/repo#number: {args.ref}")
        return 1
    session = None
    if not args.all_sessions:
        session = _require_session(args, env)
        if not session:
            return 1
    repo, number = parsed
    if ledger.untrack(paths.resolve(env).db, repo, number, session):
        print(f"untracked {repo}#{number}")
    else:
        print(f"{repo}#{number} was not tracked")
    return 0


def cmd_pick(args, env) -> int:
    from pr_tracker import picker

    return picker.run(args.session, args.print_rows)


def cmd_status(args, env) -> int:
    import shutil
    import time

    from pr_tracker import config, ledger, paths

    where = paths.resolve(env)
    _, problems = config.read(where.config)
    exists = where.config.exists()
    print(f"config      {_home(where.config)}{'' if exists else ' (none; using defaults)'}")
    print(f"ledger      {_home(where.db)}")
    gh = shutil.which("gh")
    print(f"gh          {_home(gh) if gh else 'not found on PATH'}")
    failing = []
    if where.db.exists():
        conn = ledger.connect(where.db, readonly=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) total, SUM(state = 'open') open FROM prs"
            ).fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) c FROM events WHERE consumed_at IS NULL"
            ).fetchone()["c"]
            last = ledger.meta_get(conn, "last_tick_at")
            failing = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'failing:%' ORDER BY key"
            ).fetchall()
        finally:
            conn.close()
        age = f"{ledger.humanize_age(time.time() - int(last))} ago" if last else "never"
        print(f"prs         {count['total']} ({count['open'] or 0} open)")
        print(f"events      {pending} pending")
        print(f"last tick   {age}")
    else:
        print("prs         none tracked yet")
    for row in failing:
        print(f"failing     {row['key'].split(':', 1)[1]}: {row['value']}")
    for problem in problems:
        print(f"warning     {problem}")
    return 0
