"""The README screenshot's seed data renders in the picker."""

import importlib.util
from pathlib import Path

import pytest

from pr_tracker import picker

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo-ledger.py"


@pytest.fixture
def demo(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("demo_ledger", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "home" / "demo-state"
    module.seed(state)
    monkeypatch.setenv("PR_TRACKER_STATE_DIR", str(state))
    monkeypatch.setenv("COLUMNS", "160")
    return module


def printed(capsys, *args):
    assert picker.main(["--print", *args]) == 0
    return capsys.readouterr().out.splitlines()


def test_demo_session_renders_stacks_and_states(demo, capsys, fake_gh):
    lines = printed(capsys, "--session", demo.SESSION)
    text = "\n".join(lines)
    assert "billing/invoice-model" in text and "3 PRs · 1 ready" in text
    assert "theme/tokens" in text and "2 PRs · 2 ready" in text
    assert sum(picker.SPINE_END in line for line in lines) == 2
    assert f"{picker.ICO_FAIL} 2" in text
    assert f"{picker.ICO_WAIT} 3" in text
    assert f"{picker.ICO_BELL_RING} 1/2" in text
    assert picker.ICO_ORPHAN in text
    assert "stale 2h" in text
    assert "Rate-limit the login endpoint" not in text, "merged PRs are not in the open scope"
    assert "Log queries slower" not in text, "another session's PR leaked in"
    assert fake_gh.calls() == [], "rendering must never call gh"


def test_demo_has_terminal_prs_and_another_session(demo, tmp_path):
    state_path = tmp_path / "picker.json"
    picker.write_state(
        state_path, {**picker.DEFAULT_STATE, "session_only": False, "scope": "all", "cols": 160}
    )
    lines, _ = picker.rows_for(state_path, demo.SESSION)
    text = picker.ANSI_RE.sub("", "\n".join(lines))
    assert picker.ICO_MERGED in text and picker.ICO_CLOSED in text
    assert "Log queries slower" in text


def test_demo_urls_are_all_fake(demo):
    from pr_tracker import ledger, paths

    conn = ledger.connect(paths.resolve().db, readonly=True)
    try:
        urls = [r["url"] for r in conn.execute("SELECT url FROM prs")]
    finally:
        conn.close()
    assert urls
    assert all(url.startswith("https://github.com/acme/") for url in urls)
