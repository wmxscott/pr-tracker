"""The hook's contract: exit 0 (2 from `wait`), print nothing or one JSON object, no network."""

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


def stop(**extra):
    return {"session_id": "s", "hook_event_name": "Stop", "stop_hook_active": False, **extra}


def test_stop_shows_informational_events_without_consuming(where):
    add_event(where.db, add_pr(where.db, 1), signature="abc", kind="checks_passed")
    code, out = call(["stop"], stop())
    assert code == 0
    assert out == {
        "systemMessage": "Tracked pull request updates:\n"
        "- checks_passed: abc (https://github.com/o/r/pull/1)"
    }
    assert ledger.drain(where.db, "s", peek=True)


@pytest.mark.parametrize("kind", ["checks_failed", "review_changed"])
def test_stop_hands_actionable_events_to_the_agent(where, kind):
    pr_id = add_pr(where.db, 1)
    add_event(where.db, pr_id, kind=kind)
    add_event(where.db, pr_id, signature="abc", kind="checks_passed")
    code, out = call(["stop"], stop())
    assert code == 0
    assert "decision" not in out and "systemMessage" not in out
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Tracked pull request updates:\n")
    assert "checks_passed" in context
    assert ledger.drain(where.db, "s", peek=True) == []


def test_stop_continues_a_turn_only_once(where):
    add_event(where.db, add_pr(where.db, 1))
    _, out = call(["stop"], stop(stop_hook_active=True))
    assert "systemMessage" in out
    assert ledger.drain(where.db, "s", peek=True)


def test_waking_off_only_shows(where):
    add_event(where.db, add_pr(where.db, 1))
    where.config.parent.mkdir(parents=True)
    where.config.write_text("[notify]\nwake = false\n")
    _, out = call(["stop"], stop())
    assert "systemMessage" in out
    assert ledger.drain(where.db, "s", peek=True)


def test_other_agents_only_show_at_stop(where):
    """Only Claude Code, Codex and Pi's extension are known to continue a turn from Stop."""
    add_event(where.db, add_pr(where.db, 1))
    _, out = call(["stop", "--agent", "other"], stop())
    assert "systemMessage" in out
    assert ledger.drain(where.db, "s", peek=True)


def test_pi_continues_a_turn_through_additional_context(where):
    add_event(where.db, add_pr(where.db, 1))
    _, out = call(["stop", "--agent", "pi"], stop())
    assert "checks failing" in out["hookSpecificOutput"]["additionalContext"]
    assert ledger.drain(where.db, "s", peek=True) == []


@pytest.mark.parametrize("kind", ["checks_failed", "review_changed"])
def test_codex_continues_a_turn_through_a_block_decision(where, kind):
    """Codex continues a turn only on `decision: block`, with `reason` as the next prompt,
    and rejects any field its Stop output schema doesn't know."""
    pr_id = add_pr(where.db, 1)
    add_event(where.db, pr_id, kind=kind)
    add_event(where.db, pr_id, signature="abc", kind="checks_passed")
    _, out = call([], stop(turn_id="turn-1"))
    assert set(out) == {"decision", "reason"}
    assert out["decision"] == "block"
    assert out["reason"].startswith("Tracked pull request updates:\n")
    assert "checks_passed" in out["reason"]
    assert ledger.drain(where.db, "s", peek=True) == []


def test_codex_continues_a_turn_only_once(where):
    add_event(where.db, add_pr(where.db, 1))
    _, out = call([], stop(turn_id="turn-1", stop_hook_active=True))
    assert set(out) == {"systemMessage"}
    assert ledger.drain(where.db, "s", peek=True)


def test_codex_only_shows_informational_events(where):
    add_event(where.db, add_pr(where.db, 1), signature="abc", kind="checks_passed")
    _, out = call([], stop(turn_id="turn-1"))
    assert set(out) == {"systemMessage"}
    assert ledger.drain(where.db, "s", peek=True)


def test_any_tool_call_delivers(where):
    add_event(where.db, add_pr(where.db, 1))
    body = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Agent"}
    _, out = call(["post-tool"], body)
    assert "checks failing" in out["hookSpecificOutput"]["additionalContext"]
    assert call([], {**body, "tool_name": "Read"}) == (0, None)


def test_a_prompt_delivers(where):
    add_event(where.db, add_pr(where.db, 1))
    body = {"session_id": "s", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
    _, out = call([], body)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "checks failing" in out["hookSpecificOutput"]["additionalContext"]
    assert ledger.drain(where.db, "s", peek=True) == []


def test_a_failed_shell_call_records_and_delivers(where):
    add_event(where.db, add_pr(where.db, 1))
    body = payload("git push && gh pr create --fill", hook_event_name="PostToolUseFailure")
    del body["tool_response"]
    body["error"] = f"Exit code 1\n{url(7)}\nerror: something after"
    _, out = call([], body)
    assert ("o/r", 7) in tracked(where)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


def test_a_locked_ledger_keeps_events_queued(where):
    add_event(where.db, add_pr(where.db, 1))
    holder = ledger.connect(where.db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert call(["post-bash"], payload()) == (0, None)
    finally:
        holder.rollback()
        holder.close()
    _, out = call(["post-bash"], payload())
    assert "checks failing" in out["hookSpecificOutput"]["additionalContext"]


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
        (payload(tool_name="Read"), "post-tool"),
        (payload(hook_event_name="PostToolUseFailure"), "post-bash"),
        ({"hook_event_name": "Stop"}, "stop"),
        ({"hook_event_name": "UserPromptSubmit"}, "prompt"),
        ({"hook_event_name": "Notification"}, None),
        ({"hook_event_name": "PostToolUse"}, None),
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


def agent_of(where, session="s"):
    conn = ledger.connect(where.db, readonly=True)
    try:
        row = conn.execute("SELECT agent FROM sessions WHERE session_id = ?", (session,))
        return row.fetchone()["agent"]
    finally:
        conn.close()


def claude_payload(command="ls", stdout="", session="s", **extra):
    """Claude Code's PostToolUse input, as documented."""
    return payload(
        command,
        stdout,
        session,
        transcript_path="/t.jsonl",
        permission_mode="default",
        tool_use_id="toolu_1",
        **extra,
    )


def codex_payload(command="ls", stdout="", session="s", **extra):
    """Codex's PostToolUse input: codex-rs/hooks/src/schema.rs at rust-v0.156.1."""
    return payload(
        command,
        stdout,
        session,
        turn_id="turn-1",
        transcript_path=None,
        model="gpt-test",
        permission_mode="default",
        tool_use_id="call_1",
        tool_response=stdout,
        **extra,
    )


def pi_payload(command="ls", stdout="", session="s", event="PostToolUse"):
    """What a Pi extension synthesizes: the documented fields and nothing else."""
    body = {"session_id": session, "cwd": "/work", "hook_event_name": event}
    if event == "PostToolUse":
        body |= {
            "tool_name": "bash",
            "tool_input": {"command": command},
            "tool_response": {"output": stdout},
        }
    return body


def test_codex_payloads_are_labelled_codex(where):
    call([], codex_payload("gh pr create --fill", url(1)))
    assert tracked(where) == [("o/r", 1)]
    assert agent_of(where) == "codex"


def test_codex_stop_payload_infers_its_mode(where):
    add_event(where.db, add_pr(where.db, 1))
    body = {
        "session_id": "s",
        "turn_id": "turn-1",
        "transcript_path": None,
        "cwd": "/work",
        "hook_event_name": "Stop",
        "model": "gpt-test",
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": None,
    }
    _, out = call([], body)
    assert out["decision"] == "block"
    assert "checks failing" in out["reason"]


@pytest.mark.parametrize("args", [["--agent", "other"], ["post-bash", "--agent=other"]])
def test_an_explicit_agent_beats_inference(where, args):
    call(args, codex_payload("gh pr create", url(1)))
    assert agent_of(where) == "other"


def test_claude_payloads_are_labelled_claude(where):
    call(["post-bash"], claude_payload("gh pr create", url(1)))
    call([], claude_payload("gh pr create", url(2), session="sub", agent_id="a1"))
    assert agent_of(where) == "claude"
    assert agent_of(where, "sub") == "claude"


def test_the_label_is_set_once_per_session(where):
    call([], claude_payload("gh pr create", url(1)))
    call([], codex_payload("gh pr create", url(2)))
    assert agent_of(where) == "claude"


def test_a_synthesized_pi_payload(where):
    """Through the real entry point, as a Pi extension would run it."""
    result = run_cli(["--agent", "pi"], json.dumps(pi_payload("gh pr create", url(3))))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert tracked(where) == [("o/r", 3)]
    assert agent_of(where) == "pi"

    add_event(where.db, add_pr(where.db, 3))
    shown = {**pi_payload(event="Stop"), "stop_hook_active": True}
    result = run_cli(["--agent", "pi"], json.dumps(shown))
    assert "checks failing" in json.loads(result.stdout)["systemMessage"]
    result = run_cli(["--agent", "pi"], json.dumps(pi_payload()))
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Tracked pull request updates:")

    add_event(where.db, add_pr(where.db, 3), signature="second")
    result = run_cli(["--agent", "pi"], json.dumps(pi_payload(event="Stop")))
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "checks failing" in context


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


class Clock:
    """A fake clock whose sleep advances time and can run a callback."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)

    def __call__(self):
        return self.now


def wait(clock, seconds=60, agent=None, **extra):
    body = {"session_id": "s", "hook_event_name": "Stop", **extra}
    return hook.wait(
        body, dict(os.environ), seconds, poll=10, sleep=clock.sleep, clock=clock, agent=agent
    )


def test_wait_wakes_on_an_actionable_event(where):
    pr_id = add_pr(where.db, 1)
    clock = Clock(lambda n: n == 3 and add_event(where.db, pr_id))
    text = wait(clock)
    assert (
        text
        == "Tracked pull request updates:\n- checks failing: lint (https://github.com/o/r/pull/1)"
    )
    assert clock.sleeps == 3
    assert ledger.drain(where.db, "s", peek=True) == []


def test_wait_ignores_informational_events_and_gives_up(where):
    add_event(where.db, add_pr(where.db, 1), signature="abc", kind="checks_passed")
    clock = Clock()
    assert wait(clock, seconds=60) is None
    assert clock.now == 60
    assert ledger.drain(where.db, "s", peek=True)
    assert not list((where.state / "waiters").iterdir())


def test_a_newer_waiter_takes_over(where):
    add_pr(where.db, 1)

    def supersede(n):
        for marker in (where.state / "waiters").iterdir():
            marker.write_text("someone else")

    clock = Clock(supersede)
    assert wait(clock) is None
    assert clock.sleeps == 1


@pytest.mark.parametrize(
    "extra", [{"agent_id": "sub-1"}, {"session_id": ""}, {"turn_id": "turn-1"}]
)
def test_wait_does_nothing_for_subagents_codex_or_without_a_session(where, extra):
    add_event(where.db, add_pr(where.db, 1))
    clock = Clock()
    assert wait(clock, **extra) is None
    assert clock.sleeps == 0


def test_wait_does_nothing_without_open_prs(where):
    add_event(where.db, add_pr(where.db, 1, state="merged"))
    clock = Clock()
    assert wait(clock) is None
    assert clock.sleeps == 0
    assert not where.db.with_name("waiters").exists()


def test_wait_stops_when_the_prs_close(where):
    pr_id = add_pr(where.db, 1)

    def merge(n):
        conn = ledger.connect(where.db)
        conn.execute("UPDATE prs SET state = 'merged' WHERE id = ?", (pr_id,))
        conn.commit()
        conn.close()

    clock = Clock(merge)
    assert wait(clock) is None
    assert clock.sleeps == 1


def test_wait_respects_wake_off(where):
    add_event(where.db, add_pr(where.db, 1))
    where.config.parent.mkdir(parents=True)
    where.config.write_text("[notify]\nwake = false\n")
    assert wait(Clock()) is None


def test_wait_cli_exits_2_with_the_events(where):
    """What Claude Code's asyncRewake needs: exit 2, the message on stdout, nothing on stderr."""
    add_event(where.db, add_pr(where.db, 1))
    body = json.dumps({"session_id": "s", "hook_event_name": "Stop"})
    result = run_cli(["wait", "--for", "0.2"], body)
    assert result.returncode == 2
    assert result.stdout.startswith("Tracked pull request updates:\n- checks failing: lint")
    assert result.stderr == ""
    result = run_cli(["wait", "--for=0.2"], body)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert run_cli(["wait", "--for", "soon"], body).returncode == 0


def test_wait_clears_markers_left_by_killed_waiters(where):
    add_pr(where.db, 1)
    waiters = where.state / "waiters"
    waiters.mkdir(parents=True)
    stale, fresh = waiters / "stale", waiters / "fresh"
    stale.write_text("x")
    fresh.write_text("y")
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    wait(Clock(), seconds=10)
    assert sorted(p.name for p in waiters.iterdir()) == ["fresh"]


def test_wait_runs_for_pi_but_not_other_agents(where):
    pr_id = add_pr(where.db, 1)
    clock = Clock(lambda n: n == 1 and add_event(where.db, pr_id))
    assert "checks failing" in wait(clock, agent="pi")
    assert wait(Clock(), agent="other") is None


def test_powershell_records_a_created_pr(where):
    body = payload("gh pr create --fill", url(4))
    body["tool_name"] = "powershell"
    call([], body)
    assert ("o/r", 4) in tracked(where)
