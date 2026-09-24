"""The hook's contract: always exit 0, print nothing or one JSON object, never touch the network."""

import io
import json
import os
import subprocess
import sys
import time

import pytest
from conftest import add_event, add_pr, url

from pr_tracker import hook, ledger


def payload(command="ls", stdout="", session="s", **extra):
    body = {
        "session_id": session,
        "cwd": "/work",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False},
    }
    body.update(extra)
    return body


def call(args, body, env=None):
    """Run the hook in process. Returns (exit code, parsed output or None)."""
    out = io.StringIO()
    text = body if isinstance(body, str) else json.dumps(body)
    code = hook.run(args, stdin=io.StringIO(text), stdout=out, env=env or dict(os.environ))
    raw = out.getvalue()
    return code, (json.loads(raw) if raw else None)


def run_cli(args, stdin, env=None):
    """Run the real console entry point in a subprocess."""
    return subprocess.run(
        [sys.executable, "-m", "pr_tracker", "hook", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env or dict(os.environ),
        timeout=30,
    )


def tracked(where):
    if not where.db.exists():
        return []
    conn = ledger.connect(where.db, readonly=True)
    try:
        return [(r["repo"], r["number"]) for r in conn.execute("SELECT repo, number FROM prs")]
    finally:
        conn.close()


def test_gh_pr_create_is_recorded_offline(where, fake_gh):
    code, out = call(["post-bash"], payload("gh pr create --fill", url(12)))
    assert (code, out) == (0, None)
    assert tracked(where) == [("o/r", 12)]
    assert fake_gh.calls() == []


def test_gh_pr_view_is_not_recorded(where):
    call(["post-bash"], payload("gh pr view 12 --json url", url(12)))
    assert tracked(where) == []
    assert not where.db.exists()


def test_gh_stack_submit_records_every_pr_and_queues_a_scan(where):
    out = f"{url(1)}\n{url(2)}\n{url(3)}\n"
    call(["post-bash"], payload("gh stack submit --auto", out))
    assert sorted(n for _, n in tracked(where)) == [1, 2, 3]
    conn = ledger.connect(where.db, readonly=True)
    assert conn.execute("SELECT local_path FROM stack_scans").fetchone()[0] == "/work"
    conn.close()


def test_mcp_create_pull_request(where):
    response = [
        {
            "type": "text",
            "text": json.dumps({"html_url": url(5), "body": f"see {url(4)}"}),
        }
    ]
    body = payload(tool_name="mcp__github__create_pull_request", tool_response=response)
    assert call(["post-mcp"], body) == (0, None)
    assert tracked(where) == [("o/r", 5)]


def test_events_are_delivered_as_additional_context(where):
    add_event(where.db, add_pr(where.db, 1))
    code, out = call(["post-bash"], payload())
    assert code == 0
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "Tracked pull request updates:\n"
            "- checks failing: lint (https://github.com/o/r/pull/1)",
        }
    }
    # consumed: the next call is quiet
    assert call(["post-bash"], payload()) == (0, None)


def test_mcp_calls_deliver_too(where):
    add_event(where.db, add_pr(where.db, 1))
    body = payload(tool_name="mcp__github__create_pull_request", tool_response=[])
    _, out = call(["post-mcp"], body)
    assert "checks failing" in out["hookSpecificOutput"]["additionalContext"]


def test_other_sessions_events_stay_queued(where):
    add_event(where.db, add_pr(where.db, 1, session="other"), session="other")
    assert call(["post-bash"], payload()) == (0, None)
    assert ledger.drain(where.db, "other", peek=True)


def test_subagents_record_but_never_drain(where):
    add_event(where.db, add_pr(where.db, 1))
    body = payload("gh pr create", url(2), agent_id="sub-1")
    assert call(["post-bash"], body) == (0, None)
    assert ("o/r", 2) in tracked(where)
    assert call(["stop"], {"session_id": "s", "agent_id": "sub-1"}) == (0, None)
    assert ledger.drain(where.db, "s", peek=True)


def test_stop_shows_without_consuming(where):
    add_event(where.db, add_pr(where.db, 1))
    code, out = call(["stop"], {"session_id": "s", "hook_event_name": "Stop"})
    assert code == 0
    assert out == {
        "systemMessage": "Tracked pull request updates:\n"
        "- checks failing: lint (https://github.com/o/r/pull/1)"
    }
    assert "decision" not in out
    assert ledger.drain(where.db, "s", peek=True)


def test_notify_settings_silence_delivery(where):
    add_event(where.db, add_pr(where.db, 1))
    where.config.parent.mkdir(parents=True)
    where.config.write_text("[notify]\npost_tool_use = false\nstop_surface = false\n")
    assert call(["post-bash"], payload()) == (0, None)
    assert call(["stop"], {"session_id": "s"}) == (0, None)
    assert ledger.drain(where.db, "s", peek=True)
    # recording still happens
    call(["post-bash"], payload("gh pr create", url(9)))
    assert ("o/r", 9) in tracked(where)


def test_disabled(where):
    env = {**os.environ, "PR_TRACKER_DISABLE": "1"}
    assert call(["post-bash"], payload("gh pr create", url(1)), env) == (0, None)
    assert not where.db.exists()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (payload(), "post-bash"),
        (payload(tool_name="shell"), "post-bash"),
        (payload(tool_name="mcp__github__create_pull_request"), "post-mcp"),
        (payload(tool_name="Read"), None),
        ({"hook_event_name": "Stop"}, "stop"),
        ({"hook_event_name": "UserPromptSubmit"}, None),
    ],
)
def test_infer_mode(body, expected):
    assert hook.infer_mode(body) == expected


def test_auto_mode(where):
    call([], payload("gh pr create", url(3)))
    call(["auto"], payload(tool_name="mcp__github__create_pull_request", tool_response=url(4)))
    assert sorted(n for _, n in tracked(where)) == [3, 4]


def test_argv_list_commands(where):
    body = payload(["bash", "-lc", "gh pr create --fill"], url(8), tool_name="shell")
    call(["post-bash", "--agent", "codex"], body)
    assert tracked(where) == [("o/r", 8)]
    conn = ledger.connect(where.db, readonly=True)
    assert conn.execute("SELECT agent FROM sessions").fetchone()["agent"] == "codex"
    conn.close()


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[]",
        "null",
        '"string"',
        "{}",
        '{"session_id": 5}',
        '{"session_id": "s", "tool_input": "oops", "tool_response": 7}',
        '{"session_id": "s", "tool_input": {"command": ["gh", "pr", "create"]}}',
    ],
)
def test_bad_input_is_ignored(stdin, where):
    for mode in ("post-bash", "post-mcp", "stop", "auto"):
        assert call([mode], stdin) == (0, None)


def test_unknown_mode_is_ignored(where):
    assert call(["pre-bash"], payload("gh pr create", url(1))) == (0, None)
    assert call(["--agent"], payload("gh pr create", url(1))) == (0, None)
    assert not where.db.exists()


def test_a_broken_ledger_is_swallowed(where):
    where.state.mkdir(parents=True)
    where.db.write_text("this is not a database")
    assert call(["post-bash"], payload("gh pr create", url(1))) == (0, None)
    assert call(["stop"], {"session_id": "s"}) == (0, None)


def test_an_unwritable_state_dir_is_swallowed(where):
    where.state.parent.mkdir(parents=True)
    where.state.write_text("a file where the directory should be")
    assert call(["post-bash"], payload("gh pr create", url(1))) == (0, None)


def test_cli_contract(where):
    """Through the real entry point: exit 0, stdout empty or exactly one JSON object."""
    add_event(where.db, add_pr(where.db, 1))
    result = run_cli(["post-bash"], json.dumps(payload()))
    assert result.returncode == 0
    assert result.stderr == ""
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    for stdin in ("", "garbage", json.dumps(payload())):
        result = run_cli(["post-bash"], stdin)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_cli_survives_a_closed_stdout(where):
    add_event(where.db, add_pr(where.db, 1))
    proc = subprocess.Popen(
        [sys.executable, "-m", "pr_tracker", "hook", "post-bash"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc.stdout.close()
    proc.stdin.write(json.dumps(payload()))
    proc.stdin.close()
    assert proc.wait(timeout=30) == 0
    assert proc.stderr.read() == ""


def test_the_common_case_is_fast(where):
    # No ledger and not a PR command: nothing but startup and a stat.
    body = json.dumps(payload())
    start = time.monotonic()
    for _ in range(3):
        assert run_cli(["post-bash"], body).returncode == 0
    assert (time.monotonic() - start) / 3 < 1.0
