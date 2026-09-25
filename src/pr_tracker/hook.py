"""`pr-tracker hook`: the entry point for agent hooks.

    pr-tracker hook post-bash   after a shell tool call: record a PR it created, then deliver events
    pr-tracker hook post-mcp    after a GitHub MCP create_pull_request call: the same
    pr-tracker hook stop        when a turn ends: show undelivered events without consuming them
    pr-tracker hook             infer the mode from the payload's hook_event_name and tool_name

The hook JSON arrives on stdin. When there is something to say, one JSON
object goes to stdout: `hookSpecificOutput.additionalContext` after a tool
call, `systemMessage` at the end of a turn. Otherwise stdout stays empty.

The payload is Claude Code's hook JSON, which Codex also sends and adapters
for other agents synthesize. The README documents the fields read as a stable
contract.

This runs on every shell call in every session, so it does its work in
process and makes no network call. The common case, no ledger or nothing
queued, costs a stat or one indexed query. It never fails the agent: every
error, including a bad payload, exits 0 with no output.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Mapping
from typing import Any, TextIO

MODES = ("post-bash", "post-mcp", "stop", "auto")
SHELL_TOOLS = {"bash", "shell", "local_shell", "exec_command", "run_shell_command"}
# Codex adds these to every turn-scoped hook input, a deliberate departure from
# Claude Code's schema (codex-rs/hooks/src/schema.rs at rust-v0.156.1).
CODEX_KEYS = ("turn_id",)


def infer_mode(payload: dict[str, Any]) -> str | None:
    event = str(payload.get("hook_event_name") or "")
    tool = str(payload.get("tool_name") or "")
    if event in ("Stop", "stop"):
        return "stop"
    if event not in ("PostToolUse", "post_tool_use", ""):
        return None
    from pr_tracker.scrape import is_mcp_create

    if is_mcp_create(tool):
        return "post-mcp"
    if tool.lower() in SHELL_TOOLS:
        return "post-bash"
    return None


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


def respond(
    mode: str, payload: dict[str, Any], env: Mapping[str, str], agent: str | None = None
) -> dict[str, Any] | None:
    """Do the hook's work and return the JSON to print, if any."""
    from pr_tracker import paths
    from pr_tracker.scrape import command_source, mcp_pr_urls, parse_pr_urls, tool_output

    if mode == "auto":
        mode = infer_mode(payload) or ""
    if mode not in ("post-bash", "post-mcp", "stop"):
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    source = command_source(command) if mode == "post-bash" else None

    where = paths.resolve(env)
    # Nothing has ever been tracked and this call is not creating a PR: the
    # overwhelmingly common case, and it ends here.
    if not source and mode != "post-mcp" and not where.db.exists():
        return None
    from pr_tracker import config, ledger

    agent = agent or infer_agent(payload)
    if source:
        urls = parse_pr_urls(tool_output(payload.get("tool_response")))
        ledger.record(where.db, session_id, cwd, source, urls, agent)
    elif mode == "post-mcp":
        urls = mcp_pr_urls(payload.get("tool_response"))
        ledger.record(where.db, session_id, cwd, "mcp", urls, agent)

    if is_subagent(payload):
        return None
    cfg = config.load(where.config)

    if mode == "stop":
        if not cfg["stop_surface"]:
            return None
        # Peek, never consume: the next tool call delivers these into the
        # agent's own context. Stop only makes them visible to the user.
        lines = ledger.drain(where.db, session_id, peek=True)
        if not lines:
            return None
        return {"systemMessage": ledger.EVENTS_HEADING + "\n" + "\n".join(lines)}

    if not cfg["post_tool_use"]:
        return None
    lines = ledger.drain(where.db, session_id)
    if not lines:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": str(payload.get("hook_event_name") or "PostToolUse"),
            "additionalContext": ledger.EVENTS_HEADING + "\n" + "\n".join(lines),
        }
    }


def parse_args(argv: list[str]) -> tuple[str, str | None] | None:
    mode, agent = "auto", None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--agent" and args:
            agent = args.pop(0)
        elif arg.startswith("--agent="):
            agent = arg.split("=", 1)[1]
        elif arg in MODES:
            mode = arg
        else:
            return None
    return mode, agent or None


def run(
    argv: list[str],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Always returns 0."""
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
        output = respond(parsed[0], payload, env, parsed[1])
        if output:
            stdout.write(json.dumps(output) + "\n")
            stdout.flush()
    except BaseException:
        # Anything left in stdout's buffer would be flushed at exit, and a
        # closed pipe would then turn this exit status non-zero.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), stdout.fileno())
    return 0
