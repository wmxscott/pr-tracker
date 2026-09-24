"""The picker's rendering and actions, without a terminal or fzf."""

import json
import os
import stat
import sys

import pytest
from conftest import add_event, add_pr

from pr_tracker import picker

strip = picker.ANSI_RE.sub


def solo(db, number, session="s", **fields):
    defaults = {
        "title": f"PR {number}",
        "head_ref": f"b{number}",
        "base_ref": "main",
        "needs_hydrate": 0,
        "last_refreshed_at": 4_000_000_000,
    }
    return add_pr(db, number, session=session, **{**defaults, **fields})


@pytest.fixture
def repo_main(where):
    where.state.mkdir(parents=True, exist_ok=True)
    from pr_tracker import ledger

    conn = ledger.connect(where.db)
    conn.execute("INSERT INTO repos (repo, default_branch) VALUES ('o/r', 'main')")
    conn.commit()
    conn.close()


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.json"
    picker.write_state(path, {**picker.DEFAULT_STATE, "cols": 140, "theme": "light"})
    return str(path)


def rows(state_path, session="s"):
    lines, _ = picker.rows_for(state_path, session)
    return [strip("", line) for line in lines]


def g(pr_id, number, depth, **kw):
    row = {
        "id": pr_id,
        "number": number,
        "depth": depth,
        "state": "open",
        "is_draft": 0,
        "checks_rollup": "SUCCESS",
        "head_ref": f"b{number}",
    }
    row.update(kw)
    return row


def test_runs_of_one_are_not_stacks():
    assert [kind for kind, _ in picker.group_plan([g(1, 10, 0), g(2, 11, 0)])] == ["pr", "pr"]


def test_a_root_plus_descendants_is_one_stack():
    plan = picker.group_plan([g(1, 10, 0), g(2, 11, 1), g(3, 12, 2)])
    assert [kind for kind, _ in plan] == ["stack"]
    assert [pr["number"] for pr in plan[0][1]] == [10, 11, 12]


def test_stacks_and_solos_interleave():
    plan = picker.group_plan([g(1, 10, 0), g(2, 11, 1), g(3, 20, 0), g(4, 30, 0), g(5, 31, 1)])
    assert [kind for kind, _ in plan] == ["stack", "pr", "stack"]


def test_stack_title_prefers_the_stack_tool_then_the_root_branch():
    rooted = [g(1, 10, 0, head_ref="feat/thing"), g(2, 11, 1)]
    assert picker.stack_title(rooted) == "feat/thing"
    assert picker.stack_title([dict(rooted[0], stack_id="my-stack"), rooted[1]]) == "my-stack"
    bare = [dict(rooted[0], head_ref=None, stack_id=None), rooted[1]]
    assert picker.stack_title(bare) == "#10"


def test_ready_is_open_undrafted_and_green():
    members = [
        g(1, 10, 0),
        g(2, 11, 1, checks_rollup="FAILURE"),
        g(3, 12, 1, is_draft=1),
        g(4, 13, 1, state="merged"),
    ]
    assert picker.ready_count(members) == 1


def test_numeric_refs_survive_and_junk_is_dropped():
    assert picker.resolve_refs(["3", "1", "3", "x", ""]) == [3, 1]


@pytest.mark.parametrize(
    ("cols", "expected"), [(140, 81), (141, 82), (155, 90), (160, 93), (200, 117)]
)
def test_list_width_matches_what_fzf_leaves(cols, expected):
    # Measured against fzf 0.74 under a pty: an over-estimate wastes screen,
    # an under-estimate hands the status gutter to the truncator.
    assert picker.list_width({"cols": cols}) == expected


def test_fzf_columns_wins_when_set(monkeypatch):
    monkeypatch.setenv("FZF_COLUMNS", "200")
    assert picker.list_width({"cols": 140}) == 117


def test_render_empty(where, state_path):
    assert rows(state_path) == []


def test_render_rows(where, repo_main, state_path):
    solo(where.db, 1, title="Add the thing", checks_pass=3, checks_fail=1, checks_rollup="FAILURE")
    add_pr(where.db, 2)  # recorded, not yet refreshed
    lines = rows(state_path)
    assert len(lines) == 2
    first = next(line for line in lines if "#1" in line)
    assert "Add the thing" in first
    assert first.split("\t")[1].isdigit()
    assert f"{picker.ICO_PASS} 3" in first
    assert f"{picker.ICO_FAIL} 1" in first
    assert picker.ICO_ROLLUP_FAIL in first
    pending = next(line for line in lines if "#2" in line)
    assert "o/r (not yet refreshed)" in pending


def test_render_keeps_status_columns_aligned(where, repo_main, state_path):
    solo(where.db, 1, title="short", checks_pass=1, checks_rollup="SUCCESS")
    solo(where.db, 2, title="a much longer title " * 10, checks_pass=12, checks_rollup="SUCCESS")
    lines = [line.split("\t")[0] for line in rows(state_path)]
    width = picker.list_width({"cols": 140})
    assert all(picker.visible_len(line) <= width for line in lines)
    columns = {line.index(picker.ICO_ROLLUP_PASS) for line in lines}
    assert len(columns) == 1
    assert "…" in next(line for line in lines if "#2" in line)


def test_render_stack(where, repo_main, state_path):
    solo(where.db, 1, head_ref="feat-a", base_ref="main")
    solo(where.db, 2, head_ref="feat-b", base_ref="feat-a")
    lines = rows(state_path)
    assert len(lines) == 3
    assert lines[0].split("\t")[1].startswith("g:")
    assert "feat-a" in lines[0]
    assert "2 PRs" in lines[0]
    assert picker.SPINE in lines[1]
    assert picker.SPINE_END in lines[2]

    state = picker.read_state(state_path)
    state["collapsed"] = [int(lines[0].split("\t")[1][2:])]
    picker.write_state(state_path, state)
    assert len(rows(state_path)) == 1


def test_badges_and_flags(where, repo_main, state_path):
    pr_id = solo(where.db, 1, base_ref="gone", last_refreshed_at=1)
    add_event(where.db, pr_id, signature="a")
    add_event(where.db, pr_id, signature="b")
    from pr_tracker import ledger

    ledger.drain(where.db, "s")
    add_event(where.db, pr_id, signature="c")
    line = rows(state_path)[0]
    assert f"{picker.ICO_BELL_RING} 2/3" in line
    assert picker.ICO_ORPHAN in line
    assert "stale" in line


def test_session_and_scope_filters(where, repo_main, state_path):
    solo(where.db, 1, session="mine")
    solo(where.db, 2, session="other")
    solo(where.db, 3, session="mine", state="merged", terminal_at=1)
    solo(where.db, 4, session="mine", checks_rollup="FAILURE")

    def numbers(**state):
        picker.write_state(state_path, {**picker.read_state(state_path), **state})
        return sorted(int(line.split("#")[1].split()[0]) for line in rows(state_path, "mine"))

    assert numbers(session_only=True, scope="open") == [1, 4]
    assert numbers(session_only=False, scope="open") == [1, 2, 4]
    assert numbers(session_only=True, scope="terminal") == [3]
    assert numbers(session_only=True, scope="attention") == [4]
    assert numbers(session_only=True, scope="all") == [1, 3, 4]


def test_merged_session_widens_scope_not_session(where, state_path):
    solo(where.db, 1, session="mine", state="merged", terminal_at=1)
    solo(where.db, 9, session="other")
    state = {**picker.read_state(state_path), "session_only": True, "scope": "open"}
    assert picker.widen(state_path, "mine", state)
    state = picker.read_state(state_path)
    assert state["scope"] == "all"
    assert state["session_only"], "must not swap in another session's PRs"


def test_empty_session_falls_through_to_all_sessions(where, state_path):
    solo(where.db, 9, session="other")
    state = {**picker.read_state(state_path), "session_only": True, "scope": "open"}
    assert picker.widen(state_path, "ghost", state)
    state = picker.read_state(state_path)
    assert not state["session_only"]
    assert state["scope"] == "open"


def test_nothing_tracked_anywhere(where, state_path):
    state = {**picker.read_state(state_path), "session_only": True}
    assert not picker.widen(state_path, "mine", state)


def test_header_pills(state_path):
    header = strip("", picker.header(picker.read_state(state_path), "s", picker.LATTE))
    assert "session" in header
    assert "open" in header
    assert "q quit" in header
    header = strip("", picker.header(picker.read_state(state_path), "", picker.LATTE))
    assert " all " in header


def test_actions_toggle_state_and_reload(state_path, capsys):
    picker.act("s", state_path, "s")
    assert not picker.read_state(state_path)["session_only"]
    out = capsys.readouterr().out
    assert out.startswith("reload(")
    assert "-m pr_tracker.picker --rows" in out

    picker.act("a", state_path, "s")
    assert picker.read_state(state_path)["scope"] == "attention"
    picker.act("search", state_path, "s")
    assert capsys.readouterr().out.split("\n")[-2].startswith("show-input+unbind(")
    picker.act("escape", state_path, "s")
    assert capsys.readouterr().out.strip() == "abort"
    picker.act("nonsense", state_path, "s")
    assert capsys.readouterr().out.strip() == "ignore"


def test_enter_on_a_header_folds(state_path, capsys):
    picker.act_enter(state_path, "s", "g:7", [])
    assert picker.read_state(state_path)["collapsed"] == [7]
    picker.act_enter(state_path, "s", "g:7", [])
    assert picker.read_state(state_path)["collapsed"] == []


def test_preview(where, monkeypatch, capsys):
    pr_id = solo(where.db, 1, body="Line one\r\n\r\nLine two", review_state="approved")
    from pr_tracker import ledger

    conn = ledger.connect(where.db)
    conn.executemany(
        "INSERT INTO pr_checks (pr_id, name, state) VALUES (?, ?, ?)",
        [(pr_id, "zeta", "SUCCESS"), (pr_id, "alpha", "FAILURE")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("FZF_PREVIEW_COLUMNS", "40")
    picker.preview(pr_id)
    out = strip("", capsys.readouterr().out)
    assert "PR 1" in out
    assert "o/r#1" in out
    assert "b1 → main" in out
    assert "approved" in out
    assert out.index("alpha") < out.index("zeta")
    assert "Line one\n\nLine two" in out
    assert picker.preview(999) == 0


def test_pr_urls_keep_fzf_order(where):
    a = solo(where.db, 1)
    b = solo(where.db, 2)
    assert picker.pr_urls([b, a, 999]) == [
        "https://github.com/o/r/pull/2",
        "https://github.com/o/r/pull/1",
    ]


def test_print_mode(where, repo_main, capsys):
    solo(where.db, 1, title="Printed")
    assert picker.main(["--print", "--session", "s"]) == 0
    out = capsys.readouterr().out
    assert "Printed" in out
    assert "\t" not in out
    assert "\x1b[" not in out


def test_nothing_tracked(where, capsys):
    assert picker.main(["--print"]) == 0
    assert "no tracked pull requests" in capsys.readouterr().err


def fake_fzf(tmp_path, monkeypatch, version):
    bin_dir = tmp_path / "fzf-bin"
    bin_dir.mkdir()
    fzf = bin_dir / "fzf"
    fzf.write_text(f"#!/bin/sh\necho '{version} (fake)'\n")
    fzf.chmod(fzf.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_missing_fzf_falls_back_to_printing(where, monkeypatch, capsys):
    solo(where.db, 1, title="Fallback")
    monkeypatch.setattr(picker.shutil, "which", lambda name: None)
    assert picker.main([]) == 1
    captured = capsys.readouterr()
    assert "fzf not found" in captured.err
    assert "Fallback" in captured.out


def test_old_fzf_is_refused(tmp_path, monkeypatch):
    fake_fzf(tmp_path, monkeypatch, "0.44.1")
    assert "too old" in picker.check_fzf()


def test_new_fzf_is_accepted(tmp_path, monkeypatch):
    fake_fzf(tmp_path, monkeypatch, "0.74.4")
    assert picker.check_fzf() is None


@pytest.mark.parametrize(
    ("env", "cfg", "expected"),
    [
        ({"PR_TRACKER_THEME": "dark"}, {"theme": "light"}, "dark"),
        ({}, {"theme": "dark"}, "dark"),
        ({}, {"theme": "light"}, "light"),
    ],
)
def test_theme_choice(env, cfg, expected):
    assert picker.resolve_theme(cfg, env) == expected


def test_auto_theme_reads_theme_monitor(tmp_path):
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}
    trigger = tmp_path / "data/theme-monitor/theme-change.trigger"
    trigger.parent.mkdir(parents=True)
    trigger.write_text("dark")
    assert picker.resolve_theme({"theme": "auto"}, env) == "dark"
    trigger.write_text("light")
    assert picker.resolve_theme({"theme": "auto"}, env) == "light"


def test_palette_follows_the_state_file():
    assert picker.palette({"theme": "dark"}) is picker.MACCHIATO
    assert picker.palette({"theme": "light"}) is picker.LATTE


def test_icons_are_single_codepoints():
    icons = [
        value
        for name, value in vars(picker).items()
        if name.startswith(("ICO_", "PILL_", "SPINE", "GROUP_SEP"))
    ]
    assert icons
    assert all(isinstance(i, str) and len(i) == 1 for i in icons)


def test_internal_commands_through_main(where, repo_main, state_path, capsys):
    solo(where.db, 1)
    assert picker.main(["--rows", state_path]) == 0
    assert "#1" in strip("", capsys.readouterr().out)
    assert picker.main(["--header", state_path]) == 0
    assert "quit" in capsys.readouterr().out
    assert picker.main(["--preview", state_path, ""]) == 0
    assert capsys.readouterr().out == ""


def test_bound_commands_run_this_interpreter(state_path):
    binds = "\n".join(picker.list_binds(state_path))
    assert sys.executable in binds or json.dumps(sys.executable)[1:-1] in binds
    assert "-P -m pr_tracker.picker" in binds
