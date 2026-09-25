"""`pr-tracker hook`: the entry point for agent hooks.

    pr-tracker hook post-bash   after a shell tool call: record a PR it created, then deliver events
    pr-tracker hook post-mcp    after a GitHub MCP create_pull_request call: the same
    pr-tracker hook post-tool   after any other tool call: deliver events
    pr-tracker hook prompt      when the user submits a prompt: deliver events
    pr-tracker hook stop        when a turn ends: hand the agent red checks or a review
                                decision so it keeps going, else show events to the user
    pr-tracker hook wait        after a turn ends, in the background: wait for red checks or
                                a review decision, print them and exit 2 to wake the agent
    pr-tracker hook             infer the mode from the payload's hook_event_name and tool_name

The hook JSON arrives on stdin. When there is something to say, one JSON
object goes to stdout: `hookSpecificOutput.additionalContext` for the agent,
or `systemMessage` for the user. Otherwise stdout stays empty.

The payload is Claude Code's hook JSON, which Codex also sends and adapters
for other agents synthesize. The README documents the fields read as a stable
contract.

This runs on every tool call in every session, so it does its work in
process and makes no network call. The common case, no ledger or nothing
queued, costs a stat or one indexed query. It never fails the agent: every
error, including a bad payload, exits 0 with no output. `wait` is the one
exception, by design: it exits 2 when it has events, which is how Claude
Code's `asyncRewake` hooks wake an idle session.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

MODES = ("post-bash", "post-mcp", "post-tool", "prompt", "stop", "wait", "auto")
DELIVERING = ("post-bash", "post-mcp", "post-tool", "prompt")
EVENT_NAMES = {"prompt": "UserPromptSubmit", "stop": "Stop"}
TOOL_EVENTS = ("PostToolUse", "PostToolUseFailure", "post_tool_use", "")
SHELL_TOOLS = {"bash", "shell", "local_shell", "exec_command", "run_shell_command", "powershell"}
# Codex adds these to every turn-scoped hook input, a deliberate departure from
# Claude Code's schema (codex-rs/hooks/src/schema.rs at rust-v0.156.1).
CODEX_KEYS = ("turn_id",)
WAIT_SECONDS = 540
WAIT_POLL_SECONDS = 15
WAKE_EXIT = 2
WAITERS = ("claude", "pi")


def infer_mode(payload: dict[str, Any]) -> str | None:
    event = str(payload.get("hook_event_name") or "")
    tool = str(payload.get("tool_name") or "")
    if event in ("Stop", "stop"):
        return "stop"
    if event in ("UserPromptSubmit", "user_prompt_submit"):
        return "prompt"
    if event not in TOOL_EVENTS:
        return None
    from pr_tracker.scrape import is_mcp_create

    if is_mcp_create(tool):
        return "post-mcp"
    if tool.lower() in SHELL_TOOLS:
        return "post-bash"
    return "post-tool" if tool else None


def infer_agent(payload: dict[str, Any]) -> str:
    """The agent to label a new session with when `--agent` is not given.

    Codex marks its payloads; Claude Code's are the unmarked baseline. Any
    other agent's adapter passes `--agent`.
    """
    return "codex" if any(key in payload for key in CODEX_KEYS) else "claude"


def is_subagent(payload: dict[str, Any]) -> bool:
    """Subagent tool calls must not drain the root session's queue.

    A drained event would land in a context that is discarded when the
    subagent returns, and the session that owns the PR would never see it.
    """
    return bool(payload.get("agent_id") or payload.get("subagent_id"))


def _session(payload: dict[str, Any]) -> str | None:
    session_id = payload.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def _text(rows: list) -> str:
    from pr_tracker import ledger

    return ledger.EVENTS_HEADING + "\n" + "\n".join(ledger.event_lines(rows))


def respond(
    mode: str, payload: dict[str, Any], env: Mapping[str, str], agent: str | None = None
) -> dict[str, Any] | None:
    """Do the hook's work and return the JSON to print, if any."""
    from pr_tracker import paths
    from pr_tracker.scrape import command_source, mcp_pr_urls, parse_pr_urls, tool_output

    if mode == "auto":
        mode = infer_mode(payload) or ""
    if mode not in (*DELIVERING, "stop"):
        return None
    session_id = _session(payload)
    if not session_id:
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    source = command_source(command) if mode == "post-bash" else None
    # PostToolUseFailure carries the output in `error` rather than `tool_response`.
    response = payload.get("tool_response", payload.get("error"))

    where = paths.resolve(env)
    # Nothing has ever been tracked and this call is not creating a PR: the
    # overwhelmingly common case, and it ends here.
    if not source and mode != "post-mcp" and not where.db.exists():
        return None
    from pr_tracker import config, ledger

    agent = agent or infer_agent(payload)
    if source:
        urls = parse_pr_urls(tool_output(response))
        ledger.record(where.db, session_id, cwd, source, urls, agent)
    elif mode == "post-mcp":
        urls = mcp_pr_urls(response)
        ledger.record(where.db, session_id, cwd, "mcp", urls, agent)

    if is_subagent(payload):
        return None
    cfg = config.load(where.config)
    event = str(payload.get("hook_event_name") or EVENT_NAMES.get(mode, "PostToolUse"))

    if mode == "stop":
        rows = ledger.pending(where.db, session_id)
        if not rows:
            return None
        # Claude Code and Pi's extension continue a turn on Stop additionalContext,
        # Codex only on a block decision whose reason becomes the next prompt.
        # stop_hook_active means this turn already continued once.
        wake = cfg["wake"] and cfg["post_tool_use"] and agent in ("claude", "codex", "pi")
        if wake and not payload.get("stop_hook_active") and any(map(ledger.is_actionable, rows)):
            claimed = ledger.claim(where.db, session_id)
            if not claimed:
                return None
            if agent == "codex":
                return {"decision": "block", "reason": _text(claimed)}
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": _text(claimed),
                }
            }
        if not cfg["stop_surface"]:
            return None
        # Peek, never consume: the next tool call or prompt delivers these into
        # the agent's own context. Here they are only shown to the user.
        return {"systemMessage": _text(rows)}

    if not cfg["post_tool_use"] or not ledger.pending(where.db, session_id):
        return None
    claimed = ledger.claim(where.db, session_id)
    if not claimed:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": _text(claimed)}}


def wait(
    payload: dict[str, Any],
    env: Mapping[str, str],
    seconds: float = WAIT_SECONDS,
    poll: float = WAIT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    agent: str | None = None,
) -> str | None:
    """Block until the session has red checks or a review decision, then claim its events.

    Registered as a Claude Code Stop hook with `asyncRewake`, so it runs in
    the background after every turn and its output wakes the idle agent. Each
    new waiter for a session takes over from the last, so they never pile up.
    """
    import hashlib

    from pr_tracker import config, ledger, paths

    session_id = _session(payload)
    # Only Claude Code and Pi's extension run this in the background; anywhere
    # else it would hold up the turn.
    if not session_id or is_subagent(payload) or (agent or infer_agent(payload)) not in WAITERS:
        return None
    where = paths.resolve(env)
    cfg = config.load(where.config)
    if not (cfg["wake"] and cfg["post_tool_use"]) or not ledger.has_open_prs(where.db, session_id):
        return None
    waiters = where.state / "waiters"
    marker = waiters / hashlib.sha256(session_id.encode()).hexdigest()[:32]
    token = f"{os.getpid()}:{time.time_ns()}"
    waiters.mkdir(parents=True, exist_ok=True)
    # A waiter killed at its timeout, or with its session, leaves its marker.
    for old in waiters.iterdir():
        with contextlib.suppress(OSError):
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink()
    marker.write_text(token)

    def current() -> bool:
        with contextlib.suppress(OSError):
            return marker.read_text() == token
        return False

    end = clock() + seconds
    try:
        while clock() < end:
            sleep(max(0.0, min(poll, end - clock())))
            if not current() or not ledger.has_open_prs(where.db, session_id):
                return None
            rows = ledger.pending(where.db, session_id)
            if any(map(ledger.is_actionable, rows)):
                claimed = ledger.claim(where.db, session_id)
                if claimed:
                    return _text(claimed)
        return None
    finally:
        if current():
            with contextlib.suppress(OSError):
                marker.unlink()


def parse_args(argv: list[str]) -> tuple[str, str | None, float] | None:
    mode, agent, seconds = "auto", None, float(WAIT_SECONDS)
    args = list(argv)
    try:
        while args:
            arg = args.pop(0)
            if arg == "--agent" and args:
                agent = args.pop(0)
            elif arg.startswith("--agent="):
                agent = arg.split("=", 1)[1]
            elif arg == "--for" and args:
                seconds = float(args.pop(0))
            elif arg.startswith("--for="):
                seconds = float(arg.split("=", 1)[1])
            elif arg in MODES:
                mode = arg
            else:
                return None
    except ValueError:
        return None
    return mode, agent or None, seconds


def run(
    argv: list[str],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Returns 0, or 2 from `wait` when it printed events to wake the agent with."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    env = os.environ if env is None else env
    try:
        if env.get("PR_TRACKER_DISABLE"):
            return 0
        parsed = parse_args(argv)
        if parsed is None or stdin.isatty():
            return 0
        payload = json.loads(stdin.read() or "null")
        if not isinstance(payload, dict):
            return 0
        mode, agent, seconds = parsed
        if mode == "wait":
            text = wait(payload, env, seconds, agent=agent)
            if text:
                stdout.write(text + "\n")
                stdout.flush()
                return WAKE_EXIT
            return 0
        output = respond(mode, payload, env, agent)
        if output:
            stdout.write(json.dumps(output) + "\n")
            stdout.flush()
    except BaseException:
        # Anything left in stdout's buffer would be flushed at exit, and a
        # closed pipe would then turn this exit status non-zero.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), stdout.fileno())
    return 0
